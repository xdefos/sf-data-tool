"""Authentification via le CLI `sf` : aucun credential dans ce depot.

Le couple (source, cible) est un parametre de lancement : n'importe quel alias deja authentifie par
`sf org login ...`. On recupere `instanceUrl` + `accessToken` puis on construit un client simple_salesforce.
"""

from __future__ import annotations

import json
import subprocess

from simple_salesforce import Salesforce


class AuthError(RuntimeError):
    pass


def _sf_json(args: list[str]) -> dict:
    try:
        proc = subprocess.run(["sf", *args, "--json"], capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise AuthError("CLI `sf` introuvable dans le PATH. Installer @salesforce/cli.") from exc
    brace = proc.stdout.find("{")  # le CLI peut afficher des warnings avant le JSON
    if brace == -1:
        raise AuthError(f"Reponse inattendue de `sf {' '.join(args)}` : {proc.stdout or proc.stderr}")
    payload = json.loads(proc.stdout[brace:])
    if payload.get("status") != 0:
        raise AuthError(f"`sf {' '.join(args)}` a echoue : {payload.get('message', payload)}")
    return payload["result"]


def org_display(alias: str) -> dict:
    return _sf_json(["org", "display", "--target-org", alias])


def is_sandbox(alias: str) -> bool:
    """Fail-safe : une org introuvable ou ambigue est traitee comme PRODUCTION."""
    result = _sf_json(["org", "list"])
    for bucket in ("nonScratchOrgs", "scratchOrgs"):
        for org in result.get(bucket, []):
            if alias in (org.get("alias"), org.get("username")):
                flag = org.get("isSandbox")
                return bool(flag) if flag is not None else False
    return False


def get_connection(alias: str, api_version: str | None = None) -> Salesforce:
    info = org_display(alias)
    if not info.get("instanceUrl"):
        raise AuthError(f"Org '{alias}' non connectee. Relancer `sf org login web --alias {alias}`.")
    token = _sf_json(["org", "auth", "show-access-token", "--target-org", alias]).get("accessToken")
    if not token:
        raise AuthError(f"Org '{alias}' : accessToken manquant dans la reponse `sf`.")
    return Salesforce(instance_url=info["instanceUrl"], session_id=token,
                      version=api_version or info.get("apiVersion") or "62.0")


def describe_org(alias: str) -> dict:
    info = org_display(alias)
    return {"alias": alias, "username": info.get("username"), "orgId": info.get("id"),
            "instanceUrl": info.get("instanceUrl"), "isSandbox": is_sandbox(alias),
            "connectedStatus": info.get("connectedStatus")}
