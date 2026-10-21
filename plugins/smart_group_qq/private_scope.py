"""Read only routing identity metadata when proving legacy private ownership."""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import sqlite3


def legacy_evidence(gateway, chat_id):
    evidence = {"authenticated_private": True, "sessions": []}
    sessions = getattr(gateway, "session_store", None)
    home = getattr(sessions, "_routing_home", None)
    if home is None:
        directory = getattr(sessions, "sessions_dir", None)
        home = Path(directory).parent if directory is not None else None
    if home is None:
        return evidence
    path = Path(home) / "state.db"
    if not path.is_file():
        return evidence
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT source,chat_id,user_id,chat_type,origin_json FROM sessions "
                "WHERE source='qqbot' AND (chat_id=? OR "
                "CASE WHEN json_valid(origin_json) THEN json_extract(origin_json,'$.chat_id') END=?)",
                (str(chat_id), str(chat_id)),
            ).fetchall()
        for row in rows:
            try:
                origin = json.loads(row["origin_json"] or "{}")
            except (ValueError, TypeError):
                origin = {}
            if not isinstance(origin, dict):
                origin = {}
            evidence["sessions"].append({
                key: str(row[key] or origin.get(key) or "")
                for key in ("source", "chat_id", "user_id", "chat_type")
            })
    except (sqlite3.Error, OSError):
        pass  # Missing provenance must never authorize a guessed migration.
    return evidence
