# RESILIENCE-ALPHA — raccourcis d'exploitation. `make help` pour la liste.
COMPOSE ?= docker compose
SHELL := /bin/bash

.PHONY: help env up down reset reset-blocklist logs ps build wait test e2e chaos incident-local collect train ai-up assistant-up lint secrets-scan fp-check

help: ## Affiche cette aide
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

env: ## Crée .env depuis .env.example s'il n'existe pas (dev local uniquement — en prod : sops -d .env.enc > .env)
	@test -f .env || (cp .env.example .env && echo ".env créé depuis .env.example — CHANGER les mots de passe avant toute démo")

up: env ## Construit et démarre toute la plateforme, puis attend qu'elle soit saine
	$(COMPOSE) up -d --build --remove-orphans
	$(MAKE) wait

ai-up: env ## Ollama local + assistant RAG (profil ai) ; modèles pris dans .env (LLM_MODEL, EMBED_MODEL). PC IA distant : voir README
	$(COMPOSE) --profile ai up -d --build --force-recreate assistant ollama
	set -a && . ./.env && set +a && $(COMPOSE) exec ollama ollama pull $$LLM_MODEL && $(COMPOSE) exec ollama ollama pull $$EMBED_MODEL

assistant-up: env ## Assistant RAG seul, contre l'Ollama du PC IA (OLLAMA_URL dans .env)
	$(COMPOSE) --profile ai up -d --build --force-recreate assistant

wait: ## Attend edge + remédiateur
	@for i in $$(seq 1 40); do \
	  if curl -fsS http://127.0.0.1/healthz >/dev/null 2>&1 && curl -fsS http://127.0.0.1:3000/bff/health >/dev/null 2>&1; then \
	    echo "✅ plateforme en ligne — front http://127.0.0.1/  dashboard http://127.0.0.1:3000/  grafana http://127.0.0.1:3001/"; exit 0; fi; \
	  sleep 3; done; echo "❌ la plateforme ne répond pas — make logs"; exit 1

down: ## Arrête la plateforme (conserve les données)
	$(COMPOSE) --profile ai down --remove-orphans

reset: ## Remise à zéro complète : données, blocklist, puis redémarrage
	$(COMPOSE) --profile ai down -v --remove-orphans
	$(MAKE) reset-blocklist
	$(MAKE) up

reset-blocklist: ## Remise à zéro entre deux démos : blocages et étranglements (mémoire du remédiateur + fichiers + rechargement de l'edge)
	@$(COMPOSE) exec -T remediator python -c "import httpx;print(httpx.post('http://127.0.0.1:8000/admin/reset',timeout=10).json())" 2>/dev/null \
	  || { printf '# Géré par le remédiateur — une ligne "deny <ip>;" par source bloquée. Vidé par `make reset`.\n' > infra/edge/blocklist/blocklist.conf; \
	       printf '# Géré par le remédiateur — une ligne "<ip> 1;" par source étranglée. Vidé par `make reset-blocklist`.\n' > infra/edge/blocklist/throttle.conf; \
	       docker kill -s HUP ra-edge >/dev/null 2>&1; echo "fichiers vidés, edge rechargé (remédiateur non démarré)"; }

logs: ## Logs de tous les services (ou SVC=api)
	$(COMPOSE) logs -f --tail=100 $(SVC)

ps: ## État des conteneurs
	$(COMPOSE) ps

build: ## Reconstruit les images sans démarrer
	$(COMPOSE) build

e2e: ## Boucle de reprise J1 : paiement → kill api → reprise autonome → paiement
	./tests/e2e_loop.sh

chaos: ## Chaos engineering : RUNS pannes en série (stop/pause api-auth, kill db au run DB_RUN) → tests/out/chaos-*.csv
	python3 tests/chaos.py --runs $${RUNS:-10} --db-run $${DB_RUN:-5} $${SCENARIOS:+--scenarios $$SCENARIOS}

test: ## Tests d'intégration API/WAF (stack démarrée)
	python3 -m pytest tests/ -q

incident-local: ## Lance le scénario d'incident depuis CE poste (test sans PC Attaquant)
	python3 attack/incident.py --target http://127.0.0.1 --mode login --rps 120 --duration 40

# collect/train tournent dans l'image du détecteur (mêmes versions de scikit-learn, aucun Python requis sur l'hôte)
ML_RUN = $(COMPOSE) run --rm --no-deps -v "$(CURDIR)/ml:/ml" -w /ml --entrypoint python detector

collect: ## Collecte la baseline ML (MINUTES=60, LABEL=normal|incident) → ml/data/baseline.csv
	$(ML_RUN) collect.py --prometheus http://prometheus:9090 --minutes $${MINUTES:-60} --label $${LABEL:-normal}

train: ## Entraîne l'IsolationForest (CONTAMINATION=0.01) et recharge le détecteur
	$(ML_RUN) train.py --contamination $${CONTAMINATION:-0.01}
	$(COMPOSE) restart detector

fp-check: ## Faux positifs du détecteur sur MINUTES de trafic normal (défaut 30) : fenêtres anormales, alertes, marge au seuil
	$(ML_RUN) fp_check.py --minutes $${MINUTES:-30} --machine "$$(hostname)"

secrets-scan: ## Vérifie qu'aucun secret n'est en clair (gitleaks requis)
	gitleaks detect --source . --no-banner

lint: ## Valide le compose et la syntaxe Python
	$(COMPOSE) config -q && python3 -m py_compile services/*/app/main.py services/*/main.py ml/*.py attack/*.py && echo "lint OK"

esp-flash: ## Compile et flashe la LED ESP32 (iot/esp-led, config.h requis) — PIO=~/.pio-venv/bin/pio si PlatformIO n'est pas dans le PATH
	@test -f iot/esp-led/include/config.h || { echo "copier iot/esp-led/include/config.example.h en config.h et renseigner le WiFi"; exit 1; }
	cd iot/esp-led && $${PIO:-pio} run -e esp32dev -t upload $(if $(PORT),--upload-port $(PORT),)

esp-monitor: ## Moniteur série de l'ESP32 (115200 bauds)
	cd iot/esp-led && $${PIO:-pio} device monitor -b 115200 $(if $(PORT),-p $(PORT),)

# Cibles de livraison (dossier, slides, archive) : fichier interne, absent du dépôt public.
-include livraison.mk
