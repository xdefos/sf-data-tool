"""CLI `sfdata` : init-mappings, profile, status, prereqs, backup, init, delta, verify, purge.

Garde-fous : la source n'est jamais ecrite ; une cible non-sandbox est refusee (sauf --dry-run) ;
purge reservee aux sandbox. Aucune org par defaut : --source / --target sont toujours explicites.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import click

from sfdata import journal, prereqs
from sfdata.auth import AuthError, describe_org, get_connection, is_sandbox
from sfdata.config import (SNAPSHOT_DIR, STATE_DIR, list_available_mappings, load_object_mapping,
                           load_users_map)
from sfdata.extract import OrgSource
from sfdata.ledger import live_entries, load_entries
from sfdata.mapgen import build_mappings, write_mappings
from sfdata.purge import build_plan, execute_plan, format_plan
from sfdata.runner import RunOptions, run
from sfdata.sequencer import build_sequence, flatten
from sfdata.snapshot import SnapshotSource, create_snapshot
from sfdata.state import get_watermark
from sfdata.users import UserResolver
from sfdata.verify import check_object, checks_to_dict, format_checks

PROD_ENV_VAR = "SFDATA_ALLOW_PROD"


def _conn(alias):
    try:
        return get_connection(alias)
    except AuthError as exc:
        raise click.ClickException(str(exc))


def _keys(objects_csv):
    if not objects_csv:
        return None
    keys = [k.strip() for k in objects_csv.split(",") if k.strip()]
    unknown = sorted(set(keys) - set(list_available_mappings()))
    if unknown:
        raise click.ClickException(f"Mapping(s) inconnu(s) : {', '.join(unknown)}. Disponibles : {', '.join(list_available_mappings())}")
    return keys


def _ordered(keys):
    flat = [k for _, k in flatten(build_sequence())]
    return [k for k in flat if not keys or k in keys]


def _sandbox(alias) -> bool:
    try:
        return is_sandbox(alias)
    except AuthError as exc:
        raise click.ClickException(str(exc))


def _guard_write_target(target, allow_prod, confirm):
    """Sandbox : ecriture libre. Prod : --allow-prod + --confirm <alias> + SFDATA_ALLOW_PROD=1 (les trois)."""
    if _sandbox(target):
        if allow_prod:
            raise click.ClickException(f"--allow-prod refuse : '{target}' est un sandbox.")
        return False
    missing = []
    if not allow_prod:
        missing.append("--allow-prod")
    if confirm != target:
        missing.append(f"--confirm {target}")
    if os.environ.get(PROD_ENV_VAR) != "1":
        missing.append(f"variable {PROD_ENV_VAR}=1")
    if missing:
        raise click.ClickException(
            f"Org cible '{target}' detectee comme PRODUCTION. Ecriture interdite : utiliser --dry-run, un sandbox, "
            f"ou le deverrouillage complet (manque : {', '.join(missing)}).")
    return True


@click.group()
def cli():
    """Extraction, chargement et purge de donnees Salesforce entre orgs. Voir README.md."""


@cli.command("init-mappings")
@click.option("--source", "source_alias", required=True)
@click.option("--objects", "sobjects_csv", required=True, help="Objets API source, separes par des virgules")
@click.option("--force", is_flag=True, help="Ecrase les mappings existants")
def init_mappings(source_alias, sobjects_csv, force):
    """Genere les mappings YAML (champs, lookups, niveaux) depuis le describe de la source."""
    sobjects = [o.strip() for o in sobjects_csv.split(",") if o.strip()]
    written = write_mappings(build_mappings(_conn(source_alias), sobjects), force=force)
    click.echo(f"{len(written)} mapping(s) ecrit(s) : {', '.join(written) or '(aucun, deja presents : --force)'}")
    click.echo("A relire : transcodes, scope_filter, files, cle d'identifiant externe, exclusions.")


@cli.command()
@click.option("--source", "source_alias", required=True)
@click.option("--json", "as_json", is_flag=True)
def status(source_alias, as_json):
    """Connexion a la source + watermarks par objet."""
    try:
        info = describe_org(source_alias)
    except AuthError as exc:
        raise click.ClickException(str(exc))
    wms = {k: get_watermark(source_alias, k) for k in list_available_mappings()}
    if as_json:
        click.echo(json.dumps({"source": source_alias, "username": info["username"], "sandbox": info["isSandbox"],
                               "watermarks": wms}, ensure_ascii=False, indent=2))
        return
    click.echo(f"Source '{source_alias}' : {info['username']} ({'sandbox' if info['isSandbox'] else 'PRODUCTION'}) "
               f"- {info['connectedStatus']}")
    for k, w in wms.items():
        click.echo(f"  {k:28s} watermark = {w or '(jamais synchronise)'}")


@cli.command()
@click.option("--source", "source_alias", required=True)
@click.option("--objects", "objects_csv", default=None, help="Cles de mapping (defaut : tous)")
def profile(source_alias, objects_csv):
    """Volumes de la source pour le perimetre (scope_filter inclus)."""
    sf = _conn(source_alias)
    for k in _ordered(_keys(objects_csv)):
        m = load_object_mapping(k)
        where = f" WHERE {m.scope_filter}" if m.scope_filter else ""
        click.echo(f"  {k:28s} {sf.query('SELECT COUNT() FROM ' + m.source_object + where)['totalSize']:>10}")


@cli.command("prereqs")
@click.option("--target", "target_alias", required=True)
@click.option("--objects", "objects_csv", default=None)
def prereqs_cmd(target_alias, objects_csv):
    """Prerequis de la cible (lecture seule)."""
    checks = prereqs.run_checks(_conn(target_alias), [load_object_mapping(k) for k in _ordered(_keys(objects_csv))])
    click.echo(prereqs.format_checks(checks))
    if any(not c.ok for c in checks):
        raise SystemExit(1)


@cli.command()
@click.option("--source", "source_alias", required=True)
@click.option("--objects", "objects_csv", default=None, help="Cles de mapping (defaut : tous)")
@click.option("--where", default=None, help="Restriction SOQL supplementaire (en plus du scope_filter)")
@click.option("--limit", type=int, default=None, help="N premiers records par objet")
@click.option("--no-files", is_flag=True)
@click.option("--out", type=click.Path(path_type=Path), default=SNAPSHOT_DIR)
def backup(source_alias, objects_csv, where, limit, no_files, out):
    """Sauvegarde (snapshot) : tous les champs lisibles + fichiers, dans un dossier horodate. Lecture seule."""
    sf = _conn(source_alias)
    path = create_snapshot(sf, source_alias, [load_object_mapping(k) for k in _ordered(_keys(objects_csv))],
                           where, limit, not no_files, out)
    journal.append("backup", source=source_alias, path=str(path))
    click.echo(f"\nSnapshot : {path}\nContient des donnees clients : ne jamais le committer.")


def _run_cmd(mode, source_alias, from_snapshot, target_alias, objects_csv, dry_run, anonymize, where, limit,
             no_files, allow_prod, confirm):
    if bool(source_alias) == bool(from_snapshot):
        raise click.ClickException("Indiquer exactement une source : --source <alias> ou --from-snapshot <dossier>.")
    prod_write = False
    if not dry_run:
        prod_write = _guard_write_target(target_alias, allow_prod, confirm)
    if not anonymize and _sandbox(target_alias):
        raise click.ClickException(f"--no-anonymize refuse : '{target_alias}' est un sandbox.")
    source = SnapshotSource(from_snapshot) if from_snapshot else OrgSource(_conn(source_alias), source_alias)
    if from_snapshot and mode == "delta":
        raise click.ClickException("delta n'a pas de sens depuis un snapshot : utiliser init.")
    sf_target = _conn(target_alias)
    keys = _keys(objects_csv)
    checks = prereqs.run_checks(sf_target, [load_object_mapping(k) for k in _ordered(keys)])
    click.echo(f"--- Prerequis cible '{target_alias}'\n{prereqs.format_checks(checks)}")
    if any(not c.ok for c in checks) and not dry_run:
        raise click.ClickException("Prerequis non satisfaits sur la cible : run annule.")
    resolver = UserResolver(sf_target, load_users_map())
    res = run(source, sf_target, target_alias,
              RunOptions(mode, keys, dry_run, anonymize, where, limit, not no_files, click.echo), resolver)
    if prod_write:
        click.echo("ATTENTION : ecriture en PRODUCTION effectuee.")
    if res.errors:
        raise click.ClickException("Run termine avec des erreurs (voir rapports state/run_*.csv).")


def _run_options(f):
    for dec in reversed([
        click.option("--source", "source_alias", default=None, help="Alias `sf` de l'org source"),
        click.option("--from-snapshot", type=click.Path(exists=True, path_type=Path), default=None,
                     help="Dossier de snapshot utilise comme source"),
        click.option("--target", "target_alias", required=True, help="Alias `sf` de l'org cible"),
        click.option("--objects", "objects_csv", default=None, help="Cles de mapping (defaut : tous)"),
        click.option("--dry-run", is_flag=True, help="Extract + transform, aucune ecriture"),
        click.option("--no-anonymize", "anonymize", flag_value=False, default=True,
                     help="Desactive l'anonymisation (refuse sur sandbox)"),
        click.option("--where", default=None, help="Restriction SOQL supplementaire sur la source"),
        click.option("--limit", type=int, default=None, help="N premiers records par objet (iteration par tranches)"),
        click.option("--no-files", is_flag=True),
        click.option("--allow-prod", is_flag=True),
        click.option("--confirm", "confirm", default=None, help="Repeter l'alias cible (cible de production)"),
    ]):
        f = dec(f)
    return f


@cli.command("init")
@_run_options
def init_cmd(**kw):
    """Chargement initial : tout le perimetre (ignore les watermarks)."""
    _run_cmd("init", **kw)


@cli.command()
@_run_options
def delta(**kw):
    """Chargement incremental : ce qui a change depuis le dernier run (SystemModstamp)."""
    _run_cmd("delta", **kw)


@cli.command()
@click.option("--source", "source_alias", required=True)
@click.option("--target", "target_alias", required=True)
@click.option("--objects", "objects_csv", default=None)
@click.option("--json", "as_json", is_flag=True)
def verify(source_alias, target_alias, objects_csv, as_json):
    """Controle post-run (lecture seule) : source / charges / cible / doublons."""
    s, t = _conn(source_alias), _conn(target_alias)
    checks = [check_object(s, t, load_object_mapping(k), target_alias) for k in _ordered(_keys(objects_csv))]
    click.echo(json.dumps(checks_to_dict(checks), ensure_ascii=False, indent=2) if as_json else format_checks(checks))
    journal.append("verify", source=source_alias, target=target_alias, gaps={c.key: c.gap for c in checks if not c.ok})


@cli.command()
@click.option("--target", "target_alias", required=True)
@click.option("--objects", "objects_csv", default=None)
@click.option("--all", "all_", is_flag=True, help="Tout le journal de la cible")
@click.option("--run", "run_ts", default=None, help="Limiter aux records crees par ce run (horodatage)")
@click.option("--with-dependents", is_flag=True)
@click.option("--execute", is_flag=True, help="Supprime (defaut : plan seul)")
@click.option("--confirm", default=None, help="Repeter l'alias cible pour confirmer")
@click.option("--source", "source_alias", default=None, help="Remet aussi a zero les watermarks de cette source")
def purge(target_alias, objects_csv, all_, run_ts, with_dependents, execute, confirm, source_alias):
    """Purge des records crees par l'outil (sandbox uniquement ; plan seul sans --execute)."""
    if _sandbox(target_alias) is False:
        raise click.ClickException(f"Purge refusee : '{target_alias}' n'est pas un sandbox.")
    if not (objects_csv or all_):
        raise click.ClickException("Indiquer --objects a,b ou --all.")
    entries = live_entries(load_entries(target_alias))
    if run_ts:
        entries = [e for e in entries if e.get("run_ts") == run_ts]
    sf = _conn(target_alias)
    plan = build_plan(sf, entries, set(_keys(objects_csv) or []) or None, with_dependents)
    click.echo(format_plan(plan))
    if not execute:
        click.echo("\nPlan seul. Ajouter --execute --confirm <alias cible> pour supprimer.")
        return
    if confirm != target_alias:
        raise click.ClickException(f"Confirmation manquante : --confirm {target_alias}")
    if plan.blocked:
        raise click.ClickException("Plan bloque (dependants ou enfants bloquants) : rien n'est supprime.")
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    journal.append("purge", target=target_alias, run_ts=ts, objects=[o.key for o in plan.objects])
    execute_plan(sf, plan, target_alias, ts, source_alias, STATE_DIR, click.echo)


if __name__ == "__main__":
    cli()
