---
mode: agent
description: Chargement réel d'une cible sandbox, avec contrôles avant et après
---
1. `sfdata prereqs --target <cible>` ; stopper si KO.
2. `sfdata init --source <src> --target <cible> --objects <clés> --dry-run [--limit N]` ; résumer volumes et erreurs.
3. Annoncer à l'humain : org cible, objets, volumes, effets de bord (triggers, flows, e-mails). **Attendre sa confirmation explicite.**
4. Relancer sans `--dry-run`, puis `sfdata verify` ; lire les rapports `state/run_<ts>_*.csv` pour les erreurs.
5. Vérifier l'anonymisation des emails (`.invalid`) et rendre compte (compteurs uniquement).
