"""Moteur de run (`init` / `delta`) : extraction -> transformation -> chargement, niveau par niveau.

La source est une org (`OrgSource`) ou un snapshot (`SnapshotSource`). La cible est toujours une org.
Fonctionnement iteratif : chaque run est rejouable (upsert), `delta` repart du watermark, `--limit` charge
par tranches successives (le watermark n'avance que sur ce qui a ete lu ET charge sans erreur).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sfdata import journal
from sfdata.config import load_object_mapping
from sfdata.files import link_file, push_file
from sfdata.ledger import record_created
from sfdata.load import (LoadResult, insert_tracked, insert_with_dedup, update_by_id, upsert,
                         upsert_with_backfill)
from sfdata.report import print_summary, write_report
from sfdata.sequencer import build_sequence
from sfdata.state import (get_watermark, load_backfill, load_files_map, load_id_map, max_system_modstamp,
                          set_backfill_ids, set_files_map, set_id_map, set_watermark)
from sfdata.transform import Context, TransformError, check_condition, resolve_source_path, transform_record


@dataclass
class RunOptions:
    mode: str = "init"                 # init (tout le perimetre) | delta (depuis le watermark)
    keys: list | None = None           # cles de mapping (None = tous, dans l'ordre des niveaux)
    dry_run: bool = False
    anonymize: bool = True
    where: str | None = None           # restriction SOQL supplementaire (org source uniquement)
    limit: int | None = None           # N premiers records par objet (iteration par tranches)
    with_files: bool = True
    emit: object = print


@dataclass
class RunResult:
    run_ts: str
    errors: bool = False
    summaries: list = field(default_factory=list)
    unresolved_users: set = field(default_factory=set)


def _ordered_keys(plan, wanted) -> list[str]:
    return [k for lvl in plan for k in lvl.objects if not wanted or k in wanted]


def _parent_maps(target_alias: str, mapping) -> dict:
    """Idmaps des parents references `via_idmap` (parents sans identifiant externe)."""
    return {lk.parent_key: load_id_map(target_alias, lk.parent_key)
            for lk in mapping.lookups if lk.via_idmap and lk.parent_key}


def _lookup_backfills(target_alias: str, mapping) -> dict:
    return {lk.backfill_object: load_backfill(target_alias, lk.backfill_object)
            for lk in mapping.lookups if lk.backfill_object}


def select_with_parents(source, keys, wm_of, opts) -> tuple[dict, dict]:
    """`--limit N` : N premiers records de chaque objet (graines) + leurs parents requis, recursivement.

    Sans cette fermeture, une trame partielle casse les liens (enquete -> delegue absent de la trame).
    Retourne ({cle: [records]}, {cle: graines}). Le watermark ne suit que les graines (ordre SystemModstamp).
    """
    maps = {k: load_object_mapping(k) for k in keys}
    seeds, sel = {}, {}
    for k, m in maps.items():
        seeds[k] = source.records(m, wm_of(k), opts.where, opts.limit)
        sel[k] = {r["Id"]: r for r in seeds[k]}
    order = sorted(maps, key=lambda k: -int(maps[k].level))
    asked: dict = {k: set() for k in maps}  # ids deja demandes (un parent hors perimetre ne revient jamais)
    changed = True
    while changed:
        changed = False
        for k in order:
            for lk in maps[k].lookups:
                pk = lk.parent_key
                if lk.user or "." in lk.source or pk not in maps:
                    continue
                need = {r.get(lk.source) for r in sel[k].values()} - {None} - set(sel[pk]) - asked[pk]
                if need:
                    asked[pk] |= need
                    for r in source.records_by_ids(maps[pk], need):
                        sel[pk][r["Id"]] = r
                    changed = True
    return {k: list(v.values()) for k, v in sel.items()}, seeds


def run(source, sf_target, target_alias: str, opts: RunOptions, user_resolver=None) -> RunResult:
    emit = opts.emit
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    res = RunResult(run_ts)
    plan = build_sequence()
    wanted = set(opts.keys) if opts.keys else None
    keys = _ordered_keys(plan, wanted)
    journal.append("run_start", run_ts=run_ts, mode=opts.mode, source=source.alias, target=target_alias,
                   objects=keys, dry_run=opts.dry_run, where=opts.where, limit=opts.limit)

    def wm_of(k):
        return get_watermark(source.alias, k) if opts.mode == "delta" and not source.is_snapshot else None

    selection = seeds = None
    if opts.limit:
        try:
            selection, seeds = select_with_parents(source, keys, wm_of, opts)
        except Exception as exc:
            emit(f"ERREUR extraction : {exc}")
            res.errors = True
            keys = []
    for key in keys:
        mapping = load_object_mapping(key)
        watermark = wm_of(key)
        try:
            records = selection[key] if selection is not None else source.records(mapping, watermark, opts.where, None)
        except Exception as exc:  # erreur SOQL / champ absent de la source : on continue les autres objets
            emit(f"[{key}] ERREUR extraction : {exc}")
            res.errors = True
            continue
        emit(f"[{key}] {len(records)} record(s) extrait(s)" + (f" depuis {watermark}" if watermark else ""))
        if not records:
            continue
        ctx = Context(anonymize=opts.anonymize, user_resolver=user_resolver,
                      backfill_map=load_backfill(target_alias, key) if mapping.external_id_backfill else {},
                      lookup_backfill_maps=_lookup_backfills(target_alias, mapping),
                      id_maps=_parent_maps(target_alias, mapping))
        try:
            payloads = [transform_record(r, mapping, 1, ctx) for r in records]
        except TransformError as exc:
            emit(f"[{key}] ERREUR transform : {exc}")
            res.errors = True
            continue
        if opts.dry_run:
            emit(f"[{key}] dry-run : {len(payloads)} payload(s) pret(s), aucune ecriture")
            continue

        results, new_backfill = _load(sf_target, mapping, records, payloads, target_alias, ctx)
        if new_backfill:
            set_backfill_ids(target_alias, key, new_backfill)
            ctx.backfill_map.update(new_backfill)
            emit(f"[{key}] {len(new_backfill)} identifiant(s) genere(s) memorise(s) pour '{target_alias}'")
        record_created(target_alias, key, mapping.target_object, results, run_ts)
        summary = write_report(key, results, run_ts)
        res.summaries.append(summary)
        emit(print_summary(summary))
        obj_error = summary.errors != 0

        if mapping.two_pass and any(lk.reflexive for lk in mapping.lookups):
            obj_error |= _pass2(sf_target, mapping, records, results, ctx, target_alias, run_ts, emit, res)
        if opts.with_files and mapping.files:
            _copy_files(source, sf_target, mapping, records, results, target_alias, emit)

        if obj_error:
            res.errors = True
        elif not source.is_snapshot:
            wm = max_system_modstamp(seeds[key] if seeds is not None else records)
            if wm:
                set_watermark(source.alias, key, wm)
    if user_resolver is not None:
        res.unresolved_users = set(user_resolver.unresolved)
        if res.unresolved_users:
            emit(f"{len(res.unresolved_users)} utilisateur(s) source sans equivalent cible (proprietaire par defaut) : "
                 f"voir mappings/_users.yaml")
    journal.append("run_end", run_ts=run_ts, errors=res.errors,
                   objects={s.key: {"ok": s.ok, "errors": s.errors, "created": s.created} for s in res.summaries})
    return res


def _load(sf, mapping, records, payloads, target_alias, ctx):
    """Dispatch selon la variante du mapping ; retourne (results alignes sur `records`, backfill nouveau)."""
    new_backfill: dict = {}
    if mapping.track_target_id:
        id_map = load_id_map(target_alias, mapping.key)
        fresh = [(r, p) for r, p in zip(records, payloads) if r["Id"] not in id_map]
        results = insert_tracked(sf, mapping, [p for _, p in fresh])
        set_id_map(target_alias, mapping.key,
                   {r["Id"]: res.target_id for (r, _), res in zip(fresh, results) if res.success and res.target_id})
        return results, new_backfill
    if mapping.insert_only:
        return insert_with_dedup(sf, mapping, payloads), new_backfill
    if mapping.external_id_backfill:
        return upsert_with_backfill(sf, mapping, payloads)
    return upsert(sf, mapping, payloads), new_backfill


def _pass2(sf, mapping, records, results, ctx, target_alias, run_ts, emit, res) -> bool:
    """Lookups reflexifs (self-lookup, cycles) une fois tous les records du niveau en place."""
    emit(f"[{mapping.key}] pass 2 (lookups reflexifs)...")
    if mapping.track_target_id:
        id_map = load_id_map(target_alias, mapping.key)
        ctx.id_maps[mapping.key] = id_map
        updates = []
        for r in records:
            own = id_map.get(r["Id"])
            if not own:
                continue
            upd = {"id": own}
            for lk in (x for x in mapping.lookups if x.reflexive):
                if lk.only_if and not check_condition(r, lk.only_if):
                    continue
                parent = resolve_source_path(r, lk.source)
                upd[lk.target] = id_map.get(parent) if parent else None
            updates.append(upd)
        out = update_by_id(sf, mapping, updates)
    else:
        ok = [r for r in records if not mapping.external_id_backfill or ctx.backfill_map.get(r["Id"])]
        out = upsert(sf, mapping, [transform_record(r, mapping, 2, ctx) for r in ok])
    s = write_report(f"{mapping.key}_pass2", out, run_ts)
    res.summaries.append(s)
    emit(print_summary(s))
    return s.errors != 0


def _copy_files(source, sf_target, mapping, records, results, target_alias, emit) -> None:
    """Copie les fichiers lies. Parent cible : Id reel du record (cree ou retrouve par identifiant externe)."""
    done = load_files_map(target_alias, mapping.key)
    id_map = load_id_map(target_alias, mapping.key) if mapping.track_target_id else {}
    target_of = {}
    for r, res in zip(records, results):
        if res.success and res.target_id:
            target_of[r["Id"]] = res.target_id
        elif r["Id"] in id_map:
            target_of[r["Id"]] = id_map[r["Id"]]
    items = [i for i in source.files(mapping, list(target_of)) if i["doc"] not in done]
    new = {}
    for it in items:
        parents = [target_of[p] for p in it["parents"] if p in target_of]
        if not parents:
            continue
        doc = push_file(sf_target, it, it["data"], parents[0])
        for p in parents[1:]:
            link_file(sf_target, doc, p)
        new[it["doc"]] = doc
    set_files_map(target_alias, mapping.key, new)
    emit(f"[{mapping.key}] {len(new)} fichier(s) copie(s)")
