// Copier en config.h (ignoré par Git). Réseaux essayés dans l'ordre, ~10 s chacun, en boucle :
// le hotspot du PC Hôte d'abord, puis un réseau de secours (labo, poste d'un coéquipier…). Chaque réseau a son serveur.
#pragma once
struct WifiTarget { const char *ssid; const char *password; const char *ledStateUrl; };
static const WifiTarget WIFI_TARGETS[] = {
  { "RESILIENCE-G9", "mot-de-passe-du-hotspot", "http://192.168.137.1/admin/api/led-state" },
  { "WIFI_LABO",     "mot-de-passe-du-labo",    "http://10.0.3.66/admin/api/led-state" },
};
#define WIFI_TRY_MS 10000   // durée d'essai par réseau avant de passer au suivant
