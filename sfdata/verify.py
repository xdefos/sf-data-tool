"""Controle post-run (lecture seule) : totaux source / charges / cible et doublons par objet."""

from __future__ import annotations

from dataclasses import dataclass

from sfdata.config import ObjectMapping
from sfdata.ledger import live_entries, load_entries


def _count(sf, sobject: str, where: str | None) -> int:
    return sf.query(f"SELECT COUNT() FROM {sobject}" + (f" WHERE {where}" if where else ""))["totalSize"]


@dataclass
class ObjectCheck:
    key: str
    source: int
    loaded: int
    target: int
    duplicates: int

    @property
    def gap(self) -> int:
        return self.source - self.loaded

    @property
    def ok(self) -> bool:
        return self.gap == 0 and self.duplicates == 0


def check_object(sf_source, sf_target, mapping: ObjectMapping, target_alias: str) -> ObjectCheck:
    src = _count(sf_source, mapping.source_object, mapping.scope_filter)
    tgt = _count(sf_target, mapping.target_object, None)
    loaded = sum(1 for e in live_entries(load_entries(target_alias)) if e["object"] == mapping.key)
    dup = 0
    if not mapping.track_target_id and not mapping.external_id_compute:
        f = mapping.external_id_field
        dup = len(sf_target.query_all(f"SELECT {f}, COUNT(Id) n FROM {mapping.target_object} WHERE {f} != null "
                                      f"GROUP BY {f} HAVING COUNT(Id) > 1")["records"])
    return ObjectCheck(mapping.key, src, loaded, tgt, dup)


def format_checks(checks: list[ObjectCheck]) -> str:
    lines = [f"{'objet':28s} {'source':>8} {'charges':>8} {'ecart':>6} {'cible':>8} {'doublons':>8}"]
    for c in checks:
        lines.append(f"{c.key:28s} {c.source:>8} {c.loaded:>8} {c.gap:>6} {c.target:>8} {c.duplicates:>8}"
                     + ("" if c.ok else "  <-- a expliquer"))
    return "\n".join(lines)


def checks_to_dict(checks):
    return [{"key": c.key, "source": c.source, "loaded": c.loaded, "gap": c.gap, "target": c.target,
             "duplicates": c.duplicates, "ok": c.ok} for c in checks]
