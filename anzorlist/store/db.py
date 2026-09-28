"""The submission ledger.

Every listing built and every submission attempted is recorded in SQLite, with the exact payload
that was sent. This exists for three reasons, in order of how much they hurt when missing:

1. **Rollback.** When a bad batch goes live, you need the list of SKUs that were actually
   created, in which marketplaces, at what price — not an approximation reconstructed from logs.
2. **Idempotency.** A listing whose payload hash is unchanged does not need resubmitting. On a
   catalog of thousands of SKUs across several marketplaces, re-running a job should be nearly
   free and should not burn SP-API quota re-sending identical payloads.
3. **Answering "why is this listing like that".** Six months on, the question is always which
   run produced a listing, from which source page, with what copy. The `source_content_hash`
   ties a live listing back to the exact version of the web page it came from.

SQLite is the right tool here: single-file, no daemon, transactional, and trivially copyable
for backup. The ledger is append-mostly — submissions are never updated in place, so the history
of a SKU is the full sequence of its rows.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

import structlog

from anzorlist.models.listing import BuiltListing, SubmissionOutcome

log = structlog.get_logger(__name__)

SCHEMA_VERSION = 1

SubmissionState = Literal["new", "changed", "accepted", "in_flight", "failed"]


class SubmissionKey(Protocol):
    """What the ledger needs to know about any channel's listing to decide what to resend."""

    @property
    def sku(self) -> str: ...

    @property
    def marketplace_id(self) -> str: ...

    @property
    def payload_hash(self) -> str: ...


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per (sku, marketplace) build. Replaced when the payload changes.
CREATE TABLE IF NOT EXISTS listings (
    sku                 TEXT NOT NULL,
    marketplace_id      TEXT NOT NULL,
    source_sku          TEXT NOT NULL,
    parent_sku          TEXT,
    product_type        TEXT NOT NULL,
    is_parent           INTEGER NOT NULL DEFAULT 0,
    payload_hash        TEXT NOT NULL,
    source_content_hash TEXT NOT NULL,
    source_url          TEXT NOT NULL,
    price               TEXT,
    currency            TEXT,
    quantity            INTEGER,
    payload_json        TEXT NOT NULL,
    issues_json         TEXT NOT NULL,
    status              TEXT NOT NULL,
    built_at            TEXT NOT NULL,
    PRIMARY KEY (sku, marketplace_id)
);

-- Append-only. Never updated; the history of a SKU is its full sequence of rows.
CREATE TABLE IF NOT EXISTS submissions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    sku            TEXT NOT NULL,
    marketplace_id TEXT NOT NULL,
    mode           TEXT NOT NULL,
    status         TEXT NOT NULL,
    submission_id  TEXT,
    request_id     TEXT,
    http_status    INTEGER,
    payload_hash   TEXT NOT NULL,
    issues_json    TEXT NOT NULL,
    submitted_at   TEXT NOT NULL,
    run_id         TEXT NOT NULL
);

-- One row per invocation, so a batch can be described, audited, or rolled back as a unit.
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    command     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    mode        TEXT NOT NULL,
    operator    TEXT,
    notes       TEXT,
    counts_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_submissions_sku ON submissions(sku, marketplace_id);
CREATE INDEX IF NOT EXISTS idx_submissions_run ON submissions(run_id);
CREATE INDEX IF NOT EXISTS idx_listings_source ON listings(source_sku);
"""


@dataclass
class LedgerEntry:
    sku: str
    marketplace_id: str
    payload_hash: str
    status: str
    built_at: str


class Ledger:
    """SQLite-backed submission ledger."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL lets a long submission run coexist with a `status` query in another terminal.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ---- runs ----

    def start_run(self, run_id: str, command: str, mode: str, operator: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO runs (run_id, command, started_at, mode, operator) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, command, _now(), mode, operator),
            )
        log.info("ledger.run_started", run_id=run_id, command=command, mode=mode)

    def finish_run(self, run_id: str, counts: dict[str, int], notes: str = "") -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE runs SET finished_at = ?, counts_json = ?, notes = ? WHERE run_id = ?",
                (_now(), json.dumps(counts), notes, run_id),
            )

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- listings ----

    def record_listing(self, listing: BuiltListing) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT OR REPLACE INTO listings
                   (sku, marketplace_id, source_sku, parent_sku, product_type, is_parent,
                    payload_hash, source_content_hash, source_url, price, currency, quantity,
                    payload_json, issues_json, status, built_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    listing.sku,
                    listing.marketplace_id,
                    listing.source_sku,
                    listing.parent_sku,
                    listing.product_type,
                    int(listing.is_parent),
                    listing.payload_hash,
                    listing.content_hash,
                    listing.source_url,
                    str(listing.offer.price) if listing.offer else None,
                    listing.offer.currency if listing.offer else None,
                    listing.offer.quantity if listing.offer else None,
                    listing.model_dump_json(),
                    json.dumps([i.model_dump(mode="json") for i in listing.issues]),
                    listing.status.value,
                    (listing.built_at or datetime.now(timezone.utc)).isoformat(),
                ),
            )

    def submission_state(self, listing: SubmissionKey) -> SubmissionState:
        """Where this exact payload stands with Amazon, from the last live write for its SKU.

        The comparison is on the payload hash, not on a timestamp: a rebuild that produces
        byte-identical attributes is genuinely a no-op, and re-sending it would consume quota
        and rewrite Amazon's `lastUpdatedDate` for no reason.
        """
        row = self._conn.execute(
            """SELECT s.payload_hash, s.status, s.mode, s.submission_id FROM submissions s
               WHERE s.sku = ? AND s.marketplace_id = ?
                 AND (s.mode = 'SUBMIT' OR (s.mode = 'DELETE' AND s.status = 'accepted'))
               ORDER BY s.id DESC LIMIT 1""",
            (listing.sku, listing.marketplace_id),
        ).fetchone()
        if row is None:
            return "new"
        if row["mode"] == "DELETE":
            return "new"  # deleted since: the same payload must be sent again to restore it
        if row["payload_hash"] != listing.payload_hash:
            return "changed"
        if row["status"] in ("submitted", "accepted", "live"):
            return "accepted"
        if row["status"] == "pending" and row["submission_id"]:
            return "in_flight"  # inside a feed that has not been reconciled
        return "failed"

    def needs_submission(self, listing: SubmissionKey) -> bool:
        """True when this exact payload is neither accepted nor already in flight."""
        return self.submission_state(listing) in ("new", "changed", "failed")

    def get_listing(self, sku: str, marketplace_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM listings WHERE sku = ? AND marketplace_id = ?", (sku, marketplace_id)
        ).fetchone()
        return dict(row) if row else None

    # ---- submissions ----

    def record_submission(self, outcome: SubmissionOutcome, run_id: str) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO submissions
                   (sku, marketplace_id, mode, status, submission_id, request_id, http_status,
                    payload_hash, issues_json, submitted_at, run_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    outcome.sku,
                    outcome.marketplace_id,
                    outcome.mode,
                    outcome.status.value,
                    outcome.submission_id,
                    outcome.request_id,
                    outcome.http_status,
                    outcome.payload_hash,
                    json.dumps([i.model_dump(mode="json") for i in outcome.issues]),
                    (outcome.submitted_at or datetime.now(timezone.utc)).isoformat(),
                    run_id,
                ),
            )
            c.execute(
                "UPDATE listings SET status = ? WHERE sku = ? AND marketplace_id = ?",
                (outcome.status.value, outcome.sku, outcome.marketplace_id),
            )

    def submissions_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM submissions WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    _LIVE_SQL = """
        SELECT s.sku, s.marketplace_id, s.payload_hash, s.status, s.submitted_at AS built_at
        FROM submissions s
        WHERE s.mode = 'SUBMIT' AND s.status IN ('submitted','accepted','live')
          AND s.id = (
            SELECT MAX(a.id) FROM submissions a
            WHERE a.sku = s.sku AND a.marketplace_id = s.marketplace_id
              AND a.mode = 'SUBMIT' AND a.status IN ('submitted','accepted','live')
          )
          AND NOT EXISTS (
            SELECT 1 FROM submissions d
            WHERE d.sku = s.sku AND d.marketplace_id = s.marketplace_id
              AND d.mode = 'DELETE' AND d.status = 'accepted' AND d.id > s.id
          )
    """

    def live_skus(self, marketplace_id: str | None = None) -> list[LedgerEntry]:
        """Every SKU believed to exist in a marketplace, on any channel. This is the rollback list.

        Derived from the write history, not from ``listings.status``: that column records the
        latest event of any kind, so a failed preview, an in-flight feed, or a rejected *update*
        to a live listing would otherwise drop a listing that is still live from this list.
        A SKU is live when it has an accepted SUBMIT and no later accepted deletion.
        """
        sql = self._LIVE_SQL
        params: tuple[Any, ...] = ()
        if marketplace_id:
            sql += " AND s.marketplace_id = ?"
            params = (marketplace_id,)
        rows = self._conn.execute(sql + " ORDER BY s.sku", params).fetchall()
        return [LedgerEntry(**dict(r)) for r in rows]

    def is_live(self, sku: str, marketplace_id: str) -> bool:
        """Whether this SKU exists in the marketplace, by the same rule as :meth:`live_skus`,
        whatever payload was last accepted for it."""
        row = self._conn.execute(
            self._LIVE_SQL + " AND s.sku = ? AND s.marketplace_id = ?", (sku, marketplace_id)
        ).fetchone()
        return row is not None

    def status_summary(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM listings GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def history(self, sku: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM submissions WHERE sku = ? ORDER BY id", (sku,)
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id(command: str) -> str:
    """A unique, sortable run id. Reusing one would overwrite an earlier run's audit record."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{command}-{stamp}-{secrets.token_hex(3)}"
