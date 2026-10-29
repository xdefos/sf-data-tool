"""Resolution des utilisateurs (OwnerId...) : username source -> Id User cible.

Ordre : table explicite `_users.yaml` (map) > meme username + `suffix` (les sandbox suffixent les usernames) >
`default` (username cible de repli) > omis (la cible met l'utilisateur d'API). Charge a la demande.
"""

from __future__ import annotations

from sfdata.config import load_users_map


class UserResolver:
    def __init__(self, sf_target, cfg: dict | None = None):
        self.sf, self.cfg = sf_target, cfg if cfg is not None else load_users_map()
        self._cache: dict[str, str | None] = {}
        self.unresolved: set[str] = set()

    def _lookup(self, username: str) -> str | None:
        esc = username.replace("'", "\\'")
        rows = self.sf.query(f"SELECT Id FROM User WHERE Username = '{esc}' AND IsActive = true")["records"]
        return rows[0]["Id"] if rows else None

    def __call__(self, username: str) -> str | None:
        if username in self._cache:
            return self._cache[username]
        mapped = (self.cfg.get("map") or {}).get(username)
        uid = None
        for candidate in (mapped, username + (self.cfg.get("suffix") or ""), self.cfg.get("default")):
            if candidate:
                uid = self._lookup(candidate)
                if uid:
                    break
        if not uid:
            self.unresolved.add(username)
        self._cache[username] = uid
        return uid
