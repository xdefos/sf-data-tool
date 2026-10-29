"""Journal des records CREES par l'outil dans une org cible (base de la purge).

Un fichier JSONL par cible (`state/ledger_<alias>.jsonl`), append-only : une ligne par record cree
(`created=True`). Un upsert qui met a jour un record deja present n'y entre jamais, donc n'est jamais
purgeable. Une purge ajoute des marqueurs `{"action": "purged"}` au lieu de reecrire le fichier.
Les champs d'audit (CreatedDate...) etant ecrits par l'outil, seul ce journal permet de retrouver ce
que l'outil a cree.
"""

from __future__ import annotations

import json
from pathlib import Path

from sfdata.config import STATE_DIR
from sfdata.load import LoadResult
from sfdata.state import safe_alias


def ledger_path(target_alias: str, state_dir: Path = STATE_DIR) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / f"ledger_{safe_alias(target_alias)}.jsonl"


def entries_from_results(key, sobject, results: list[LoadResult], run_ts, origin="run") -> list[dict]:
    return [
        {"run_ts": run_ts, "object": key, "sobject": sobject, "target_id": r.target_id,
         "external_id": r.external_id if r.external_id != r.target_id else None, "origin": origin}
        for r in results if r.success and r.created is True and r.target_id
    ]


def append_entries(target_alias: str, entries: list[dict], state_dir: Path = STATE_DIR) -> int:
    if not entries:
        return 0
    with ledger_path(target_alias, state_dir).open("a", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")
    return len(entries)


def record_created(target_alias, key, sobject, results, run_ts, state_dir=STATE_DIR) -> int:
    return append_entries(target_alias, entries_from_results(key, sobject, results, run_ts), state_dir)


def load_entries(target_alias: str, state_dir: Path = STATE_DIR) -> list[dict]:
    path = ledger_path(target_alias, state_dir)
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def live_entries(entries: list[dict]) -> list[dict]:
    purged = {e["target_id"] for e in entries if e.get("action") == "purged"}
    return [e for e in entries if e.get("action") != "purged" and e["target_id"] not in purged]
