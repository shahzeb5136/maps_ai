"""
Postgres access.

This service shares the website's database. It *reads and decrements*
`users.credits` — a table the Next.js app owns — and it *owns* `property_scans`
outright. The DDL below creates `users` only defensively, with exactly the
website's shape, so a cold database doesn't 500 on the first charge.
"""

import json
import logging
import urllib.parse
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import asyncpg

from . import config

log = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


def _dumps(value: Any) -> str:
    """JSON for a jsonb column. Postgres refuses \\u0000 in jsonb, and model
    text can carry a stray NUL; letting it through would fail a finished scan
    at the last step."""
    return json.dumps(value).replace("\\u0000", "")

# Statuses a scan can hold. `queued` exists for the brief window between the
# credit being taken and the worker picking the job up.
QUEUED, RUNNING, COMPLETED, FAILED = "queued", "running", "completed", "failed"
ACTIVE_STATUSES = (QUEUED, RUNNING)


def _ssl_mode(dsn: str):
    """Railway's Postgres presents a self-signed cert: encrypt, don't verify.
    Local and intra-project connections don't need TLS at all."""
    host = (urllib.parse.urlparse(dsn).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".internal"):
        return False
    return "require"


async def connect() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            config.DATABASE_URL,
            ssl=_ssl_mode(config.DATABASE_URL),
            min_size=1,
            max_size=5,
            command_timeout=30,
        )
        await _migrate(_pool)
    return _pool


async def disconnect() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialised")
    return _pool


async def _migrate(p: asyncpg.Pool) -> None:
    async with p.acquire() as con:
        # Mirrors the website's schema exactly. Already-existing installs skip it.
        await con.execute("""
            CREATE TABLE IF NOT EXISTS users (
              id                   TEXT PRIMARY KEY,
              credits              INTEGER NOT NULL DEFAULT 0,
              email                TEXT,
              created_at           TEXT NOT NULL,
              signup_bonus_granted BOOLEAN NOT NULL DEFAULT FALSE
            )
        """)
        await con.execute("""
            CREATE TABLE IF NOT EXISTS property_scans (
              id            TEXT PRIMARY KEY,
              user_id       TEXT NOT NULL,
              address       TEXT NOT NULL,
              status        TEXT NOT NULL,
              stage         TEXT,
              stage_detail  TEXT,
              credits_spent INTEGER NOT NULL DEFAULT 0,
              owner_photos  INTEGER NOT NULL DEFAULT 0,
              refunded      BOOLEAN NOT NULL DEFAULT FALSE,
              error_message TEXT,
              result_json   JSONB,
              created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
              started_at    TIMESTAMPTZ,
              completed_at  TIMESTAMPTZ
            )
        """)
        # Added after launch, so existing deployments need it backfilled.
        await con.execute("""
            ALTER TABLE property_scans
            ADD COLUMN IF NOT EXISTS owner_photos INTEGER NOT NULL DEFAULT 0
        """)
        await con.execute("""
            CREATE INDEX IF NOT EXISTS property_scans_user_created_idx
            ON property_scans (user_id, created_at DESC)
        """)
        # The inspection layer: what the scan is for, the makeover style, and
        # the live per-photo feed the browser polls while a scan runs.
        await con.execute("""
            ALTER TABLE property_scans
            ADD COLUMN IF NOT EXISTS goal TEXT,
            ADD COLUMN IF NOT EXISTS style TEXT,
            ADD COLUMN IF NOT EXISTS live_json JSONB
        """)
        # Questions put to a finished scan, and studio renders made from it.
        # Both cascade with the scan, whose delete also removes the files.
        await con.execute("""
            CREATE TABLE IF NOT EXISTS property_scan_questions (
              id           TEXT PRIMARY KEY,
              scan_id      TEXT NOT NULL REFERENCES property_scans(id) ON DELETE CASCADE,
              user_id      TEXT NOT NULL,
              question     TEXT NOT NULL,
              status       TEXT NOT NULL,
              answer_json  JSONB,
              error_message TEXT,
              created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        await con.execute("""
            CREATE INDEX IF NOT EXISTS property_scan_questions_scan_idx
            ON property_scan_questions (scan_id, created_at)
        """)
        await con.execute("""
            CREATE TABLE IF NOT EXISTS property_scan_renders (
              id            TEXT PRIMARY KEY,
              scan_id       TEXT NOT NULL REFERENCES property_scans(id) ON DELETE CASCADE,
              user_id       TEXT NOT NULL,
              source        TEXT NOT NULL,
              mode          TEXT NOT NULL,
              style         TEXT,
              instruction   TEXT,
              status        TEXT NOT NULL,
              file          TEXT,
              error_message TEXT,
              created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
              completed_at  TIMESTAMPTZ
            )
        """)
        await con.execute("""
            CREATE INDEX IF NOT EXISTS property_scan_renders_scan_idx
            ON property_scan_renders (scan_id, created_at)
        """)


# ── Credits ──────────────────────────────────────────────────────────────────
async def get_credits(user_id: str) -> int:
    row = await pool().fetchrow("SELECT credits FROM users WHERE id = $1", user_id)
    return row["credits"] if row else 0


class InsufficientCredits(Exception):
    """The wallet cannot cover the scan."""


class ScanAlreadyRunning(Exception):
    """The user already has a scan in flight."""


async def begin_scan(user_id: str, address: str, cost: int,
                     owner_photos: int = 0, goal: Optional[str] = None,
                     style: Optional[str] = None) -> tuple[str, int]:
    """
    Charge for a scan and create its row, atomically.

    All three steps — the one-at-a-time check, the debit and the insert — run
    inside one transaction against a locked user row. That matters because they
    are otherwise racy in both directions: two simultaneous submits could each
    see zero active scans, and a debit that lands while the insert fails would
    leave the user silently poorer with nothing to show for it. The row lock
    serialises submits per user; the rollback covers the rest.

    Returns (scan_id, remaining balance).
    """
    scan_id = uuid.uuid4().hex

    async with pool().acquire() as con:
        async with con.transaction():
            # FOR UPDATE, so a concurrent submit for this user waits here rather
            # than reading a stale balance and active-scan count.
            wallet = await con.fetchrow(
                "SELECT credits FROM users WHERE id = $1 FOR UPDATE", user_id
            )
            balance = wallet["credits"] if wallet else 0

            active = await con.fetchval(
                "SELECT count(*) FROM property_scans WHERE user_id = $1 AND status = ANY($2)",
                user_id, list(ACTIVE_STATUSES),
            )
            if active:
                raise ScanAlreadyRunning()

            if balance < cost:
                raise InsufficientCredits()

            remaining = await con.fetchval(
                "UPDATE users SET credits = credits - $2 WHERE id = $1 RETURNING credits",
                user_id, cost,
            )
            await con.execute(
                """
                INSERT INTO property_scans (id, user_id, address, status, stage,
                                            stage_detail, credits_spent, created_at,
                                            owner_photos, goal, style)
                VALUES ($1, $2, $3, $4, 'queued', 'Waiting for a worker', $5, $6, $7, $8, $9)
                """,
                scan_id, user_id, address, QUEUED, cost, datetime.now(timezone.utc),
                owner_photos, goal, style,
            )

    return scan_id, remaining


async def refund_credits(scan_id: str, user_id: str, amount: int) -> None:
    """
    Hand the credits back for a scan that failed.

    Guarded by `property_scans.refunded` so a retry, a crash-recovery sweep and
    the worker's own error handler cannot each refund the same scan.
    """
    async with pool().acquire() as con:
        async with con.transaction():
            claimed = await con.fetchrow(
                """
                UPDATE property_scans
                SET refunded = TRUE
                WHERE id = $1 AND refunded = FALSE AND credits_spent > 0
                RETURNING credits_spent
                """,
                scan_id,
            )
            if not claimed:
                return
            await con.execute(
                "UPDATE users SET credits = credits + $2 WHERE id = $1",
                user_id, min(amount, claimed["credits_spent"]),
            )
    log.info("Refunded %d credit(s) for scan %s", amount, scan_id)


# ── Scans ────────────────────────────────────────────────────────────────────
async def mark_running(scan_id: str) -> None:
    await pool().execute(
        """
        UPDATE property_scans
        SET status = $2, started_at = COALESCE(started_at, now())
        WHERE id = $1
        """,
        scan_id, RUNNING,
    )


async def set_stage(scan_id: str, stage: str, detail: str) -> None:
    await pool().execute(
        "UPDATE property_scans SET stage = $2, stage_detail = $3 WHERE id = $1",
        scan_id, stage, detail,
    )


async def set_live(scan_id: str, state: Dict[str, Any]) -> None:
    """
    Store the live per-photo snapshot.

    Updates arrive fire-and-forget from the worker thread and can land out of
    order, so each carries a version and an older one never overwrites a newer.
    """
    await pool().execute(
        """
        UPDATE property_scans SET live_json = $2::jsonb
        WHERE id = $1 AND status = 'running'
          AND (live_json IS NULL OR COALESCE((live_json->>'v')::int, 0) < $3)
        """,
        scan_id, _dumps(state), int(state.get("v", 0)),
    )


async def mark_completed(scan_id: str, result: Dict[str, Any]) -> None:
    # The live feed is superseded by the result, and fifty photos' worth of
    # it is not worth keeping on every row.
    await pool().execute(
        """
        UPDATE property_scans
        SET status = $2, result_json = $3::jsonb, completed_at = now(),
            stage = 'done', stage_detail = 'Report ready', error_message = NULL,
            live_json = NULL
        WHERE id = $1
        """,
        scan_id, COMPLETED, _dumps(result),
    )


async def mark_failed(scan_id: str, message: str) -> None:
    await pool().execute(
        """
        UPDATE property_scans
        SET status = $2, error_message = $3, completed_at = now()
        WHERE id = $1
        """,
        scan_id, FAILED, message[:1000],
    )


async def get_scan(scan_id: str, user_id: Optional[str] = None) -> Optional[asyncpg.Record]:
    if user_id is None:
        return await pool().fetchrow("SELECT * FROM property_scans WHERE id = $1", scan_id)
    return await pool().fetchrow(
        "SELECT * FROM property_scans WHERE id = $1 AND user_id = $2", scan_id, user_id
    )


async def list_scans(user_id: str, limit: int = 50) -> List[asyncpg.Record]:
    return await pool().fetch(
        """
        SELECT id, user_id, address, status, stage, stage_detail, credits_spent,
               owner_photos, goal, style, error_message, created_at, started_at,
               completed_at
        FROM property_scans
        WHERE user_id = $1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        user_id, limit,
    )


async def delete_scan(scan_id: str, user_id: str) -> bool:
    row = await pool().fetchrow(
        "DELETE FROM property_scans WHERE id = $1 AND user_id = $2 RETURNING id",
        scan_id, user_id,
    )
    return row is not None


async def reclaim_orphans() -> int:
    """
    A redeploy kills in-flight scans: the row still says `running` but no worker
    exists. Fail them on boot and give the credits back, otherwise the user is
    left staring at a spinner that will never resolve.

    Assumes a single service instance. With replicas this would need a worker
    lease column instead — see README.
    """
    rows = await pool().fetch(
        """
        UPDATE property_scans
        SET status = $1,
            error_message = 'Interrupted by a service restart. Your credit was refunded.',
            completed_at = now()
        WHERE status = ANY($2)
        RETURNING id, user_id, credits_spent
        """,
        FAILED, list(ACTIVE_STATUSES),
    )
    for row in rows:
        if row["credits_spent"]:
            await refund_credits(row["id"], row["user_id"], row["credits_spent"])
    if rows:
        log.warning("Reclaimed %d interrupted scan(s) on startup", len(rows))

    # Questions and renders cost no credit, so there is nothing to refund —
    # they only need to stop claiming to be in progress, and to stop counting
    # against the scan's allowance.
    await pool().execute(
        "UPDATE property_scan_questions SET status = 'failed', "
        "error_message = 'Interrupted by a service restart.' WHERE status = 'running'"
    )
    await pool().execute(
        "UPDATE property_scan_renders SET status = 'failed', completed_at = now(), "
        "error_message = 'Interrupted by a service restart.' WHERE status = 'running'"
    )
    return len(rows)


# ── After the scan: questions and studio renders ─────────────────────────────
class LimitReached(Exception):
    """The scan's included allowance for this feature is used up."""


class ScanNotReady(Exception):
    """The scan does not exist for this user, or has not completed."""


async def _claim(table: str, scan_id: str, user_id: str, limit: int,
                 columns: Dict[str, Any]) -> str:
    """
    Insert a 'running' row under the scan's allowance, atomically.

    The scan row is locked first, so two simultaneous requests cannot both
    see one slot left and both take it.

    Failed attempts do not use up the allowance — a refusal or a timeout is
    our problem, not the user's — but they do count towards a hard ceiling of
    twice the allowance, or a prompt the model always refuses could be retried
    for ever on one credit.
    """
    row_id = uuid.uuid4().hex
    async with pool().acquire() as con:
        async with con.transaction():
            scan = await con.fetchrow(
                "SELECT status FROM property_scans WHERE id = $1 AND user_id = $2 FOR UPDATE",
                scan_id, user_id,
            )
            if scan is None or scan["status"] != COMPLETED:
                raise ScanNotReady()
            used, attempts = await con.fetchrow(
                f"SELECT count(*) FILTER (WHERE status <> 'failed'), count(*) "
                f"FROM {table} WHERE scan_id = $1",
                scan_id,
            )
            if used >= limit or attempts >= limit * 2:
                raise LimitReached()
            names = ["id", "scan_id", "user_id", "status", *columns]
            values = [row_id, scan_id, user_id, RUNNING, *columns.values()]
            marks = ", ".join(f"${i}" for i in range(1, len(values) + 1))
            await con.execute(
                f"INSERT INTO {table} ({', '.join(names)}) VALUES ({marks})", *values
            )
    return row_id


async def count_used(table: str, scan_id: str) -> int:
    assert table in ("property_scan_questions", "property_scan_renders")
    return await pool().fetchval(
        f"SELECT count(*) FROM {table} WHERE scan_id = $1 AND status <> 'failed'", scan_id
    )


async def begin_question(scan_id: str, user_id: str, question: str, limit: int) -> str:
    return await _claim("property_scan_questions", scan_id, user_id, limit,
                        {"question": question})


async def finish_question(question_id: str, answer: Dict[str, Any]) -> None:
    await pool().execute(
        "UPDATE property_scan_questions SET status = $2, answer_json = $3::jsonb "
        "WHERE id = $1",
        question_id, COMPLETED, _dumps(answer),
    )


async def fail_question(question_id: str, message: str) -> None:
    await pool().execute(
        "UPDATE property_scan_questions SET status = $2, error_message = $3 WHERE id = $1",
        question_id, FAILED, message[:500],
    )


async def list_questions(scan_id: str, user_id: str) -> List[asyncpg.Record]:
    return await pool().fetch(
        """
        SELECT id, question, status, answer_json, error_message, created_at
        FROM property_scan_questions
        WHERE scan_id = $1 AND user_id = $2
        ORDER BY created_at
        """,
        scan_id, user_id,
    )


async def begin_render(scan_id: str, user_id: str, source: str, mode: str,
                       style: Optional[str], instruction: Optional[str], limit: int) -> str:
    return await _claim("property_scan_renders", scan_id, user_id, limit, {
        "source": source, "mode": mode, "style": style, "instruction": instruction,
    })


async def finish_render(render_id: str, file: str) -> None:
    await pool().execute(
        "UPDATE property_scan_renders SET status = $2, file = $3, completed_at = now() "
        "WHERE id = $1",
        render_id, COMPLETED, file,
    )


async def fail_render(render_id: str, message: str) -> None:
    await pool().execute(
        "UPDATE property_scan_renders SET status = $2, error_message = $3, "
        "completed_at = now() WHERE id = $1",
        render_id, FAILED, message[:500],
    )


async def get_render(render_id: str, user_id: str) -> Optional[asyncpg.Record]:
    return await pool().fetchrow(
        "SELECT * FROM property_scan_renders WHERE id = $1 AND user_id = $2",
        render_id, user_id,
    )


async def list_renders(scan_id: str, user_id: str) -> List[asyncpg.Record]:
    return await pool().fetch(
        """
        SELECT * FROM property_scan_renders
        WHERE scan_id = $1 AND user_id = $2
        ORDER BY created_at
        """,
        scan_id, user_id,
    )
