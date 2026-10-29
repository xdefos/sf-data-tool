# sf-data-tool — sauvegarde et migration de données Salesforce

Outil CLI Python qui **extrait** des données d'une org Salesforce (ou d'un snapshot), les **transforme** selon des
mappings YAML (un par objet) et les **charge** dans une autre org par upsert sur identifiant externe. Il sert à
sauvegarder la SPA, la restaurer, ou alimenter un autre environnement. Exécuté à la demande, sans service.
Configuré pour les objets SPA (`mappings/`), adaptable à d'autres modèles (`init-mappings`).

## 1. Garde-fous

- La **source n'est jamais écrite**.
- Écriture refusée sur une cible **non-sandbox**. Seul `--dry-run` y est permis. Déverrouillage prod : `--allow-prod`
  + `--confirm <alias>` + variable `SFDATA_ALLOW_PROD=1` (les trois).
- `purge` : sandbox uniquement, plan seul par défaut, `--execute --confirm <alias>` pour supprimer.
- Anonymisation des emails (`.invalid`) active par défaut ; `--no-anonymize` refusé sur sandbox.
- Aucun credential dans le dépôt : l'authentification est celle du CLI `sf`. Aucune org par défaut.

## 2. Installation

Python ≥ 3.10 et CLI [`sf`](https://developer.salesforce.com/tools/salesforcecli) avec un **alias par org** (`sf org list`).

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

## 3. Concepts

| Notion | Sens |
|---|---|
| **Environnement source** | `--source <alias>` (org, lecture seule) ou `--from-snapshot <dossier>` (sauvegarde) |
| **Environnement cible** | `--target <alias>`, toujours une org. L'état local est séparé par cible |
| **Mapping** | `mappings/<clé>.yaml` : champs, lookups, transcodes, périmètre. `<clé>` est la valeur de `--objects` |
| **Niveaux** | Champ `level` de chaque mapping : parents d'abord, un niveau attend le précédent |
| **Scope** | `scope_filter` (mapping) + `--objects` + `--where` (SOQL) + `--limit N` |
| **Itération** | Rejouable sans doublon (upsert). `delta` repart du watermark. `--limit` charge par tranches |
| **Snapshot** | Dossier horodaté (`backup`), utilisable comme source d'un chargement |
| **Journal** | Records **créés** par l'outil dans une cible : base de la purge |

## 4. Commandes

```bash
sfdata init-mappings --source "SPA Prod" --objects Enquete__c,Animal__c [--force]   # génère les YAML (à relire)
sfdata profile  --source "SPA Prod"                       # volumes du périmètre
sfdata prereqs  --target "SPA DEV3"                       # champs écrits présents sur la cible
sfdata backup   --source "SPA Prod" [--where ...] [--limit N] [--no-files]
sfdata init     --source "SPA Prod" --target "SPA DEV3" --dry-run     # toujours un dry-run d'abord
sfdata init     --from-snapshot <dossier> --target "SPA DEV3" [--objects enquete,animal]
sfdata delta    --source "SPA Prod" --target "SPA DEV3"
sfdata verify   --source "SPA Prod" --target "SPA DEV3" [--json]
sfdata purge    --target "SPA DEV3" --objects enquete,animal                      # plan
sfdata purge    --target "SPA DEV3" --objects ... --execute --confirm "SPA DEV3"
sfdata status   --source "SPA Prod" [--json]
```

Itérer par tranches : `init --limit 50`, vérifier, puis `--limit 500`, puis sans limite. Les upserts sont idempotents.

### Sauvegarde / restauration

`backup` écrit `snapshots/<alias>_<ts>/` : `data/<clé>.jsonl` (tous les champs lisibles), `files/<clé>/` (dernière
version des fichiers liés), `manifest.json`. Restaurer = `init --from-snapshot ... --target <alias>`.

## 5. Mapping YAML

`source_object`, `target_object`, `level`, `scope_filter`, `files` (copie les fichiers liés), `two_pass`.
Identifiant : `external_id_field` + `external_id_compute` (`prefer` = champ source, sinon `<prefix>_<Id source>`) ;
`external_id_backfill` (la cible génère la valeur), `insert_only`, `track_target_id` (objet sans identifiant externe :
insertion + `idmap` local).
`fields[]` : `source`, `target`, `transcode`, `drop_values`, `append_suffix`+`anonymize`, `constant`, `truncate_at`,
`only_if`/`unless` (`Champ` ou `Champ=valeur`). `lookups[]` : référence au parent par identifiant externe
(`prefer`, `compute`), `reflexive` (pass 2), `via_idmap` (parent sans identifiant externe), `user` (par username,
`mappings/_users.yaml`). Documenter le « pourquoi » dans `rule:`.

## 6. Pilotage par une IA

`CLAUDE.md` (Claude Code) et `.github/copilot-instructions.md` (GitHub Copilot) donnent le fonctionnement et les
règles : lecture seule et dry-run en autonomie, **toute écriture ou suppression après confirmation humaine**.
`.github/prompts/` : procédures `run-reel`, `controle-post-run`, `purge-plan`. `verify` et `status` acceptent `--json`.

## 7. Données d'exécution (hors git)

`~/.sf-data-tool/` (`SFDATA_DIR`) : `state/` (rapports `run_<ts>_<clé>.csv`, `watermarks_`, `idmap_`, `backfill_`,
`files_`, `ledger_` par alias), `snapshots/`, `logs/journal.jsonl`. Ne pas les supprimer entre deux runs d'une même
cible (idempotence, purge). Ils contiennent des données clients : ne jamais les committer.

## 8. Effets de bord et limites

Les triggers et flows de la cible s'exécutent (Tinify, e-mails, rappels, dlrs) : charger en sandbox, e-mails
désactivés. Champs d'audit (`CreatedDate`...) : permission `CreateAuditFields` requise pour les poser ; sinon ignorés.
Propriétaires : résolus par username, sinon utilisateur d'API. Lookups hors périmètre omis. Fichiers : dernière version.
