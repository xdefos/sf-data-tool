"""Generation des mappings YAML depuis le describe de l'org source (`init-mappings`).

Le YAML genere est un point de depart : on le relit et on l'ajuste a la main (transcodes, exclusions, niveaux).
Regeneration non destructive : un mapping existant n'est ecrase qu'avec --force.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from sfdata.config import MAPPINGS_DIR
from sfdata.graph import load_order

SYSTEM_FIELDS = {"Id", "IsDeleted", "CreatedDate", "CreatedById", "LastModifiedDate", "LastModifiedById",
                 "SystemModstamp", "LastActivityDate", "LastViewedDate", "LastReferencedDate", "OwnerId"}
SKIP_TYPES = {"base64", "address", "location", "encryptedstring"}
EXT_CANDIDATES = ("ExtId__c", "ExternalId__c")


def describe_object(sf, sobject: str) -> dict:
    return getattr(sf, sobject).describe()


def build_mappings(sf, sobjects: list[str]) -> dict[str, dict]:
    """{cle: dict YAML}. Cle = nom d'objet en minuscules sans `__c`."""
    desc = {o: describe_object(sf, o) for o in sobjects}
    key_of = {o: o.lower().removesuffix("__c") for o in sobjects}
    refs: dict[str, dict] = {}
    for o, d in desc.items():
        refs[o] = {f["name"]: {t for t in f["referenceTo"] if t in desc}
                   for f in d["fields"] if f["type"] == "reference" and f["createable"]
                   and any(t in desc for t in f["referenceTo"])}
    order, deferred = load_order(refs)
    level = {o: i + 1 for i, o in enumerate(order)}
    out = {}
    for o in sobjects:
        d = desc[o]
        names = {f["name"] for f in d["fields"]}
        ext = next((c for c in EXT_CANDIDATES if c in names), None)
        fields, lookups = [], []
        for f in d["fields"]:
            n = f["name"]
            if n in SYSTEM_FIELDS or not f["createable"] or f["calculated"] or f["type"] in SKIP_TYPES or n == ext:
                continue
            if f["type"] == "reference":
                targets = [t for t in f["referenceTo"] if t in desc]
                if len(f["referenceTo"]) == 1 and targets:
                    t = targets[0]
                    lk = {"source": n, "target": n, "parent_key": key_of[t]}
                    tparent_ext = next((c for c in EXT_CANDIDATES if c in {x["name"] for x in desc[t]["fields"]}), None)
                    if tparent_ext:
                        lk["parent_external_id"] = tparent_ext
                        if f["relationshipName"]:
                            lk["relationship_name"] = f["relationshipName"]
                            lk["prefer"] = f"{f['relationshipName']}.{tparent_ext}"
                            lk["compute"] = {"prefix": key_of[t].upper()}
                    else:
                        lk["via_idmap"] = True
                    if n in deferred.get(o, set()):
                        lk["reflexive"] = True
                    lookups.append(lk)
                continue  # references hors perimetre (User, RecordType...) : omises
            fields.append({"source": n, "target": n})
        if "OwnerId" in names:
            lookups.append({"source": "Owner.Username", "target": "OwnerId", "user": True,
                            "rule": "proprietaire resolu par username (mappings/_users.yaml)"})
        m = {"source_object": o, "target_object": o, "level": level[o], "scope_filter": None, "files": False}
        if ext:
            m["external_id_field"] = ext
            m["external_id_compute"] = {"source": "Id", "prefer": ext, "prefix": key_of[o].upper()}
        else:
            m["external_id_field"] = "Id"
            m["track_target_id"] = True
        if deferred.get(o):
            m["two_pass"] = True
        m["fields"], m["lookups"] = fields, lookups
        out[key_of[o]] = m
    # une cle `ext` non-externalId sur la cible fait echouer l'upsert : on le verifie via describe
    for o, m in out.items():
        if m.get("external_id_compute"):
            f = next(x for x in desc[next(s for s in sobjects if key_of[s] == o)]["fields"] if x["name"] == m["external_id_field"])
            if not f.get("externalId") and not f.get("unique"):
                m.pop("external_id_compute")
                m["external_id_field"] = "Id"
                m["track_target_id"] = True
    return out


def write_mappings(mappings: dict[str, dict], mappings_dir: Path = MAPPINGS_DIR, force: bool = False) -> list[str]:
    mappings_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for key, m in mappings.items():
        path = mappings_dir / f"{key}.yaml"
        if path.exists() and not force:
            continue
        path.write_text(yaml.safe_dump(m, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8")
        written.append(key)
    return written
