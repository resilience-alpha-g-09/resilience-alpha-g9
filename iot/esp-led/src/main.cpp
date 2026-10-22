// LED bicolore pilotée par l'état d'authentification du back-office.
// Interroge le serveur du réseau retenu (WIFI_TARGETS, par priorité) toutes les LED_POLL_MS ms et applique le motif localement :
//   off | green_blink (0,5 s) | green_solid | red_blink (10 clignotements puis off) | red_solid
#include <Arduino.h>
#include <ArduinoJson.h>
#if defined(ESP32)
  #include <WiFi.h>
  #include <HTTPClient.h>
#else
  #include <ESP8266WiFi.h>
  #include <ESP8266HTTPClient.h>
#endif
#include <WiFiUdp.h>
#ifndef BEACON_PORT
  #define BEACON_PORT 4210   // balise UDP émise par l'agent caméra du PC Hôte : "RA-LED http://<ip>/admin/api/led-state ..."
#endif
#include "config.h"

enum State { OFF, GREEN_BLINK, GREEN_SOLID, RED_BLINK, RED_SOLID };
State state = OFF;
unsigned long lastPoll = 0, lastToggle = 0, stateSince = 0;
bool ledOn = false;
int blinksDone = 0;
WiFiClient wifi;
WiFiUDP beacon;

static void leds(bool green, bool red) {
  digitalWrite(GREEN_PIN, green ? HIGH : LOW); digitalWrite(RED_PIN, red ? HIGH : LOW);
#ifdef STATUS_PIN   // LED intégrée de la carte : reflète « une LED allumée » (test sans câblage externe)
  digitalWrite(STATUS_PIN, (green || red) ? HIGH : LOW);
#endif
}

static State parse(const char *s) {
  if (!strcmp(s, "green_blink")) return GREEN_BLINK;
  if (!strcmp(s, "green_solid")) return GREEN_SOLID;
  if (!strcmp(s, "red_blink"))   return RED_BLINK;
  if (!strcmp(s, "red_solid"))   return RED_SOLID;
  return OFF;
}

static const int WIFI_COUNT = sizeof(WIFI_TARGETS) / sizeof(WIFI_TARGETS[0]);
int target = -1;   // index du réseau actuellement connecté
String serverUrl;           // URL réellement interrogée : celle du réseau, ou celle découverte sur le sous-réseau
int pollFails = 1, discoveryFails = 0;   // 1 au départ : la balise peut donner l'adresse avant même la 1re réponse

static void scanWifi() {   // diagnostic : l'ESP32 ne voit que le 2,4 GHz — un hotspot en 5 GHz n'apparaît pas ici
  int n = WiFi.scanNetworks();
  Serial.printf("scan : %d réseau(x) 2,4 GHz visibles\n", n);
  for (int i = 0; i < n && i < 15; i++) {
    const char *tag = "";
    for (int t = 0; t < WIFI_COUNT; t++) if (WiFi.SSID(i) == WIFI_TARGETS[t].ssid) tag = "  <= cible";
    Serial.printf("  %-28s ch%-2d %4d dBm%s\n", WiFi.SSID(i).c_str(), (int) WiFi.channel(i), (int) WiFi.RSSI(i), tag);
  }
  WiFi.scanDelete();
}

// Essaie chaque réseau de WIFI_TARGETS dans l'ordre (WIFI_TRY_MS chacun), en boucle jusqu'à connexion.
// Orange clignotant pendant la recherche. Le serveur interrogé est celui du réseau retenu.
static void connectWifi(int first = 0) {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);          // latence réseau stable (utile pour la découverte du serveur)
  WiFi.disconnect(true);
  state = OFF; target = -1; serverUrl = ""; pollFails = 1; discoveryFails = 0; leds(false, false);
  scanWifi();
  for (int round = 0;; round++) {
    for (int k = 0; k < WIFI_COUNT; k++) {
      int t = (first + k) % WIFI_COUNT;
      Serial.printf("WiFi %s (%d/%d)", WIFI_TARGETS[t].ssid, t + 1, WIFI_COUNT);
      WiFi.begin(WIFI_TARGETS[t].ssid, WIFI_TARGETS[t].password);
      unsigned long t0 = millis();
      while (WiFi.status() != WL_CONNECTED && millis() - t0 < WIFI_TRY_MS) {
        delay(250); Serial.print("."); ledOn = !ledOn; leds(ledOn, !ledOn);
      }
      if (WiFi.status() == WL_CONNECTED) {
        target = t; serverUrl = WIFI_TARGETS[t].ledStateUrl; leds(false, false);
        beacon.stop(); beacon.begin(BEACON_PORT);
        Serial.printf("\nconnecté à %s, IP %s, serveur %s\n", WIFI_TARGETS[t].ssid, WiFi.localIP().toString().c_str(), WIFI_TARGETS[t].ledStateUrl);
        return;
      }
      Serial.printf(" échec (statut %d)\n", WiFi.status());
      WiFi.disconnect(true);
    }
    if (round % 3 == 2) scanWifi();   // re-scan de temps en temps pour le diagnostic
  }
}

static bool probe(const String &url, int connectMs, int readMs) {
  HTTPClient http;
  http.setConnectTimeout(connectMs); http.setTimeout(readMs);
  if (!http.begin(wifi, url)) return false;
  bool ok = http.GET() == 200 && http.getString().indexOf("\"state\"") >= 0;
  http.end();
  return ok;
}

// Hotspot de téléphone : l'IP du PC Hôte n'est pas connue à l'avance. On cherche sur le /24 du réseau courant
// un hôte qui répond à /admin/api/led-state (passerelle d'abord : sur un hotspot Windows, c'est le PC Hôte). Jusqu'à ~90 s :
// c'est le secours quand la balise UDP de l'agent (checkBeacon) n'arrive pas.
static bool discoverServer() {
  IPAddress ip = WiFi.localIP(), gw = WiFi.gatewayIP();
  String path = String(WIFI_TARGETS[target].ledStateUrl);
  path = path.substring(path.indexOf('/', 8));
  String base = String(ip[0]) + "." + ip[1] + "." + ip[2] + ".";
  Serial.printf("recherche du serveur sur %s0/24 (passerelle %s)…\n", base.c_str(), gw.toString().c_str());
  for (int i = 0; i <= 254; i++) {
    int last = (i == 0) ? gw[3] : i;
    if (last == 0 || last == ip[3] || (i > 0 && last == gw[3])) continue;
    String url = "http://" + base + last + path;
    if (probe(url, 350, 500)) { serverUrl = url; Serial.printf("serveur trouvé : %s\n", url.c_str()); leds(false, false); return true; }
    if (i % 16 == 0) { ledOn = !ledOn; leds(ledOn, ledOn); }   // signe de vie pendant la recherche
  }
  leds(false, false);
  Serial.println("aucun serveur sur ce réseau");
  return false;
}

// Balise de l'agent : adoptée dès que le serveur courant ne répond plus (ou n'a pas encore répondu).
static void checkBeacon() {
  int n = beacon.parsePacket();
  if (n <= 0) return;
  char buf[600]; int len = beacon.read(buf, sizeof(buf) - 1); buf[len > 0 ? len : 0] = 0;
  if (strncmp(buf, "RA-LED ", 7) || pollFails == 0) return;   // serveur courant OK : on ne change pas
  char *save;
  for (char *url = strtok_r(buf + 7, " \r\n", &save); url; url = strtok_r(nullptr, " \r\n", &save)) {
    if (serverUrl == url) continue;
    if (probe(String(url), 400, 600)) { serverUrl = url; pollFails = discoveryFails = 0; Serial.printf("serveur annoncé par la balise : %s\n", url); return; }
  }
}

static void poll() {
  if (WiFi.status() != WL_CONNECTED) { connectWifi(); return; }
  HTTPClient http;
  http.setConnectTimeout(300); http.setTimeout(400);
  int code = -1;
  if (http.begin(wifi, serverUrl)) code = http.GET();
  if (code == 200) {
    pollFails = 0;
    JsonDocument doc;
    if (!deserializeJson(doc, http.getString())) {
      State s = parse(doc["state"] | "off");
      if (s != state) { state = s; stateSince = millis(); blinksDone = 0; ledOn = false; lastToggle = 0;
                        Serial.printf("état → %s\n", doc["state"] | "off"); }
    }
  } else if (++pollFails >= 6) {                 // ~3 s sans réponse : le serveur n'est pas à cette adresse
    http.end();
    state = OFF;
    checkBeacon();                                 // la balise a-t-elle donné l'adresse entre-temps ?
    if (pollFails == 0) return;
    pollFails = 0;
    Serial.printf("serveur %s injoignable (code %d)\n", serverUrl.c_str(), code);
    if (discoverServer()) discoveryFails = 0;
    else if (++discoveryFails >= 2) { Serial.println("pas de serveur ici : réseau suivant"); connectWifi(target + 1); }
    return;
  }
  http.end();
}

void setup() {
  Serial.begin(115200);
  pinMode(GREEN_PIN, OUTPUT); pinMode(RED_PIN, OUTPUT);
#ifdef STATUS_PIN
  pinMode(STATUS_PIN, OUTPUT);
#endif
  leds(false, false);
  connectWifi();
#ifdef PROBE_TEST   // diagnostic : mesure réelle des délais de sonde
  for (int ms : {150, 300, 600, 1000}) {
    for (const char *h : {PROBE_TEST, "10.242.129.200"}) {
      String url = String("http://") + h + "/admin/api/led-state";
      unsigned long t0 = millis(); bool ok = probe(url, ms, ms * 2);
      Serial.printf("sonde %-16s connect %4d ms → %s en %lu ms\n", h, ms, ok ? "OK" : "KO", millis() - t0);
    }
  }
#endif
}

void loop() {
  unsigned long now = millis();
  if (now - lastPoll >= LED_POLL_MS) { lastPoll = now; poll(); }
  if (pollFails > 0) checkBeacon();
  switch (state) {
    case OFF:         leds(false, false); break;
    case GREEN_SOLID: leds(true, false);  break;
    case RED_SOLID:   leds(false, true);  break;
    case GREEN_BLINK:
      if (now - lastToggle >= 500) { lastToggle = now; ledOn = !ledOn; leds(ledOn, false); }
      break;
    case RED_BLINK:   // 10 clignotements (250 ms allumé / 250 ms éteint) puis extinction locale
      if (blinksDone >= 10) { leds(false, false); break; }
      if (now - lastToggle >= 250) { lastToggle = now; ledOn = !ledOn; leds(false, ledOn); if (!ledOn) blinksDone++; }
      break;
  }
  delay(5);
}
