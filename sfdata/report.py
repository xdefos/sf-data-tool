"""Rapport CSV par objet et par run (`state/run_<ts>_<objet>.csv`) : une ligne par record."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from sfdata.config import STATE_DIR
from sfdata.load import LoadResult


@dataclass
class Summary:
    key: str
    total: int
    ok: int
    errors: int
    created: int
    updated: int
    path: Path


def write_report(key: str, results: list[LoadResult], run_ts: str, state_dir: Path = STATE_DIR) -> Summary:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"run_{run_ts}_{key}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["external_id", "success", "created", "target_id", "errors", "empty_fields"])
        for r in results:
            w.writerow([r.external_id, r.success, r.created, r.target_id, " | ".join(r.errors), ",".join(r.empty_fields)])
    ok = sum(1 for r in results if r.success)
    created = sum(1 for r in results if r.success and r.created)
    return Summary(key, len(results), ok, len(results) - ok, created, ok - created, path)


def print_summary(s: Summary) -> str:
    return (f"[{s.key}] {s.total} record(s) : {s.ok} OK ({s.created} crees, {s.updated} mis a jour), "
            f"{s.errors} en erreur -> {s.path}")
