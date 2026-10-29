"""Journal d'execution : une ligne JSON par evenement (`logs/journal.jsonl`)."""

import json
from datetime import datetime, timezone

from sfdata.config import LOGS_DIR


def append(event: str, **fields) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    line = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    with (LOGS_DIR / "journal.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
