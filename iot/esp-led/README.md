# LED bicolore — ESP32 / ESP8266 (PlatformIO, CLion)

Signale l'état de l'authentification du back-office (consigne additionnelle 1) :

| État serveur | LED | Déclencheur |
|---|---|---|
| `off` | éteinte | défaut, fin des temporisations |
| `green_blink` | verte clignotante (0,5 s) | visage reconnu, en attente du mot de passe |
| `green_solid` | verte fixe 30 s | mot de passe validé, back-office ouvert |
| `red_blink` | rouge, 10 clignotements | mot de passe refusé → retour à l'identification |
| `red_solid` | rouge fixe 30 s | visage non reconnu → identifiant effacé, caméra éteinte |

**Le hotspot doit émettre en 2,4 GHz** (ESP32 et ESP8266 n'ont pas de radio 5 GHz : hotspot Windows → Paramètres → Point d'accès mobile → Bande 2,4 GHz ; hotspot iPhone → « Maximiser la compatibilité »). Au démarrage, le firmware liste sur le port série les réseaux 2,4 GHz qu'il voit et marque la cible. Le module se connecte en WiFi au hotspot `RESILIENCE-G9` et interroge `GET http://192.168.137.1/admin/api/led-state` toutes les 500 ms (route publique en lecture, via l'edge). Aucun port série nécessaire.

## Comment la carte trouve le serveur (hotspot de téléphone : IP du PC Hôte inconnue)
1. **Balise UDP** : l'agent caméra du PC Hôte (`faceauth/agent`, qui tourne nativement) diffuse chaque seconde sur le port 4210
   `RA-LED http://<ip-du-pc>/admin/api/led-state`. La carte l'adopte dès que l'adresse configurée ne répond pas (mesuré : 1,2 s après
   la connexion WiFi). `LED_BEACON=0` côté agent pour désactiver.
2. **Adresse configurée** dans `config.h` (hotspot Windows : `192.168.137.1`).
3. **Balayage du sous-réseau** (secours si l'agent ne tourne pas) : passerelle puis les 254 adresses du /24, 350 ms chacune, jusqu'à ~90 s.
4. Rien trouvé après deux balayages → **réseau suivant** de la liste, et ainsi de suite en boucle.

Sur le PC Hôte, vérifier une fois que le port 80 est joignable depuis le hotspot (pare-feu Windows, profil « réseau public ») :
depuis un téléphone sur le même hotspot, ouvrir `http://<ip-du-pc>/admin/api/led-state`.

## Matériel
La consigne demande un **ESP8266** ; le matériel disponible dans l'équipe est un **ESP32** : le firmware compile pour les deux (`esp32dev` par défaut, `nodemcuv2` pour l'ESP8266). Câblage : LED verte sur GPIO 26, rouge sur GPIO 25 (ESP32, câblage de la carte de l'équipe) — D5 / D6 sur ESP8266 — avec résistances 220–330 Ω vers GND. Une LED bicolore à cathode commune se branche de la même façon (deux anodes). Sur ESP32, la LED bleue intégrée (GPIO 2) s'allume dès qu'une des deux couleurs est allumée : les motifs (clignotement 0,5 s, 10 clignotements rapides, fixe) se lisent sans câblage externe.

## Compiler et flasher (CLion + PlatformIO)
1. CLion → *Open* → ce dossier (`iot/esp-led`) ; le plugin PlatformIO crée la configuration.
2. `cp include/config.example.h include/config.h` et renseigner les réseaux : liste **par ordre de priorité**, chacun avec son serveur (`RESILIENCE-G9` → `192.168.137.1`, puis un réseau de secours, par ex. le labo → IP du poste qui fait tourner la stack). Le firmware essaie chaque réseau 10 s (`WIFI_TRY_MS`) puis passe au suivant, en boucle ; le port série indique le réseau retenu et le serveur interrogé.
3. Cible `esp32dev` → *Upload* (ou en ligne de commande : `pio run -e esp32dev -t upload && pio device monitor`).
4. Au démarrage, les deux LED clignotent en alternance jusqu'à la connexion WiFi, puis s'éteignent.

## Tester sans la page de connexion
Depuis le back-office (admin) : *Accueil → Test LED*, ou `POST /admin/api/led/test {"state":"green_blink"}` avec une session admin.

## Validé le 22/10 (Mac, réseau du labo)
ESP32-D0WD-V3 (DevKit, port `usbserial-0001`), firmware `esp32dev` : connexion WiFi en ~4 s, interrogation du back-office ~2×/s via l'edge,
chaque changement d'état appliqué en 0,3–0,7 s, `red_blink` éteint seul après 10 clignotements. Bascule testée : hotspot absent → abandon après 10 s → réseau de secours connecté en 1 s. Hotspot de téléphone (10.242.129.0/24) : adresse du serveur adoptée via la balise 1,2 s après la connexion, états appliqués en 0,3–0,5 s. Sans PlatformIO dans CLion : `make esp-flash`.
