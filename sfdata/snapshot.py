"""Sauvegarde (snapshot) : un dossier horodate qui peut servir de SOURCE a un chargement.

    snapshots/<alias>_<ts>/manifest.json
                            data/<cle>.jsonl      (tous les champs lisibles + les chemins utiles aux mappings)
                            files/<cle>/<Id>, files/<cle>/index.json

Un snapshot se charge comme une org source (`--from-snapshot`), sans toucher la source : sauvegarde,
restauration, et chargement vers un autre environnement a partir d'un meme instantane.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sfdata.auth import describe_org
from sfdata.config import SNAPSHOT_DIR, ObjectMapping
from sfdata.extract import source_fields
from sfdata.files import fetch_files, write_index
from sfdata.state import safe_alias

_SKIP_TYPES = {"base64", "address", "location", "encryptedstring"}


def readable_fields(sf, sobject: str) -> list[str]:
    d = getattr(sf, sobject).describe()
    return sorted(f["name"] for f in d["fields"] if f["type"] not in _SKIP_TYPES and not f["name"].startswith("Jigsaw"))


def snapshot_soql(sf, mapping: ObjectMapping, where=None, limit=None) -> str:
    fields = set(readable_fields(sf, mapping.source_object)) | source_fields(mapping)
    soql = f"SELECT {', '.join(sorted(fields))} FROM {mapping.source_object}"
    conds = [f"({x})" for x in (mapping.scope_filter, where) if x]
    if conds:
        soql += " WHERE " + " AND ".join(conds)
    soql += " ORDER BY Id"
    return soql + (f" LIMIT {int(limit)}" if limit else "")


def create_snapshot(sf, alias: str, mappings: list[ObjectMapping], where=None, limit=None, with_files=True,
                    out_root: Path = SNAPSHOT_DIR) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = out_root / f"{safe_alias(alias)}_{ts}"
    (out / "data").mkdir(parents=True)
    manifest = {"created": ts, "org": describe_org(alias), "objects": {}, "files": {}}
    for m in mappings:
        recs = sf.query_all(snapshot_soql(sf, m, where, limit)).get("records", [])
        with (out / "data" / f"{m.key}.jsonl").open("w", encoding="utf-8") as fh:
            for r in recs:
                r.pop("attributes", None)
                for v in r.values():
                    if isinstance(v, dict):
                        v.pop("attributes", None)
                fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
        manifest["objects"][m.key] = len(recs)
        print(f"[{m.key}] {len(recs)} record(s)")
        if with_files and m.files and recs:
            items = fetch_files(sf, [r["Id"] for r in recs], dest=out / "files" / m.key)
            write_index(out / "files" / m.key, items)
            manifest["files"][m.key] = len(items)
            print(f"[{m.key}] {len(items)} fichier(s)")
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return out


class SnapshotSource:
    """Source = un dossier de snapshot (lecture seule). `watermark`/`where` ne s'appliquent pas."""

    def __init__(self, path: Path):
        self.path = Path(path)
        if not (self.path / "manifest.json").exists():
            raise FileNotFoundError(f"Snapshot introuvable : {self.path}")
        self.manifest = json.loads((self.path / "manifest.json").read_text(encoding="utf-8"))
        self.alias, self.is_snapshot = f"snapshot:{self.path.name}", True

    def records(self, mapping, watermark=None, where=None, limit=None):
        if watermark or where:
            raise ValueError("--where et le mode delta ne s'appliquent pas a un snapshot (filtrer a la sauvegarde).")
        p = self.path / "data" / f"{mapping.key}.jsonl"
        if not p.exists():
            return []
        recs = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        return recs[:limit] if limit else recs

    def files(self, mapping, source_ids):
        d = self.path / "files" / mapping.key
        if not (d / "index.json").exists():
            return []
        items = json.loads((d / "index.json").read_text(encoding="utf-8"))
        wanted = set(source_ids)
        out = []
        for i in items:
            if wanted & set(i["parents"]):
                i["data"] = (d / i["file"]).read_bytes()
                out.append(i)
        return out
