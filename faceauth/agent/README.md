# Agent caméra du PC Hôte — reconnaissance faciale (consigne additionnelle 1)

Reprend le prototype de Karim (`README-prototype.md`, Haar + LBPH, OpenCV) et l'expose en API locale pour le back-office.
**Tourne sur le PC Hôte, sous Windows, hors Docker** : WSL2 ne voit pas les webcams USB ; le back-office conteneurisé
l'appelle via `http://host.docker.internal:8090`.

## Installation (PC Hôte, PowerShell)
```powershell
cd faceauth\agent
py -3.12 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
$env:AGENT_TOKEN = "<même valeur que AGENT_TOKEN dans ..\..\.env>"
.\.venv\Scripts\python agent.py enroll admin      # amorçage : enrôler le visage du premier administrateur (30 captures)
.\.venv\Scripts\python agent.py serve             # API sur :8090 — laisser tourner pendant la démo
```
Pare-feu Windows : autoriser Python sur le port 8090 **pour le réseau Docker uniquement** (l'API est protégée par le jeton, mais inutile de l'exposer au hotspot).

## API
| Méthode | Route | Rôle |
|---|---|---|
| GET | `/health` | état, opérateurs enrôlés, seuil |
| POST | `/verify` `{login, timeout_s}` | ouvre la caméra ≤ 10 s, conclut après 5 analyses concordantes → `recognized` / `unknown` / `no_face` / `timeout`, puis éteint la caméra |
| POST | `/enroll` `{login, frames}` | 30 captures automatiques d'un visage unique, ré-entraînement LBPH multi-utilisateurs |
| DELETE | `/enroll/{login}` | supprime les données biométriques d'un opérateur |

Toutes les routes sauf `/health` exigent l'en-tête `X-Agent-Token`.

## Fenêtre caméra
Pendant une vérification ou un enrôlement, l'agent ouvre une fenêtre **sur l'écran du PC Hôte** (celui où tourne aussi le navigateur) : image de la webcam, cadre vert/rouge autour du visage, identifiant, compte à rebours, puis le verdict affiché une seconde avant fermeture. C'est ce que le jury voit « s'allumer ». `AGENT_WINDOW=0` pour la désactiver (serveur sans écran).

## Réglages
`FACE_THRESHOLD` (60, distance LBPH max — plus bas = plus strict), `FACE_STABILITY` (5 analyses concordantes), `CAMERA_INDEX` (0), `AGENT_WINDOW` (1), `AGENT_MOCK=1` (simulation sans caméra, `AGENT_MOCK_RESULT=recognized|unknown|timeout`, `AGENT_MOCK_DELAY=3`).

## Données
`data/<login>/*.png` et `model/` restent sur le PC Hôte, exclus de Git. Images collectées avec le consentement des opérateurs ; pas d'anti-usurpation (photo/vidéo) — limite documentée dans le dossier.

## Balise LED (ESP32)
En mode `serve`, l'agent diffuse chaque seconde en UDP broadcast (port 4210) l'URL `http://<ip-locale>/admin/api/led-state` : la carte ESP32 trouve ainsi le PC Hôte même sur un hotspot de téléphone où son IP change. Variables : `LED_BEACON=0` (désactiver), `LED_BEACON_PORT`, `LED_BEACON_PATH`.
