"""Extraction SOQL depuis l'org source (pagination REST, filtre delta, scope d'execution)."""

from __future__ import annotations

import re

from simple_salesforce import Salesforce

from sfdata.config import ObjectMapping


def _cond_field(condition: str) -> str:
    return condition.split("=", 1)[0]


def source_fields(mapping: ObjectMapping) -> set[str]:
    """Champs a lire : mappes, lookups (+ identifiant parent prefere), conditions, identifiant, techniques."""
    s = {"Id", "SystemModstamp"}
    s |= {f.source for f in mapping.fields if f.source and f.constant is None}
    for lk in mapping.lookups:
        s.add(lk.source)
        if lk.prefer:
            s.add(lk.prefer)
    c = mapping.external_id_compute
    if c:
        s.add(c["source"])
        if c.get("prefer"):
            s.add(c["prefer"])
    elif not mapping.track_target_id:
        s.add(mapping.external_id_field)
    for item in (*mapping.fields, *mapping.lookups):
        s |= {_cond_field(x) for x in (item.only_if, item.unless) if x}
    return s


def normalize_watermark(wm: str) -> str:
    """SOQL n'accepte pas `+0000` : `2026-09-04T10:00:00.000+0000` -> `...Z`."""
    return re.sub(r"\+0000$", "Z", wm)


def build_soql(mapping: ObjectMapping, watermark: str | None = None, where: str | None = None,
               limit: int | None = None) -> str:
    """`watermark` : mode delta (`SystemModstamp >`). `where` : restriction d'execution, en plus du
    `scope_filter` du mapping. `limit` : N premiers records (ordre SystemModstamp, donc iterable en delta)."""
    select = ", ".join(sorted(source_fields(mapping)) + list(mapping.select_extra))
    soql = f"SELECT {select} FROM {mapping.source_object}"
    conds = [f"({x})" for x in (mapping.scope_filter, where) if x]
    if watermark:
        conds.append(f"SystemModstamp > {normalize_watermark(watermark)}")
    if conds:
        soql += " WHERE " + " AND ".join(conds)
    soql += " ORDER BY SystemModstamp, Id"
    if limit:
        soql += f" LIMIT {int(limit)}"
    return soql


def extract(sf: Salesforce, mapping: ObjectMapping, watermark=None, where=None, limit=None) -> list[dict]:
    records = sf.query_all(build_soql(mapping, watermark, where, limit)).get("records", [])
    for r in records:
        r.pop("attributes", None)
        for v in r.values():
            if isinstance(v, dict):
                v.pop("attributes", None)
    return records


class OrgSource:
    """Source = une org Salesforce (lecture seule)."""

    def __init__(self, sf: Salesforce, alias: str):
        self.sf, self.alias, self.is_snapshot = sf, alias, False

    def records(self, mapping, watermark=None, where=None, limit=None):
        return extract(self.sf, mapping, watermark, where, limit)

    def files(self, mapping, source_ids):
        from sfdata.files import fetch_files
        return fetch_files(self.sf, source_ids)
