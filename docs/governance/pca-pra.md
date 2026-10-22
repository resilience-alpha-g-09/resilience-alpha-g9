# PCA / PRA — plateforme de paiement fictive

## Objectifs
| Service | RTO cible | RPO cible | Mesuré (voir runbooks/timings.md) |
|---|---|---|---|
| Paiement (api+auth+db) | 60 s (automatique) | dernière transaction validée | MTTR e2e : __ s · incident login : __ s |
| Supervision | 5 min (manuel) | 72 h de rétention | — |

## Scénarios couverts
| Scénario | Réponse | Automatique ? | Responsable |
|---|---|---|---|
| Service applicatif KO (api/auth) | watchdog → restart → vérification | oui | remédiateur |
| Saturation depuis une source externe | ML → blocage IP → restart si besoin → vérification | oui | remédiateur |
| Base de données KO | restart db (manuel), puis watchdog relance api/auth | partiel | B (INFRA) |
| PC Hôte perdu | redéploiement complet sur un autre poste : `git clone` + `sops -d` + `ansible restore.yml` | manuel, ~10 min | B (INFRA) |

## Rôles pendant un incident (soutenance)
- **Observateur** (A) : commente le dashboard, ne touche à rien.
- **Astreinte** (B) : n'intervient que si l'état passe à FAILED, après la démo.
- **Communication** (D) : qualifie l'incident (disponibilité vs compromission), déclenche la notification (DORA art. 19 si réel).

## Restauration
1. Automatique : voir `docs/runbooks/reprise-api.md`.
2. Reconstruction : `ansible/restore.yml` (données réinitialisées → RPO non tenu : à documenter comme limite).
3. Tests : `make e2e` à chaque changement ; exercice complet (restore) une fois par jour de sprint.

## Risques résiduels après reprise
Attaque distribuée (plusieurs IP) · perte de la DB sans réplication · secret JWT partagé · hotspot sans TLS · dépendance à un seul PC Hôte.
