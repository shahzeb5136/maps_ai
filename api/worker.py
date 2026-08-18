"""
Background scan execution.

A scan is 1-3 minutes of blocking network I/O, far too long for a request. The
API charges the credit, writes a `queued` row, returns immediately, and hands
the work to this module. The frontend polls the row.

Concurrency is capped by a semaphore rather than a queue table because the
service runs as a single instance — see README for what changes if you scale it
to replicas.
"""

import asyncio
import logging
from typing import List, Optional, Set

from scanner import ScanFailed, run_scan
from scanner.config import SCAN_TIMEOUT_SECONDS

from . import config, db, storage

log = logging.getLogger(__name__)

_semaphore: asyncio.Semaphore | None = None
_tasks: Set[asyncio.Task] = set()


def init() -> None:
    global _semaphore
    _semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_SCANS)


def submit(scan_id: str, user_id: str, address: str,
           owner_photos: Optional[List[str]] = None) -> None:
    """Kick off a scan. Returns as soon as the task is scheduled."""
    task = asyncio.create_task(_run(scan_id, user_id, address, owner_photos or []))
    # Without a strong reference the event loop may garbage-collect the task
    # mid-flight.
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def shutdown(grace_seconds: float = 5.0) -> None:
    """Give running scans a moment on redeploy; anything still going is
    reclaimed and refunded on the next boot."""
    if not _tasks:
        return
    log.info("Waiting up to %.0fs for %d in-flight scan(s)", grace_seconds, len(_tasks))
    await asyncio.wait(set(_tasks), timeout=grace_seconds)


async def _run(scan_id: str, user_id: str, address: str,
               owner_photos: List[str]) -> None:
    assert _semaphore is not None, "worker.init() was not called"

    loop = asyncio.get_running_loop()

    def on_progress(stage: str, detail: str) -> None:
        # Called from the worker thread — hop back onto the loop to touch the DB.
        # Fire-and-forget: a dropped progress update must never fail the scan.
        asyncio.run_coroutine_threadsafe(_safe_stage(scan_id, stage, detail), loop)

    async with _semaphore:
        try:
            await db.mark_running(scan_id)
            payload = await asyncio.wait_for(
                asyncio.to_thread(run_scan, address, storage.scan_dir(scan_id),
                                  on_progress, owner_photos),
                timeout=SCAN_TIMEOUT_SECONDS,
            )
            await db.mark_completed(scan_id, payload)
            log.info("Scan %s completed (%s)", scan_id, address)
            return

        except asyncio.TimeoutError:
            # wait_for cancels the awaiting task, but a thread cannot be killed:
            # the pipeline runs on until its outstanding HTTP calls time out, and
            # may write a few files after we clean up below. They are orphaned on
            # the volume until that scan id is deleted. Bounded and rare — every
            # request in the pipeline carries its own 20-30s timeout.
            message = ("The scan took too long and was stopped. Your credit has been "
                       "refunded.")
            log.warning("Scan %s timed out after %ds", scan_id, SCAN_TIMEOUT_SECONDS)
        except ScanFailed as exc:
            # Expected, user-facing failure — bad address, no imagery, model refusal.
            message = f"{exc} Your credit has been refunded."
            log.info("Scan %s failed: %s", scan_id, exc)
        except Exception as exc:
            message = ("The scan failed unexpectedly. Your credit has been refunded.")
            log.exception("Scan %s crashed: %s", scan_id, exc)

    # Only reached on failure — the success path returns inside the block.
    await db.mark_failed(scan_id, message)
    await db.refund_credits(scan_id, user_id, config.CREDIT_COST)
    storage.delete_scan_files(scan_id)


async def _safe_stage(scan_id: str, stage: str, detail: str) -> None:
    try:
        await db.set_stage(scan_id, stage, detail)
    except Exception as exc:
        log.debug("Progress update dropped for %s: %s", scan_id, exc)
