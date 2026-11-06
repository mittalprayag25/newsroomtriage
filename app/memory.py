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
        self._db.commit()

    def close(self) -> None:
        self._db.close()
