# Analyse de risques — EBIOS Risk Manager (condensée)

## Atelier 1 — Cadrage et socle de sécurité
- **Missions** : traiter des paiements fictifs pour des clients et un marchand ; maintenir la disponibilité et l'intégrité des soldes.
- **Valeurs métier** : V1 exécution des paiements · V2 authentification des clients · V3 intégrité des soldes et de l'historique · V4 supervision/traçabilité.
- **Biens supports** : edge/WAF, api, auth, PostgreSQL, Docker/WSL2 sur le PC Hôte, hotspot, chaîne d'observabilité, remédiateur, secrets.
- **Événements redoutés** : ER1 paiement indisponible (V1, gravité 3) · ER2 usurpation de compte (V2, g3) · ER3 soldes altérés (V3, g4) · ER4 perte de traçabilité (V4, g2) · ER5 exfiltration de données clients (V2/V3, g4).
- **Socle** : conteneurs durcis, réseaux séparés, WAF CRS, secrets chiffrés, journaux centralisés (voir README « Sécurité »).

## Atelier 2 — Sources de risque / objectifs visés
| Source | Objectif | Pertinence |
|---|---|---|
| Attaquant opportuniste externe | rendre le paiement indisponible (DDoS applicatif), bourrage d'identifiants | élevée (scénario démontré) |
| Attaquant ciblé | injection applicative → données | moyenne (WAF démontré) |
| Erreur d'exploitation | mauvaise config → panne | moyenne (IaC + restore.yml) |

## Atelier 3 — Scénarios stratégiques
SS1 : saturation de `/auth/login` depuis l'extérieur → auth saturé → api en 503 → ER1.
SS2 : injection via `/api/*` → ER3/ER5 (bloqué par CRS, journalisé).
SS3 : compromission du remédiateur (droits élevés) → ER1/ER3 — traité par le socket-proxy et l'isolement réseau.
SS4 : accès physique ou usurpation d'identité sur le back-office → ER2/ER3 — traité par l'authentification à trois étapes (identifiant, visage, mot de passe), le blocage après deux échecs et le journal (consigne additionnelle 1).

## Atelier 4 — Scénarios opérationnels (SS1, démontré)
Reconnaissance (edge :80) → flood login (bcrypt CPU) → timeouts `auth /verify` → `api` 503 → échecs des paiements simulés → détection ML (p95, 5xx, auth_rps, auth_401_ratio) → blocage IP + restart → vérification.
Vraisemblance : 4/4 (trivial à exécuter). Gravité initiale : 3. Après mesures : vraisemblance 4, gravité 1 (indisponibilité < 40 s, aucune perte de données).

## Atelier 5 — Traitement du risque
| Risque | Mesure | État | Risque résiduel |
|---|---|---|---|
| SS1 indisponibilité | détection ML + blocage automatique + redémarrage + vérification | démontré | attaque distribuée (plusieurs IP) non couverte |
| SS2 injection | WAF CRS PL1, requêtes paramétrées, journaux | démontré (403) | contournements PL1 |
| SS3 remédiateur | socket-proxy restart-only, réseau ops, GET-only depuis le dashboard | en place | compromission de l'hôte Docker |
| Perte DB | redéploiement `restore.yml` | partiel | RPO = dernière transaction (pas de réplication) |

> À relier aux mesures réelles : chaque ligne renvoie à un fichier du repo (compose, nginx, remediator). Un rapport générique ne suffit pas (sujet §3.5).
