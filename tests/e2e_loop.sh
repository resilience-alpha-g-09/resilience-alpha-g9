#!/usr/bin/env bash
# Boucle de reprise de bout en bout (J1) : un paiement passe → on tue l'API → le remédiateur la relance seul →
# un nouveau paiement passe. Échoue si une action humaine serait nécessaire ou si la reprise dépasse MAX_SECONDS.
set -euo pipefail
EDGE=${EDGE:-http://127.0.0.1}
BFF=${BFF:-http://127.0.0.1:3000/bff}
MAX_SECONDS=${MAX_SECONDS:-60}
EMAIL=${EMAIL:-alice@demo.local}
PASSWORD=${PASSWORD:-${DEMO_PASSWORD:-demo1234}}

say() { printf '\033[1;34m[e2e]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[e2e] ÉCHEC :\033[0m %s\n' "$*"; exit 1; }

login() { curl -fsS "$EDGE/auth/login" -H 'Content-Type: application/json' -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])'; }
pay() { curl -sS -o /dev/null -w '%{http_code}' "$EDGE/api/transactions" -H "Authorization: Bearer $1" -H 'Content-Type: application/json' -d '{"to_account":10,"amount":"2.00"}'; }
state() { curl -fsS "$BFF/state" | python3 -c 'import sys,json;s=json.load(sys.stdin);print(s["state_name"])'; }

say "1/5 santé edge"; curl -fsS "$EDGE/healthz" >/dev/null || fail "edge injoignable sur $EDGE"
TOKEN=$(login) || fail "login impossible"
say "2/5 paiement avant incident : HTTP $(pay "$TOKEN")"
[ "$(pay "$TOKEN")" = "201" ] || fail "le paiement nominal ne passe pas"

say "3/5 incident : arrêt du conteneur ra-api (docker stop : la politique restart de Docker ne le relancera pas)"
docker stop -t 0 ra-api >/dev/null
T0=$(date +%s)

say "4/5 attente d'une reprise autonome (max ${MAX_SECONDS}s)…"
while :; do
  S=$(state 2>/dev/null || echo "?")
  NOW=$(( $(date +%s) - T0 ))
  printf '   t+%3ds  état=%s\n' "$NOW" "$S"
  [ "$S" = "RECOVERED" ] && break
  [ "$S" = "FAILED" ] && fail "le remédiateur a déclaré l'échec"
  [ "$NOW" -ge "$MAX_SECONDS" ] && fail "pas de reprise en ${MAX_SECONDS}s (état $S)"
  sleep 2
done
MTTR=$(curl -fsS "$BFF/state" | python3 -c 'import sys,json;print(json.load(sys.stdin)["last_incident"]["mttr_seconds"])')

TOKEN=$(login) || fail "login impossible après reprise"
CODE=$(pay "$TOKEN")
say "5/5 paiement après reprise : HTTP $CODE"
[ "$CODE" = "201" ] || fail "le paiement ne passe pas après la reprise"
say "OK — reprise autonome en ${MTTR}s, nouvelle transaction réussie ✅"
