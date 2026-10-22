# PROJECT RESILIENCE-ALPHA — Workshop EPSI M2 2026 · Consortium G9

Plateforme de paiement **fictive** conteneurisée qui encaisse un incident de sécurité lancé depuis un PC distinct,
le détecte par machine learning et se rétablit **sans intervention humaine** — preuve à l'appui : une nouvelle
transaction fictive réussit après l'incident.

Dépôt : https://github.com/resilience-alpha-g-09/resilience-alpha-g9

> Toutes les données, comptes et opérations sont fictifs. Les scénarios offensifs sont limités au laboratoire du groupe
> (hotspot `RESILIENCE-G9`, réseau `192.168.137.0/24`).

## Architecture

```
PC ATTAQUANT ──HTTP──▶ edge (nginx + ModSecurity CRS, :80) ──▶ api / auth / front
                           │ logs JSON                             │ /metrics
                           ▼                                       ▼
                 Promtail ─▶ Loki ◀──────────────── Prometheus ◀─ cAdvisor
                           │                              │
                           └──▶ detector (IsolationForest, 5 s) ──▶ POST /incident
                                                              │
                blocklist nginx + SIGHUP ◀── remediator ──▶ socket-proxy (restart api/auth)
                                                              │  verify : login → POST tx → GET tx
                                                              ▼
                                               dashboard React (:3000)  ·  Grafana (127.0.0.1:3001)
```

| Service | Rôle | Réseau | Publié |
|---|---|---|---|
| `edge` | WAF (OWASP CRS, PL1) + reverse proxy, seule entrée | edge, app | **:80** |
| `front` | React/Tailwind — login, paiement, historique | app | via edge `/` |
| `api` | FastAPI — comptes, transactions ; vérifie le token auprès de `auth` | app, data | via edge `/api/` |
| `auth` | FastAPI — login (bcrypt), JWT, `/verify` | app, data | via edge `/auth/` |
| `db` | PostgreSQL 16 | data | — |
| `simulator` | trafic métier normal 3 tx/s (baseline ML, état « avant ») | app | — |
| `remediator` | watchdog + machine à états + actions (`block_ip`, `restart`, `verify`) | app, ops | — (BFF via dashboard) |
| `socket-proxy` | seul accès au démon Docker : `restart`/`kill`, rien d'autre | ops | — |
| `detector` | features Prometheus → score IsolationForest → `/incident` | ops | — |
| `dashboard` | **supervision unifiée** : état/phase/LED, tuiles, panneaux Grafana embarqués, incident, journal d'accès back-office | edge, ops | via edge **`/supervision/`** (aussi :3000) |
| `prometheus` `cadvisor` `loki` `promtail` `grafana` | observabilité, provisionnée en code ; Grafana en lecture anonyme sous `/grafana/` (embarqué dans la supervision) | ops | via edge `/grafana/` · 127.0.0.1:9090 / :3001 |
| `backoffice` | authentification à 3 étapes (identifiant → caméra → mot de passe), habilitations, état LED | app, data | via edge `/admin/` |
| agent caméra | sur le PC Hôte hors Docker : reconnaissance faciale (OpenCV) + caméra USB | — | :8090 (jeton) |
| `ollama` `assistant` | RAG local (profil `ai`, J2), interrogeable depuis le dashboard | ops, edge | 127.0.0.1:8081 · dashboard `/assistant/ask` |

Gouvernance (EBIOS RM, PCA/PRA, note NIS2/DORA) : [docs/governance/](docs/governance/).

## Démarrage — PC Hôte (Windows 10/11 + Docker Desktop/WSL2)

1. **Hotspot** : Paramètres → Réseau → Point d'accès sans fil mobile, nom `RESILIENCE-G9`. Le PC Hôte obtient `192.168.137.1`.
2. **WSL2** : dans `%UserProfile%\.wslconfig` → `[wsl2]` `memory=10GB` `processors=4`, puis `wsl --shutdown`.
3. **Pare-feu Windows** : autoriser les ports entrants TCP 80 et 3000 sur le profil du hotspot (sinon le PC Attaquant ne voit rien).
4. Dans un terminal WSL (Ubuntu) :
   ```bash
   git clone <repo> resilience-alpha && cd resilience-alpha
   cp .env.example .env            # dev : changer les mots de passe ; prod de démo : sops -d .env.enc > .env
   make up                         # build + démarrage + attente de santé (~3-5 min la première fois)
   ```
5. Vérifier — **une seule origine, tout passe par l'edge** : http://localhost/ (client, `alice@demo.local` / `DEMO_PASSWORD`) · **http://localhost/supervision/** (l'écran à projeter pendant l'incident) · http://localhost/admin/ (back-office) · http://localhost/grafana/ (lecture seule ; admin via 127.0.0.1:3001).

Déploiement reproductible via Ansible (depuis WSL) : `ansible-playbook -i ansible/inventory.ini ansible/deploy.yml` ;
reconstruction complète (PRA) : `ansible-playbook -i ansible/inventory.ini ansible/restore.yml`.

## Prouver la reprise

```bash
make e2e              # paiement → kill ra-api → reprise autonome → paiement ; affiche le MTTR
make test             # tests d'intégration API + WAF (pip install -r tests/requirements.txt)
```

Incident de sécurité depuis le **PC Attaquant** (voir [attack/README.md](attack/README.md)) :
```bash
python incident.py --target http://192.168.137.1 --mode login --rps 180 --duration 60
```
Attendu : dégradation visible → détection ML → IP bloquée à l'edge + service(s) relancé(s) → transaction de vérification ✅ → `RECOVERED` + MTTR.
Entre deux démos : `make reset-blocklist`.

## Assistant RAG (J2) — avec ou sans PC de délestage IA
- **Modèle retenu : `qwen3:8b` (8,2 Md de paramètres) + `embeddinggemma`.** Le sujet fixe un plafond de 8 Md ; l'écart de 0,2 Md (le tag `8b` est un arrondi) a été **validé exceptionnellement par les coachs le 19/10/2026**. À rappeler dans le dossier (§3.3 : cohérence modèle / matériel) : le modèle tourne sur le PC de délestage IA, pas sur le PC Hôte.
- Ollama sur le PC Hôte (repli, 16 Go partagés avec la stack) : `make ai-up` — préférer alors un modèle plus petit (`LLM_MODEL=qwen3:4b`).
- Ollama sur un **PC IA dédié** (recommandé pour un 7-8B) : sur ce PC, `OLLAMA_HOST=0.0.0.0 ollama serve` + `ollama pull qwen3:8b` + `ollama pull embeddinggemma` ; sur le PC Hôte, dans `.env` : `OLLAMA_URL=http://192.168.137.<ip>:11434`, puis `docker compose --profile ai up -d assistant`. Ollama n'ayant aucune authentification, ne l'exposer qu'une fois sur le hotspot du groupe, jamais sur le Wi-Fi du campus. Test : `curl -s localhost:8081/ask -H 'Content-Type: application/json' -d '{"question":"quelle est la procédure de reprise de l'API ?"}'`.
- **Dans le dashboard** (:3000) : panneau « Assistant opérateur » en bas de page, avec questions suggérées (dont une liée à l'incident en cours). Le nginx du dashboard n'expose que `POST /assistant/ask` (6 req/min par IP, rafale de 3, corps ≤ 4 Ko) et `GET /assistant/health` ; `/reindex` reste inaccessible. Si le profil `ai` n'est pas lancé, le panneau affiche « hors ligne » et le reste du dashboard fonctionne normalement.
- Latence mesurée sur le Mac dev (Ollama local, `qwen3:8b`) : 19 à 23 s par réponse ; à remesurer sur le PC IA.


## Back-office à authentification renforcée (consigne additionnelle 1)
`http://<hôte>/admin/login` : identifiant → **caméra USB du PC Hôte** (reconnaissance faciale Haar + LBPH, agent `faceauth/agent/` hors Docker) → mot de passe. Visage non reconnu : identifiant effacé, caméra éteinte, LED rouge 30 s ; deux échecs → blocage 5 min. Habilitations (opérateurs, rôles, visages, déblocage, journal) dans le back-office. LED bicolore sur ESP32/ESP8266 en WiFi (`iot/esp-led/`, PlatformIO) pilotée par `GET /admin/api/led-state`. Tests sans caméra : profil `mockagent`.

## Détection ML (J2)

```bash
pip install -r ml/requirements.txt
make collect MINUTES=60    # baseline normale (simulator seul, aucun incident) → ml/data/baseline.csv
make train                 # IsolationForest + seuil → ml/model/, puis redémarre le détecteur
```
Sans modèle, le détecteur tourne en observation (features loguées, aucun déclenchement). `DETECTOR_DRY_RUN=true` pour tester sans remédier.

### Index de l'assistant

L'index Chroma est **en mémoire**, reconstruit à chaque démarrage (~5 s pour `docs/`) et nommé d'après le modèle d'embedding :
changer `EMBED_MODEL` ou les documents ne demande qu'un `docker compose restart assistant` (ou `POST /reindex`).
Il n'y a volontairement pas de persistance : elle dupliquait les documents à chaque redémarrage et pouvait mélanger
silencieusement les vecteurs de deux modèles de même dimension (768 pour `nomic-embed-text` comme `embeddinggemma`).

### Contraintes des modèles

- `reasoning=False` suppose un modèle qui sait raisonner (`qwen3`) et
  `langchain-ollama >= 0.3.4`. Sans cela, chaque réponse paie ~5 s de
  raisonnement produit puis jeté (mesuré : 6,0 s contre 0,8 s).
- `embeddinggemma` plafonne à 2 048 tokens ; `chunk_size=800` laisse une marge
  confortable.
- Les préfixes de tâche d'`embeddinggemma` sont appliqués par
  `PrefixedOllamaEmbeddings` : ni Ollama ni LangChain ne les ajoutent.

## Sécurité de la plateforme

- Une seule entrée publiée (edge WAF) ; DB, secrets, Prometheus, Grafana, remédiateur jamais exposés au hotspot.
- Conteneurs non-root, `read_only`, `cap_drop ALL`, `no-new-privileges`, 4 réseaux séparés (`app` et `data` sans sortie).
- Le remédiateur n'a **pas** le socket Docker : il passe par `docker-socket-proxy` (restart/kill uniquement) — et le dashboard ne l'atteint qu'en GET.
- Secrets : `.env` jamais commité ; chiffré avec **SOPS + age** → `.env.enc`. `make secrets-scan` (gitleaks) avant chaque dépôt.
- L'assistant RAG n'a aucun droit sur la plateforme (lecture de `docs/` uniquement).

## Limites connues
- cAdvisor ne voit pas les conteneurs sous Docker Desktop macOS (cgroup racine seulement) ; à revérifier sur le PC Hôte WSL2. Les features ML utilisent les métriques `process_*` des services, indépendantes de cAdvisor.
- `restart: unless-stopped` relance un conteneur qui *crashe* avant même le remédiateur (défense en profondeur) ; un conteneur *arrêté* ou un service dégradé mais vivant ne sont repris que par la chaîne détection → remédiation. Le test `make e2e` utilise `docker stop` pour cette raison.
- Une seule instance PostgreSQL : RPO = dernière transaction validée ; `restore.yml` réinitialise les données.
- Le blocage d'IP suppose un attaquant mono-source (cas du labo) ; une attaque distribuée demanderait un rate-limiting par motif (CRS + `limit_req`).

## Arborescence

```
docker-compose.yml  Makefile  .env.example  .sops.yaml
services/   api auth front dashboard simulator detector remediator assistant
infra/      edge (nginx+CRS, blocklist) · prometheus · grafana (dashboards provisionnés) · loki · promtail
db/         init.sql (schéma + comptes fictifs)
ansible/    deploy.yml · restore.yml
ml/         features.py · collect.py · train.py · model/
attack/     incident.py (PC Attaquant, labo uniquement)
docs/       governance (EBIOS RM, PCA/PRA, NIS2/DORA)
tests/      e2e_loop.sh · test_api.py
```

## Équipe & conventions

Commits préfixés `[DEV]` `[INFRA]` `[IA]` `[CYBER]` `[GOV]`. Propriétaires : A — DEV/remédiation · B — INFRA/PCA-PRA · C — IA/DATA · D — CYBER/EBIOS/NIS2-DORA.
