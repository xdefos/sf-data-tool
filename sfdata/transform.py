"""Transformation d'un record source (dict SOQL) en payload d'ecriture cible.

Logique pure, sans appel reseau (testable sans org). Les parents sont references par leur identifiant
externe (syntaxe REST imbriquee `{"Enquete__r": {"ExtId__c": "..."}}`) : l'API Salesforce les resout
cote cible, on ne manipule jamais les Id source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from sfdata.config import LookupMapping, ObjectMapping


class TransformError(Exception):
    pass


@dataclass
class Context:
    anonymize: bool = True
    backfill_map: dict = field(default_factory=dict)            # Id source -> ExternalId genere (objet courant)
    lookup_backfill_maps: dict = field(default_factory=dict)    # parent_key -> {Id source -> ExternalId}
    id_maps: dict = field(default_factory=dict)                 # parent_key -> {Id source -> Id cible}
    user_resolver: Callable[[str], str | None] | None = None    # username source -> Id User cible


def relationship_name(field_name: str) -> str:
    """`AccountId` -> `Account`, `Custom__c` -> `Custom__r`. Sinon `relationship_name:` dans le YAML."""
    if field_name.endswith("__c"):
        return field_name[:-3] + "__r"
    if field_name.endswith("Id") and field_name != "Id":
        return field_name[:-2]
    return field_name


def resolve_source_path(record: dict, path: str):
    """`record[path]` avec traversee de relation si `path` contient un '.' (SOQL renvoie des dicts imbriques)."""
    value = record
    for part in path.split("."):
        if value is None:
            return None
        value = value.get(part)
    return value


def check_condition(record: dict, condition: str) -> bool:
    """`champ` (non vide) ou `champ=valeur` (egalite)."""
    if "=" in condition:
        path, _, expected = condition.partition("=")
        return str(resolve_source_path(record, path)) == expected
    return bool(resolve_source_path(record, condition))


def compute_external_id(record: dict, spec: dict):
    """`prefer` (identifiant source s'il existe) sinon `<prefix>_<source>` (typiquement l'Id source)."""
    if spec.get("prefer"):
        v = resolve_source_path(record, spec["prefer"])
        if v:
            return v
    raw = resolve_source_path(record, spec["source"])
    if raw and spec.get("id_length") and isinstance(raw, str):
        raw = raw[:spec["id_length"]]
    return f"{spec['prefix']}_{raw}" if raw and spec.get("prefix") else raw


def external_id_value(record: dict, mapping: ObjectMapping, backfill_map: dict | None = None):
    if mapping.external_id_compute:
        return compute_external_id(record, mapping.external_id_compute)
    if mapping.external_id_backfill:
        return record.get(mapping.external_id_field) or (backfill_map or {}).get(record["Id"])
    return record.get(mapping.external_id_field)


def apply_transcode(value, transcode):
    """Valeur absente de la table = erreur bloquante (detecte les valeurs non couvertes par le mapping)."""
    if value is None or not transcode:
        return value
    if value not in transcode:
        raise TransformError(f"Valeur '{value}' absente de la table de transcodage {sorted(transcode, key=str)}")
    return transcode[value]


def _active(record: dict, item) -> bool:
    if item.only_if and not check_condition(record, item.only_if):
        return False
    return not (item.unless and check_condition(record, item.unless))


def resolve_lookup(record: dict, lk: LookupMapping, ctx: Context) -> dict:
    """Fragment de payload pour un lookup ({} = omis : parent hors perimetre ou non resolu)."""
    fk = resolve_source_path(record, lk.source)
    if lk.user:
        uid = ctx.user_resolver(fk) if fk and ctx.user_resolver else None
        return {lk.target: uid} if uid else {}
    if not fk:
        return {lk.target: None}
    if lk.via_idmap:
        tid = ctx.id_maps.get(lk.parent_key or "", {}).get(fk)
        return {lk.target: tid} if tid else {}
    if lk.backfill_object:
        ext = ctx.lookup_backfill_maps.get(lk.backfill_object, {}).get(fk)
    else:
        ext = resolve_source_path(record, lk.prefer) if lk.prefer else None
        if not ext and lk.compute:
            ext = f"{lk.compute['prefix']}_{fk}"
    if not ext:
        return {}
    return {lk.relationship_name or relationship_name(lk.target): {lk.parent_external_id: ext}}


def transform_record(record: dict, mapping: ObjectMapping, pass_number: int = 1, ctx: Context | None = None) -> dict:
    """Payload d'ecriture.

    pass 1 : champs + lookups non reflexifs + constantes. pass 2 (`two_pass`) : identifiant + lookups
    reflexifs seulement (update cible une fois tous les records du niveau en place).
    """
    ctx = ctx or Context()
    out: dict = {}
    if not mapping.track_target_id:
        ext = external_id_value(record, mapping, ctx.backfill_map)
        if ext:
            out[mapping.external_id_field] = ext
        elif mapping.external_id_backfill:
            out["_source_id"] = record["Id"]  # route vers l'INSERT : la cible generera l'identifiant
        else:
            raise TransformError(f"Identifiant externe vide ({mapping.external_id_field}) pour {record.get('Id')}")
    if pass_number == 2:
        for lk in mapping.lookups:
            if lk.reflexive and _active(record, lk):
                out.update(resolve_lookup(record, lk, ctx))
        return out
    for f in mapping.fields:
        if not _active(record, f):
            continue
        if f.constant is not None:
            out[f.target] = f.constant
            continue
        v = resolve_source_path(record, f.source)
        if v is None:
            out[f.target] = None
            continue
        if f.drop_values and v in f.drop_values:
            continue
        v = apply_transcode(v, f.transcode)
        if isinstance(v, str):
            if f.truncate_at:
                v = v[:f.truncate_at]
            if f.append_suffix and (not f.anonymize or ctx.anonymize):
                v += f.append_suffix
        out[f.target] = v
    for lk in mapping.lookups:
        if (not lk.reflexive or not mapping.two_pass) and _active(record, lk):
            out.update(resolve_lookup(record, lk, ctx))
    out.update(mapping.constants)
    return out
