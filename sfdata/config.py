"""Configuration : mappings YAML par objet + niveaux de sequencement.

Aucun credential ici : les orgs sont resolues via `sfdata.auth` (CLI `sf`). Ce module ne connait que
les fichiers YAML sous `mappings/` et le dossier des donnees d'execution (hors depot git).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT_DIR / "mappings"
LEVELS_FILE = MAPPINGS_DIR / "_levels.yaml"
USERS_FILE = MAPPINGS_DIR / "_users.yaml"
# Donnees d'execution (rapports, watermarks, maps d'identifiants, journaux, snapshots) : JAMAIS dans
# le depot git. Surchargeable via SFDATA_DIR (defaut ~/.sf-data-tool). Elles contiennent des donnees clients.
DATA_DIR = Path(os.environ.get("SFDATA_DIR") or Path.home() / ".sf-data-tool").expanduser()
STATE_DIR = DATA_DIR / "state"
LOGS_DIR = DATA_DIR / "logs"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
ANALYSE_DIR = DATA_DIR / "analyse"


@dataclass
class FieldMapping:
    source: str
    target: str
    transcode: dict | None = None
    rule: str | None = None
    only_if: str | None = None   # champ (ou `champ=valeur`) : n'ecrit que si la condition est vraie
    unless: str | None = None    # inverse de only_if
    drop_values: list = field(default_factory=list)  # valeurs source ignorees (champ omis), sans bloquer
    append_suffix: str | None = None
    anonymize: bool = False      # suffixe applique seulement avec l'anonymisation active (defaut)
    constant: object = None      # valeur fixe ecrite a la place de la source quand la condition est vraie
    truncate_at: int | None = None
    default: object = None       # valeur ecrite quand la source est vide (champ obligatoire cible)
    default_unique_base: int | None = None  # idem, entier unique derive de l'Id source (base + base62 des 6 derniers car.)


@dataclass
class LookupMapping:
    source: str                  # FK source (ex. Enquete__c) ; avec `user`, le chemin du username (Owner.Username)
    target: str                  # champ lookup cible
    parent_external_id: str = "ExtId__c"
    parent_key: str | None = None   # cle de mapping du parent (graphe de dependances, purge)
    reflexive: bool = False      # resolu au pass 2 (self-lookup, cycle)
    relationship_name: str | None = None
    only_if: str | None = None
    unless: str | None = None
    prefer: str | None = None    # chemin source de l'identifiant externe du parent (Enquete__r.ExtId__c)
    compute: dict | None = None  # {prefix}: repli `<prefix>_<FK>` quand `prefer` est vide (cf. external_id_compute)
    user: bool = False           # lookup vers User : resolu par username (mappings/_users.yaml)
    via_idmap: bool = False      # parent sans identifiant externe : Id cible lu dans l'idmap local
    backfill_object: str | None = None  # ExternalId genere cote cible, lu dans la map backfill de ce parent


@dataclass
class ObjectMapping:
    key: str
    source_object: str
    target_object: str
    level: str
    external_id_field: str
    scope_filter: str | None = None
    two_pass: bool = False
    fields: list = field(default_factory=list)
    constants: dict = field(default_factory=dict)
    lookups: list = field(default_factory=list)
    external_id_compute: dict | None = None  # {source, prefix, id_length, prefer}
    external_id_backfill: bool = False
    insert_only: bool = False
    track_target_id: bool = False
    files: bool = False          # copie aussi les fichiers lies (ContentDocumentLink)
    prerequisites: list = field(default_factory=list)
    select_extra: list = field(default_factory=list)
    batch_size: int | None = None

    @classmethod
    def from_dict(cls, key: str, d: dict) -> "ObjectMapping":
        fields_ = [FieldMapping(**{k: v for k, v in f.items() if k in FieldMapping.__dataclass_fields__})
                   for f in d.get("fields") or []]
        lookups = [LookupMapping(**{k: v for k, v in lk.items() if k in LookupMapping.__dataclass_fields__})
                   for lk in d.get("lookups") or []]
        return cls(
            key=key, source_object=d["source_object"], target_object=d["target_object"],
            level=str(d["level"]), external_id_field=d["external_id_field"],
            scope_filter=d.get("scope_filter"), two_pass=bool(d.get("two_pass", False)),
            fields=fields_, constants=d.get("constants") or {}, lookups=lookups,
            external_id_compute=d.get("external_id_compute"),
            external_id_backfill=bool(d.get("external_id_backfill", False)),
            insert_only=bool(d.get("insert_only", False)),
            track_target_id=bool(d.get("track_target_id", False)),
            files=bool(d.get("files", False)),
            prerequisites=list(d.get("prerequisites") or []),
            select_extra=list(d.get("select_extra") or []), batch_size=d.get("batch_size"),
        )


def load_levels(levels_file: Path = LEVELS_FILE) -> list[dict]:
    if not levels_file.exists():
        return []
    data = yaml.safe_load(levels_file.read_text(encoding="utf-8")) or {}
    return sorted(data.get("levels", []), key=lambda lvl: lvl["order"])


def load_object_mapping(key: str, mappings_dir: Path = MAPPINGS_DIR) -> ObjectMapping:
    path = mappings_dir / f"{key}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Mapping introuvable pour '{key}' : {path}")
    return ObjectMapping.from_dict(key, yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def list_available_mappings(mappings_dir: Path = MAPPINGS_DIR) -> list[str]:
    return sorted(p.stem for p in mappings_dir.glob("*.yaml") if not p.stem.startswith("_"))


def load_users_map(users_file: Path = USERS_FILE) -> dict:
    """`_users.yaml` : {map: {username_source: username_cible}, suffix: ".xxx", default: username|null}."""
    if not users_file.exists():
        return {}
    return yaml.safe_load(users_file.read_text(encoding="utf-8")) or {}
