"""Chargement via sObject Collections, lots de 200.

Variantes selon le mapping : upsert par identifiant externe (cas general), upsert + backfill (la cible
genere l'identifiant), insertion tracee dans l'idmap (aucun identifiant externe), insertion dedoublonnee
par requete (`insert_only`), update direct par Id (pass 2 des objets traces).
`allOrNone=False` : une ligne en erreur ne bloque pas le reste du lot (rapport succes/erreurs par ligne).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from simple_salesforce import Salesforce

from sfdata.config import ObjectMapping

BATCH_SIZE = 200

# Champs d'audit : ecrivables a l'INSERT seulement (permission CreateAuditFields sur l'utilisateur d'API).
AUDIT_FIELDS = ("CreatedDate", "CreatedById", "LastModifiedDate", "LastModifiedById")
_AUDIT_RELATIONS = ("CreatedBy", "LastModifiedBy")
_AUDIT_ERROR_PREFIX = "Unable to create/update fields:"


@dataclass
class LoadResult:
    external_id: str | None
    success: bool
    created: bool | None
    errors: list = field(default_factory=list)
    empty_fields: list = field(default_factory=list)  # champs mappes dont la valeur source etait vide
    target_id: str | None = None  # Id reel cote cible (alimente le journal de purge)


def chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _is_audit_update_error(item: dict) -> bool:
    return any(e.get("message", "").startswith(_AUDIT_ERROR_PREFIX) and any(f in e["message"] for f in AUDIT_FIELDS)
               for e in item.get("errors", []))


def _body(mapping: ObjectMapping, batch: list[dict]) -> dict:
    recs = []
    for r in batch:
        item = {k: v for k, v in r.items() if not k.startswith("_")}
        item.setdefault("attributes", {"type": mapping.target_object})
        recs.append(item)
    return {"allOrNone": False, "records": recs}


def _errors(item: dict) -> list:
    return [e.get("message", str(e)) for e in item.get("errors", [])]


def _empty(record: dict) -> list:
    return sorted(k for k, v in record.items() if v is None and not k.startswith("_"))


def upsert(sf: Salesforce, mapping: ObjectMapping, payloads: list[dict]) -> list[LoadResult]:
    """PATCH composite/sobjects/<SObject>/<ExternalIdField>."""
    results: list[LoadResult] = []
    endpoint = f"composite/sobjects/{mapping.target_object}/{mapping.external_id_field}"
    for batch in chunks(payloads, mapping.batch_size or BATCH_SIZE):
        body = _body(mapping, batch)
        response = list(sf.restful(endpoint, method="PATCH", json=body))
        # Record deja present : l'UPDATE refuse les champs d'audit -> on rejoue ces lignes sans eux.
        retry = [i for i, it in enumerate(response) if not it.get("success") and _is_audit_update_error(it)]
        if retry:
            rb = {"allOrNone": False, "records": [
                {k: v for k, v in body["records"][i].items() if k not in AUDIT_FIELDS and k not in _AUDIT_RELATIONS}
                for i in retry]}
            for i, new in zip(retry, sf.restful(endpoint, method="PATCH", json=rb)):
                response[i] = new
        for record, item in zip(batch, response):
            results.append(LoadResult(record.get(mapping.external_id_field), bool(item.get("success")),
                                      item.get("created"), _errors(item), _empty(record), item.get("id")))
    return results


def insert_batch(sf: Salesforce, mapping: ObjectMapping, batch: list[dict]) -> list[dict]:
    return list(sf.restful("composite/sobjects", method="POST", json=_body(mapping, batch)))


def upsert_with_backfill(sf, mapping, payloads) -> tuple[list[LoadResult], dict]:
    """Objets `external_id_backfill` : les payloads portant `_source_id` (ni valeur source ni backfill
    local) sont inseres, la cible genere l'identifiant ; on le relit pour le memoriser (source Id -> valeur).
    """
    results = upsert(sf, mapping, [p for p in payloads if "_source_id" not in p])
    new_backfill: dict = {}
    for batch in chunks([p for p in payloads if "_source_id" in p], mapping.batch_size or BATCH_SIZE):
        created = []
        for rec, item in zip(batch, insert_batch(sf, mapping, batch)):
            if item.get("success"):
                created.append((rec["_source_id"], item["id"]))
            else:
                results.append(LoadResult(None, False, None, _errors(item)))
        if created:
            in_list = ", ".join(f"'{tid}'" for _, tid in created)
            rows = sf.query_all(f"SELECT Id, {mapping.external_id_field} FROM {mapping.target_object} "
                                f"WHERE Id IN ({in_list})")["records"]
            generated = {r["Id"]: r.get(mapping.external_id_field) for r in rows}
            for src, tid in created:
                if generated.get(tid):
                    new_backfill[src] = generated[tid]
                results.append(LoadResult(generated.get(tid), True, True, [], [], tid))
    return results, new_backfill


def insert_tracked(sf, mapping, payloads) -> list[LoadResult]:
    """Objets `track_target_id` : INSERT simple ; `external_id` du resultat porte l'Id cible reel."""
    results = []
    for batch in chunks(payloads, BATCH_SIZE):
        for rec, item in zip(batch, insert_batch(sf, mapping, batch)):
            ok = bool(item.get("success"))
            results.append(LoadResult(item.get("id") if ok else None, ok, ok or None, _errors(item),
                                      _empty(rec), item.get("id") if ok else None))
    return results


def update_by_id(sf, mapping, updates: list[dict]) -> list[LoadResult]:
    """PATCH composite/sobjects : chaque entree porte `id` (Id cible reel) + les champs a ecrire."""
    results = []
    for batch in chunks(updates, BATCH_SIZE):
        for rec, item in zip(batch, sf.restful("composite/sobjects", method="PATCH", json=_body(mapping, batch))):
            results.append(LoadResult(rec.get("id"), bool(item.get("success")), False, _errors(item)))
    return results


def insert_with_dedup(sf, mapping, payloads) -> list[LoadResult]:
    """Objets `insert_only` (identifiant non marquable `externalId`) : idempotence emulee cote client en
    interrogeant la cible pour les valeurs deja presentes."""
    field_ = mapping.external_id_field
    existing = {r[field_] for r in sf.query_all(
        f"SELECT {field_} FROM {mapping.target_object} WHERE {field_} != null")["records"]}
    results, todo = [], []
    for p in payloads:
        if p.get(field_) in existing:
            results.append(LoadResult(p.get(field_), True, False))
        else:
            todo.append(p)
    for batch in chunks(todo, mapping.batch_size or BATCH_SIZE):
        for rec, item in zip(batch, insert_batch(sf, mapping, batch)):
            ok = bool(item.get("success"))
            results.append(LoadResult(rec.get(field_), ok, ok, _errors(item), _empty(rec), item.get("id") if ok else None))
    return results
