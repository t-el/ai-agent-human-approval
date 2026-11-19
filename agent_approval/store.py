"""SQLite state: runs, an append-only hash-chained audit log, dead letters and the sent-message ledger."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, message_id TEXT UNIQUE, status TEXT, inbound TEXT, draft TEXT, findings TEXT,
                tools TEXT, final_reply TEXT, actor TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts TEXT, event TEXT, actor TEXT, data TEXT,
                prev_hash TEXT, hash TEXT);
            CREATE TABLE IF NOT EXISTS dead_letters (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, kind TEXT, error TEXT, status TEXT DEFAULT 'open',
                created_at TEXT);
            CREATE TABLE IF NOT EXISTS sent (
                idempotency_key TEXT PRIMARY KEY, provider_id TEXT, sent_at TEXT);
        """)

    # runs -------------------------------------------------------------------------------------
    def create_run(self, message_id: str, inbound: dict) -> str | None:
        """Returns a new run id, or None if this message was already handled (idempotent intake)."""
        rid = "run_" + secrets.token_hex(4)
        with self.lock, self.db:
            cur = self.db.execute("INSERT OR IGNORE INTO runs (id, message_id, status, inbound, created_at, updated_at)"
                                  " VALUES (?,?,?,?,?,?)", (rid, message_id, "received", json.dumps(inbound),
                                                            now_iso(), now_iso()))
        return rid if cur.rowcount == 1 else None

    def update_run(self, rid: str, **fields) -> None:
        enc = {k: json.dumps(v) if k in {"draft", "findings", "tools", "inbound"} else v for k, v in fields.items()}
        cols = ", ".join(f"{k}=?" for k in enc)
        with self.lock, self.db:
            self.db.execute(f"UPDATE runs SET {cols}, updated_at=? WHERE id=?", (*enc.values(), now_iso(), rid))

    def get_run(self, rid: str) -> dict | None:
        r = self.db.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        return self._decode(r) if r else None

    def run_by_message(self, message_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM runs WHERE message_id=?", (message_id,)).fetchone()
        return self._decode(r) if r else None

    def list_runs(self, status: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM runs", ()
        if status:
            q, a = q + " WHERE status=?", (status,)
        return [self._decode(r) for r in self.db.execute(q + " ORDER BY created_at, rowid", a)]

    @staticmethod
    def _decode(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in ("inbound", "draft", "findings", "tools"):
            d[k] = json.loads(d[k]) if d[k] else ({} if k in ("inbound", "draft") else [])
        return d

    # audit (tamper-evident) ------------------------------------------------------------------
    def audit(self, run_id: str, event: str, data: dict | None = None, actor: str = "system") -> None:
        with self.lock, self.db:
            last = self.db.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
            prev = last["hash"] if last else "genesis"
            ts, payload = now_iso(), json.dumps(data or {}, sort_keys=True, default=str)
            h = hashlib.sha256(f"{prev}|{run_id}|{ts}|{event}|{actor}|{payload}".encode()).hexdigest()
            self.db.execute("INSERT INTO audit (run_id, ts, event, actor, data, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
                            (run_id, ts, event, actor, payload, prev, h))

    def audit_for(self, run_id: str) -> list[dict]:
        return [dict(r) | {"data": json.loads(r["data"])}
                for r in self.db.execute("SELECT * FROM audit WHERE run_id=? ORDER BY id", (run_id,))]

    def verify_audit(self) -> tuple[bool, int | None]:
        """Recompute the chain. Any edited or deleted row breaks it from that point on."""
        prev = "genesis"
        for r in self.db.execute("SELECT * FROM audit ORDER BY id"):
            h = hashlib.sha256(f"{prev}|{r['run_id']}|{r['ts']}|{r['event']}|{r['actor']}|{r['data']}".encode()).hexdigest()
            if r["prev_hash"] != prev or r["hash"] != h:
                return False, r["id"]
            prev = r["hash"]
        return True, None

    # dead letters -----------------------------------------------------------------------------
    def dead_letter(self, run_id: str, kind: str, error: str) -> int:
        with self.lock, self.db:
            return int(self.db.execute("INSERT INTO dead_letters (run_id, kind, error, created_at) VALUES (?,?,?,?)",
                                       (run_id, kind, error, now_iso())).lastrowid)

    def dead_letters(self, status: str = "open") -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM dead_letters WHERE status=? ORDER BY id", (status,))]

    def resolve_dead_letter(self, dlq_id: int) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE dead_letters SET status='resolved' WHERE id=?", (dlq_id,))

    # sent ledger (exactly-once sending) ------------------------------------------------------
    def already_sent(self, key: str) -> str | None:
        r = self.db.execute("SELECT provider_id FROM sent WHERE idempotency_key=?", (key,)).fetchone()
        return r["provider_id"] if r else None

    def mark_sent(self, key: str, provider_id: str) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO sent VALUES (?,?,?)", (key, provider_id, now_iso()))
