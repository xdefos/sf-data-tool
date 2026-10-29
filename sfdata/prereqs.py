"""Prerequis de la cible (lecture seule) : objets/champs presents, utilisateurs d'integration declares."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Check:
    ok: bool
    label: str
    detail: str = ""


def run_checks(sf_target, mappings) -> list[Check]:
    out = []
    for m in mappings:
        try:
            d = getattr(sf_target, m.target_object).describe()
        except Exception as exc:
            out.append(Check(False, f"{m.key}: objet {m.target_object}", str(exc)[:120]))
            continue
        names = {f["name"] for f in d["fields"]}
        written = {f.target for f in m.fields if f.constant is None or f.target} | {lk.target for lk in m.lookups}
        written |= set(m.constants)
        if not m.track_target_id:
            written.add(m.external_id_field)
        missing = sorted(w for w in written if w not in names)
        out.append(Check(not missing, f"{m.key}: champs ecrits presents sur {m.target_object}",
                         f"manquants : {', '.join(missing)}" if missing else ""))
        for p in m.prerequisites:
            if p.get("type") == "user_active":
                rows = sf_target.query(f"SELECT Id FROM User WHERE Id = '{p['id']}' AND IsActive = true")["records"]
                out.append(Check(bool(rows), f"{m.key}: {p.get('label', p['id'])}"))
    return out


def format_checks(checks: list[Check]) -> str:
    return "\n".join(f"  [{'OK' if c.ok else 'KO'}] {c.label}" + (f" ({c.detail})" if c.detail else "") for c in checks)
