import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class MemoryError(Exception):
    pass


class Memory:
    """One row per Gmail message id. The event id and the doc link live on that row."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                route TEXT NOT NULL,
                disposition TEXT NOT NULL,
                event_id TEXT,
                doc_link TEXT,
                recorded_at TEXT NOT NULL
            )
            """
        )
        self._db.execute("CREATE TABLE IF NOT EXISTS gmail_checkpoints (account TEXT PRIMARY KEY, history_id TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS doc_replies (message_id TEXT PRIMARY KEY, doc_link TEXT NOT NULL, state TEXT NOT NULL, sent_id TEXT)")
        self._db.commit()

    def get(self, message_id: str) -> dict | None:
        row = self._db.execute(
            "SELECT message_id, route, disposition, event_id, doc_link, recorded_at FROM messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def record(
        self,
        message_id: str,
        route: str,
        disposition: str,
        event_id: str | None = None,
        doc_link: str | None = None,
    ) -> None:
        recorded_at = datetime.now(timezone.utc).isoformat()
        try:
            self._db.execute(
                """
                INSERT INTO messages (message_id, route, disposition, event_id, doc_link, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (message_id, route, disposition, event_id, doc_link, recorded_at),
            )
        except sqlite3.IntegrityError as exc:
            raise MemoryError(f"{message_id} is already recorded.") from exc
        if route == "article" and disposition == "dispatch" and doc_link:
            self._db.execute("INSERT OR IGNORE INTO doc_replies VALUES (?, ?, 'pending', NULL)", (message_id, doc_link))
        self._db.commit()

    def queue_doc_reply(self, message_id: str) -> None:
        row = self.get(message_id)
        if not row or row["route"] != "article" or not row["doc_link"]:
            raise MemoryError("No saved article document for this message.")
        self._db.execute("INSERT OR IGNORE INTO doc_replies VALUES (?, ?, 'pending', NULL)", (message_id, row["doc_link"]))
        self._db.commit()

    def pending_replies(self) -> list[dict]:
        return [dict(row) for row in self._db.execute("SELECT * FROM doc_replies WHERE state='pending'")]

    def reply_state(self, message_id: str, state: str, sent_id: str | None = None) -> None:
        self._db.execute("UPDATE doc_replies SET state=?, sent_id=? WHERE message_id=?", (state, sent_id, message_id))
        self._db.commit()

    def checkpoint(self, account: str) -> str | None:
        row = self._db.execute("SELECT history_id FROM gmail_checkpoints WHERE account=?", (account,)).fetchone()
        return row[0] if row else None

    def set_checkpoint(self, account: str, history_id: str) -> None:
        self._db.execute("INSERT INTO gmail_checkpoints VALUES (?,?) ON CONFLICT(account) DO UPDATE SET history_id=excluded.history_id", (account, history_id))
        self._db.commit()

    def close(self) -> None:
        self._db.close()
