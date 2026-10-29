"""Etat local : watermarks (par org source), backfill / idmap / fichiers (par org cible).

Un fichier JSON par alias, sous `state/`. Deux environnements cibles generent des Id differents pour
le meme record source : l'etat est donc toujours separe par cible. Ne jamais le supprimer entre deux
runs d'une meme cible : il garantit l'idempotence.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from sfdata.config import STATE_DIR


def safe_alias(alias: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", alias)


def _path(prefix: str, alias: str, state_dir: Path) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / f"{prefix}_{safe_alias(alias)}.json"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def load_watermarks(source_alias: str, state_dir: Path = STATE_DIR) -> dict:
    return _read(_path("watermarks", source_alias, state_dir))


def get_watermark(source_alias: str, key: str, state_dir: Path = STATE_DIR) -> str | None:
    return load_watermarks(source_alias, state_dir).get(key, {}).get("watermark")


def set_watermark(source_alias: str, key: str, watermark: str, state_dir: Path = STATE_DIR) -> None:
    """A appeler seulement si l'objet n'a eu aucune erreur : sinon le meme delta est rejoue."""
    data = load_watermarks(source_alias, state_dir)
    data[key] = {"watermark": watermark, "last_run": datetime.now(timezone.utc).isoformat()}
    _write(_path("watermarks", source_alias, state_dir), data)


def _load_map(prefix: str, target_alias: str, key: str, state_dir: Path) -> dict:
    return _read(_path(prefix, target_alias, state_dir)).get(key, {})


def _merge_map(prefix: str, target_alias: str, key: str, entries: dict, state_dir: Path) -> None:
    if not entries:
        return
    path = _path(prefix, target_alias, state_dir)
    data = _read(path)
    data.setdefault(key, {}).update(entries)
    _write(path, data)


def load_backfill(target_alias, key, state_dir=STATE_DIR):
    """Id source -> ExternalId genere cote cible (objets `external_id_backfill`)."""
    return _load_map("backfill", target_alias, key, state_dir)


def set_backfill_ids(target_alias, key, entries, state_dir=STATE_DIR):
    _merge_map("backfill", target_alias, key, entries, state_dir)


def load_id_map(target_alias, key, state_dir=STATE_DIR):
    """Id source -> Id cible reel (objets `track_target_id`, sans identifiant externe)."""
    return _load_map("idmap", target_alias, key, state_dir)


def set_id_map(target_alias, key, entries, state_dir=STATE_DIR):
    _merge_map("idmap", target_alias, key, entries, state_dir)


def load_files_map(target_alias, key, state_dir=STATE_DIR):
    """ContentDocument source -> ContentDocument cible (fichiers deja copies)."""
    return _load_map("files", target_alias, key, state_dir)


def set_files_map(target_alias, key, entries, state_dir=STATE_DIR):
    _merge_map("files", target_alias, key, entries, state_dir)


def max_system_modstamp(records: list[dict], field: str = "SystemModstamp") -> str | None:
    values = [r[field] for r in records if r.get(field)]
    return max(values) if values else None


def forget_purged(target_alias, key, target_ids: set, external_ids: set, source_alias=None, state_dir=STATE_DIR):
    """Oublie l'etat local des records purges pour qu'un rejeu les recree (idmap, backfill, watermark)."""
    for prefix, doomed in (("idmap", target_ids), ("backfill", external_ids), ("files", target_ids)):
        path = _path(prefix, target_alias, state_dir)
        if not path.exists() or not doomed:
            continue
        data = _read(path)
        data[key] = {k: v for k, v in data.get(key, {}).items() if v not in doomed}
        _write(path, data)
    if source_alias and target_ids:
        wm = load_watermarks(source_alias, state_dir)
        if wm.pop(key, None) is not None:
            _write(_path("watermarks", source_alias, state_dir), wm)
