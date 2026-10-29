"""Ordonnancement des objets par niveau : un niveau attend la fin du precedent.

Le niveau de chaque objet est le champ `level` de son mapping (calcule par `init-mappings` depuis le
graphe des relations, modifiable a la main). `_levels.yaml` ne sert qu'a nommer les niveaux.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from sfdata.config import LEVELS_FILE, MAPPINGS_DIR, list_available_mappings, load_levels, load_object_mapping


class SequencerError(Exception):
    pass


@dataclass
class LevelPlan:
    order: int
    name: str
    objects: list  # cles de mapping, triees


def build_sequence(mappings_dir: Path = MAPPINGS_DIR, levels_file: Path = LEVELS_FILE) -> list[LevelPlan]:
    names = {lvl["order"]: lvl.get("name", "") for lvl in load_levels(levels_file)}
    by_level: dict[int, list] = defaultdict(list)
    for key in list_available_mappings(mappings_dir):
        m = load_object_mapping(key, mappings_dir)
        try:
            by_level[int(m.level)].append(key)
        except ValueError as exc:
            raise SequencerError(f"Mapping '{key}' : niveau '{m.level}' non entier") from exc
    return [LevelPlan(o, names.get(o) or f"Niveau {o}", sorted(by_level[o])) for o in sorted(by_level)]


def flatten(plan: list[LevelPlan]) -> list[tuple[int, str]]:
    return [(lvl.order, k) for lvl in plan for k in lvl.objects]
