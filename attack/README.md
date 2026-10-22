# Scénario d'incident — PC Attaquant

Générateur de charge applicative contre **notre** plateforme fictive, exécuté depuis le PC Attaquant du groupe.
Périmètre strict (sujet §3.4 et §9) : uniquement le PC Hôte du consortium sur le hotspot `RESILIENCE-G9`.
Le script refuse toute cible hors `192.168.137.0/24` / localhost. Si le hotspot est un téléphone (autre sous-réseau), on l'autorise explicitement avec `--lab-net <CIDR privé>`.

## Prérequis (PC Attaquant)
```bash
python3 -m venv .venv && source .venv/bin/activate   # Windows : .venv\Scripts\activate
pip install httpx==0.28.1
```

## Lancer l'incident principal
```bash
# saturation de l'authentification (bcrypt côté serveur) puis de l'API : paiements en échec, p95 et 5xx qui montent
python incident.py --target http://192.168.137.1 --mode login --rps 60 --duration 60
```
Hotspot de téléphone (sous-réseau du PC Hôte relevé avec `ipconfig` / `ifconfig`, ex. `10.242.129.7`) :
```bash
python incident.py --target http://10.242.129.7 --lab-net 10.242.129.0/24 --mode login --rps 60 --duration 60
```
**Concurrence : 60, pas plus de ~100.** Le PC Hôte tourne sous Docker Desktop, dont le relais de ports (userspace) s'effondre vers 150–300 connexions simultanées : plus rien n'arrive à l'edge, ni l'attaque ni la supervision, et il faut redémarrer Docker Desktop. À 60, l'authentification (bcrypt) sature déjà : mesuré le 22/10, détection ML à 11 s, étranglement, RECOVERED, MTTR 3,2 s.
Variantes : `--mode api` (POST /api/transactions sans token valide), `--mode mixed`.
Le script affiche chaque seconde : requêtes envoyées, codes HTTP, latence moyenne — et s'arrête seul après `--duration` secondes
ou dès que l'edge répond 403 (= notre IP a été bloquée par le remédiateur) ou une rafale de 429 (= source étranglée : sous Docker Desktop, toutes les sources sont vues comme la passerelle NAT, le remédiateur étrangle au lieu de bloquer). Dans les deux cas c'est le signal attendu.

## Ce qu'on doit observer (page de supervision `http://<pc-hôte>/supervision/`)
1. 5–10 s : latence p95 ↑, erreurs 5xx ↑, succès des paiements simulés ↓
2. 10–15 s : `detector` → score sous le seuil sur 2 fenêtres → incident transmis au remédiateur
3. 15–20 s : IP source bloquée à l'edge (403 ici), service(s) redémarré(s) si nécessaire
4. 20–35 s : transaction de vérification OK → état RECOVERED, MTTR affiché

Remise à zéro entre deux démos : `make reset-blocklist` sur le PC Hôte.
