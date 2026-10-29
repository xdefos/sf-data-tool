"""Fichiers lies (ContentDocumentLink) : lecture depuis une org, ecriture dans une org, dossier de snapshot.

On copie la derniere version de chaque fichier. Un fichier partage par plusieurs records n'est cree qu'une
fois (map `files_<cible>.json`, ContentDocument source -> cible) puis lie aux autres parents.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from simple_salesforce import Salesforce

from sfdata.load import chunks


def _in(ids) -> str:
    return ",".join(f"'{i}'" for i in ids)


def fetch_files(sf: Salesforce, parent_ids: list[str], dest: Path | None = None) -> list[dict]:
    """[{doc, version, title, path, parents:[Id source], size, data(bytes) | file(nom dans dest)}]."""
    links: dict[str, list] = {}
    for part in chunks(parent_ids, 200):
        for r in sf.query_all("SELECT LinkedEntityId, ContentDocumentId FROM ContentDocumentLink "
                              f"WHERE LinkedEntityId IN ({_in(part)})")["records"]:
            links.setdefault(r["ContentDocumentId"], []).append(r["LinkedEntityId"])
    out = []
    for part in chunks(sorted(links), 200):
        for v in sf.query_all("SELECT Id, ContentDocumentId, Title, PathOnClient, ContentSize FROM ContentVersion "
                              f"WHERE IsLatest = true AND ContentDocumentId IN ({_in(part)})")["records"]:
            res = sf.session.get(f"{sf.base_url}sobjects/ContentVersion/{v['Id']}/VersionData",
                                 headers=sf.headers)
            res.raise_for_status()
            item = {"doc": v["ContentDocumentId"], "version": v["Id"], "title": v["Title"],
                    "path": v["PathOnClient"] or v["Title"], "size": v["ContentSize"],
                    "parents": links[v["ContentDocumentId"]]}
            if dest is not None:
                dest.mkdir(parents=True, exist_ok=True)
                (dest / v["Id"]).write_bytes(res.content)
                item["file"] = v["Id"]
            else:
                item["data"] = res.content
            out.append(item)
    return out


def push_file(sf: Salesforce, item: dict, data: bytes, first_parent: str) -> str:
    """Cree le fichier (publie sur le premier parent) et retourne le ContentDocumentId cible."""
    r = sf.ContentVersion.create({"Title": item["title"], "PathOnClient": item["path"],
                                  "VersionData": base64.b64encode(data).decode(),
                                  "FirstPublishLocationId": first_parent})
    return sf.query(f"SELECT ContentDocumentId FROM ContentVersion WHERE Id = '{r['id']}'")["records"][0]["ContentDocumentId"]


def link_file(sf: Salesforce, doc_id: str, parent_id: str) -> None:
    sf.ContentDocumentLink.create({"ContentDocumentId": doc_id, "LinkedEntityId": parent_id, "ShareType": "V"})


def write_index(dest: Path, items: list[dict]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    meta = [{k: v for k, v in i.items() if k != "data"} for i in items]
    (dest / "index.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
