"""Purge des records crees par l'outil (sandbox uniquement).

`build_plan` ne fait que des SELECT/describe. `execute_plan` est le seul point de suppression (DELETE normal,
corbeille). Source de verite : le journal `ledger.py` (jamais les champs d'audit). Ordre : niveaux decroissants.
Users jamais supprimes. Un plan est refuse s'il laisse des dependants du journal non selectionnes.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from simple_salesforce.exceptions import SalesforceError

from sfdata.config import STATE_DIR, load_object_mapping
from sfdata.ledger import append_entries
from sfdata.load import chunks
from sfdata.state import forget_purged

IGNORED_SOBJECTS = frozenset({"User"})
NOISE_SUFFIXES = ("History", "Share", "Feed", "ChangeEvent", "Tag", "Event__e")
NOISE_SOBJECTS = frozenset({"AttachedContentDocument", "AttachedContentNote", "CombinedAttachment",
                            "NetworkUserHistoryRecent", "NoteAndAttachment"})
CHUNK = 400


@dataclass
class ObjectPlan:
    key: str
    sobject: str
    level: int
    ids: list
    present: int = 0
    missing: int = 0
    deletable: list = field(default_factory=list)
    external_ids: dict = field(default_factory=dict)


@dataclass
class Finding:
    parent: str
    child: str
    field: str
    count: int
    kind: str  # cascade (supprime avec le parent) | restrict (bloque)


@dataclass
class Plan:
    objects: list = field(default_factory=list)
    ignored: dict = field(default_factory=dict)
    missing_dependents: list = field(default_factory=list)  # (parent_key, enfant_key)
    findings: list = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.missing_dependents) or any(f.kind == "restrict" and f.count for f in self.findings)


def _in(ids) -> str:
    return ", ".join(f"'{i}'" for i in ids)


def child_relationships(sf, sobject: str) -> list[dict]:
    d = sf.restful(f"sobjects/{sobject}/describe")
    return [{"child": r["childSObject"], "field": r["field"], "kind": "cascade" if r.get("cascadeDelete") else "restrict"}
            for r in d.get("childRelationships", [])
            if (r.get("cascadeDelete") or r.get("restrictedDelete"))
            and not r["childSObject"].endswith(NOISE_SUFFIXES) and r["childSObject"] not in NOISE_SOBJECTS]


def build_plan(sf, entries: list[dict], keys: set | None = None, with_dependents: bool = False) -> Plan:
    plan = Plan()
    by_key: dict[str, list[dict]] = {}
    for e in entries:
        if e["sobject"] in IGNORED_SOBJECTS:
            plan.ignored[e["sobject"]] = plan.ignored.get(e["sobject"], 0) + 1
        elif not keys or e["object"] in keys:
            by_key.setdefault(e["object"], []).append(e)
    level = {k: int(load_object_mapping(k).level) for k in by_key}
    # dependants : un enfant du journal (lookup vers un parent selectionne) non selectionne bloque le plan
    selected = set(by_key)
    for k in list(by_key):
        for lk in load_object_mapping(k).lookups:
            if lk.parent_key in selected and k not in selected:
                plan.missing_dependents.append((lk.parent_key, k))
    if keys:
        for k in sorted(set(_journal_keys(entries)) - selected):
            for lk in load_object_mapping(k).lookups:
                if lk.parent_key in selected:
                    plan.missing_dependents.append((lk.parent_key, k))
    if with_dependents:
        plan.missing_dependents = []
    for k in sorted(by_key, key=lambda x: -level[x]):
        es = by_key[k]
        sobject = es[0]["sobject"]
        ids = [e["target_id"] for e in es]
        op = ObjectPlan(k, sobject, level[k], ids)
        ext = load_object_mapping(k).external_id_field
        rows = []
        for part in chunks(ids, CHUNK):
            sel = "Id" if load_object_mapping(k).track_target_id else f"Id, {ext}"
            rows += sf.query_all(f"SELECT {sel} FROM {sobject} WHERE Id IN ({_in(part)})")["records"]
        op.deletable = [r["Id"] for r in rows]
        op.external_ids = {r["Id"]: r.get(ext) for r in rows}
        op.present, op.missing = len(rows), len(ids) - len(rows)
        plan.objects.append(op)
        if op.deletable:
            for rel in child_relationships(sf, sobject):
                n = 0
                for part in chunks(op.deletable, CHUNK):
                    n += sf.query(f"SELECT COUNT() FROM {rel['child']} WHERE {rel['field']} IN ({_in(part)})")["totalSize"]
                if n:
                    plan.findings.append(Finding(sobject, rel["child"], rel["field"], n, rel["kind"]))
    return plan


def _journal_keys(entries):
    return {e["object"] for e in entries if e["sobject"] not in IGNORED_SOBJECTS}


def format_plan(plan: Plan) -> str:
    lines = ["Plan de purge (ordre de suppression) :"]
    for o in plan.objects:
        lines.append(f"  niveau {o.level:>2}  {o.key:28s} journal={len(o.ids):>6}  presents={o.present:>6}  absents={o.missing:>6}")
    for s, n in plan.ignored.items():
        lines.append(f"  (ignore : {n} {s}, jamais supprimes)")
    for f in plan.findings:
        lines.append(f"  {f.kind.upper():8s} {f.parent} -> {f.child}.{f.field} : {f.count}")
    for p, c in plan.missing_dependents:
        lines.append(f"  BLOQUE : {c} depend de {p} et n'est pas selectionne (ajouter {c} ou --with-dependents)")
    return "\n".join(lines)


def execute_plan(sf, plan: Plan, target_alias: str, run_ts: str, source_alias=None, state_dir: Path = STATE_DIR, emit=print):
    for o in plan.objects:
        if not o.deletable:
            continue
        state_dir.mkdir(parents=True, exist_ok=True)
        with (state_dir / f"purge_{run_ts}_{o.key}_avant.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "external_id"])
            w.writerows([[i, o.external_ids.get(i)] for i in o.deletable])
        deleted, failed = [], []
        for part in chunks(o.deletable, 200):
            ids = ",".join(part)
            try:
                resp = sf.restful(f"composite/sobjects?ids={ids}&allOrNone=false", method="DELETE")
            except SalesforceError as exc:
                failed += [(i, str(exc)) for i in part]
                continue
            for i, r in zip(part, resp):
                (deleted if r.get("success") else failed).append(i if r.get("success") else (i, str(r.get("errors"))))
        with (state_dir / f"purge_{run_ts}_{o.key}.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "status", "detail"])
            w.writerows([[i, "deleted", ""] for i in deleted] + [[i, "failed", e] for i, e in failed])
        append_entries(target_alias, [{"action": "purged", "target_id": i, "object": o.key, "sobject": o.sobject,
                                       "run_ts": run_ts} for i in deleted], state_dir)
        forget_purged(target_alias, o.key, set(deleted), {o.external_ids[i] for i in deleted if o.external_ids.get(i)},
                      source_alias, state_dir)
        emit(f"[{o.key}] {len(deleted)} supprime(s), {len(failed)} en echec")
