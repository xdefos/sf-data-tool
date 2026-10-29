# sf-data-tool — instructions pour GitHub Copilot

Outil CLI Python de sauvegarde / migration de données Salesforce. Lire `README.md`.


## Règles NON NÉGOCIABLES
- **Aucune écriture sur une org de production.** Garde-fou `_guard_write_target` dans `sfdata/cli.py` : ne jamais le contourner
  ni poser de ma propre initiative `--allow-prod`, `--confirm` ou `SFDATA_ALLOW_PROD`.
- Autonome : `status`, `profile`, `prereqs`, `backup` (lecture), `init|delta --dry-run`, `verify`, `purge` (plan), `pytest`.
- **Demander avant** : `init|delta` sans `--dry-run`, `purge --execute`, toute écriture directe via `sf`, `git push`.
  Un accord vaut pour une action, pas pour la suivante. Lister org, objets, volumes avant de demander.
- Ne pas supprimer ni versionner `~/.sf-data-tool/` (idmap, ledger, watermarks) ni les snapshots (données clients).
- Après un run réel : `verify`, vérifier l'anonymisation des emails (`.invalid`).
- Ne pas coller de données de personnes dans les réponses : compteurs et noms de champs.

## Conventions
- Réponses, commentaires et docs en **français**, densité de commentaires du code existant. Pronoms neutres (they/them).
- Questions à l'utilisateur : `AskUserQuestion`, une à la fois, réponses proposées (recommandation en premier).
- Chaque changement de comportement a un test (`tests/`). Le `rule:` d'un mapping documente le « pourquoi ».
- Le YAML généré par `init-mappings` est un point de départ à relire (transcodes, `scope_filter`, `files`, identifiants).

## Code
`cli.py` (commandes, garde-fous) · `runner.py` (moteur, niveaux, pass 2, fichiers) · `extract.py`/`snapshot.py` (sources) ·
`transform.py` (pur) · `load.py` (variantes d'écriture) · `purge.py`/`ledger.py` · `state.py` · `mapgen.py`/`graph.py` ·
`users.py` · `verify.py` · `prereqs.py`.
