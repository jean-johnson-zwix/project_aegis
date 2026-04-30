"""
buffer.py — SQLite-backed offline buffer for the Greengrass publisher.

Design per BUILD.md §8:
  - WAL mode + NORMAL sync: durable under OS crashes, performant.
  - Table `queue`: pending readings, ordered by insertion (id ASC = oldest first).
  - Table `dead_letter`: readings that exhausted all retry attempts (MAX_ATTEMPTS=10).
  - drain(): publishes oldest-first; on success deletes the row; on failure
    increments attempts; at MAX_ATTEMPTS moves the row to dead_letter and logs loudly.

Offline survival test:
  Disable network → buffer.count() climbs.
  Restore network → drain() publishes all buffered readings → count returns to 0.
  See docs/offline-test.md for the recorded test procedure.
"""

import json
import logging
import sqlite3
import time
from typing import Any, Callable

log = logging.getLogger("buffer")

MAX_ATTEMPTS: int = 10

_DDL_QUEUE = """
CREATE TABLE IF NOT EXISTS queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    inserted_at  TEXT    NOT NULL,
    topic        TEXT    NOT NULL,
    payload      TEXT    NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0
);
"""

_DDL_DEAD_LETTER = """
CREATE TABLE IF NOT EXISTS dead_letter (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    inserted_at  TEXT    NOT NULL,
    topic        TEXT    NOT NULL,
    payload      TEXT    NOT NULL,
    attempts     INTEGER NOT NULL,
    failed_at    TEXT    NOT NULL
);
"""


class Buffer:
    """
    Thread-safe (connection-per-call) SQLite offline buffer.

    Designed for single-threaded use — the publisher main loop calls enqueue()
    and drain() sequentially, never concurrently. The sqlite3 connection is
    shared within the same thread.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(_DDL_QUEUE)
        self._conn.execute(_DDL_DEAD_LETTER)
        self._conn.commit()
        log.info("Buffer initialised at %s", db_path)

    # -------------------------------------------------------------------------
    # Public interface
    # -------------------------------------------------------------------------

    def enqueue(self, reading: dict[str, Any], topic: str) -> None:
        """Insert one reading into the queue."""
        now = _utcnow()
        self._conn.execute(
            "INSERT INTO queue (inserted_at, topic, payload, attempts) VALUES (?, ?, ?, 0)",
            (now, topic, json.dumps(reading)),
        )
        self._conn.commit()
        log.debug("Enqueued reading for %s (queue=%d)", topic, self.count())

    def drain(
        self,
        ipc_client: Any,
        publish_fn: Callable[[Any, str, dict], bool],
        max_n: int = 100,
    ) -> int:
        """
        Attempt to publish up to max_n buffered readings, oldest first.

        Args:
            ipc_client:  Greengrass IPC client (passed through to publish_fn).
            publish_fn:  Callable(ipc_client, topic, payload) → bool.
            max_n:       Maximum readings to attempt in one drain pass.

        Returns:
            Count of readings successfully published and removed from queue.
        """
        rows = self._conn.execute(
            "SELECT id, topic, payload, attempts FROM queue ORDER BY id ASC LIMIT ?",
            (max_n,),
        ).fetchall()

        if not rows:
            return 0

        published = 0
        now = _utcnow()

        for row_id, topic, payload_str, attempts in rows:
            try:
                payload = json.loads(payload_str)
            except json.JSONDecodeError:
                log.error("Dead-lettering row %d: invalid JSON payload", row_id)
                self._dead_letter(row_id, topic, payload_str, attempts + 1, now)
                continue

            if publish_fn(ipc_client, topic, payload):
                self._conn.execute("DELETE FROM queue WHERE id=?", (row_id,))
                self._conn.commit()
                published += 1
            else:
                new_attempts = attempts + 1
                if new_attempts >= MAX_ATTEMPTS:
                    log.error(
                        "Dead-lettering row %d after %d failed attempts (topic=%s)",
                        row_id, new_attempts, topic,
                    )
                    self._dead_letter(row_id, topic, payload_str, new_attempts, now)
                else:
                    self._conn.execute(
                        "UPDATE queue SET attempts=? WHERE id=?",
                        (new_attempts, row_id),
                    )
                    self._conn.commit()

        return published

    def count(self) -> int:
        """Number of readings currently pending in the queue."""
        row = self._conn.execute("SELECT COUNT(*) FROM queue").fetchone()
        return row[0] if row else 0

    def dead_letter_count(self) -> int:
        """Number of readings that exhausted all retry attempts."""
        row = self._conn.execute("SELECT COUNT(*) FROM dead_letter").fetchone()
        return row[0] if row else 0

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _dead_letter(
        self,
        row_id: int,
        topic: str,
        payload_str: str,
        attempts: int,
        failed_at: str,
    ) -> None:
        orig = self._conn.execute(
            "SELECT inserted_at FROM queue WHERE id=?", (row_id,)
        ).fetchone()
        inserted_at = orig[0] if orig else failed_at

        self._conn.execute(
            "INSERT INTO dead_letter (inserted_at, topic, payload, attempts, failed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (inserted_at, topic, payload_str, attempts, failed_at),
        )
        self._conn.execute("DELETE FROM queue WHERE id=?", (row_id,))
        self._conn.commit()


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
