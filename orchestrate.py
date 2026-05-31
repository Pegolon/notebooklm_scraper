#!/usr/bin/env python3
"""
Top-level orchestrator for the notebooklm_scraper two-app pipeline.

Runs both halves of the system in one process tree:

  • cloud/app.py (FastAPI via uvicorn) — long-running, restarted on crash.
  • local/scraper.py — fires once at startup, then every SCRAPE_INTERVAL_S
    (default 3600s = once an hour). Sequential runs only: if a scrape is
    still going when the timer ticks, the next start is deferred until it
    finishes.

Both children are spawned via `uv run …` in their respective subdirectories,
so each picks up its own local/.env or cloud/.env exactly the way the
manual invocations do. There is intentionally NO root-level .env: this
orchestrator's only knobs are the two listed below, read straight from the
process environment.

Environment knobs (optional; export them in your launchd/cron/systemd unit):
  SCRAPE_INTERVAL_S       Seconds between scraper runs (default 3600).
  CLOUD_HOST              uvicorn bind host (default 0.0.0.0).
  CLOUD_PORT              uvicorn bind port (default 8000).
  CLOUD_RESTART_DELAY_S   Backoff before restarting the cloud app after it
                          exits unexpectedly (default 5).

Run:
  python3 orchestrate.py

Stop:
  Ctrl-C (SIGINT) or SIGTERM — both children get SIGTERM, then SIGKILL
  after a short grace period.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import re
import signal
import sys
from pathlib import Path

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s")

ROOT = Path(__file__).resolve().parent
LOCAL_DIR = ROOT / "local"
CLOUD_DIR = ROOT / "cloud"

SCRAPE_INTERVAL_S = int(os.environ.get("SCRAPE_INTERVAL_S", "3600"))
CLOUD_HOST = os.environ.get("CLOUD_HOST", "0.0.0.0")
CLOUD_PORT = os.environ.get("CLOUD_PORT", "8000")
CLOUD_RESTART_DELAY_S = float(os.environ.get("CLOUD_RESTART_DELAY_S", "5"))

TERM_GRACE_S = 10.0  # how long to wait after SIGTERM before SIGKILL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [orchestrate] %(levelname)s %(message)s",
)
log = logging.getLogger("orchestrate")


async def _pipe_to_log(stream: asyncio.StreamReader | None, prefix: str) -> None:
    """Forward a child's stdout/stderr line-by-line to our own stdout."""
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            return
        # Generate timestamp matching orchestrator formatting
        now = datetime.datetime.now()
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
        # Decode and strip local-side timestamp if present to avoid double timestamps
        decoded_line = line.decode(errors='replace')
        decoded_line = TIMESTAMP_RE.sub("", decoded_line, count=1)
        # Children already format their own log lines; prepend the timestamp and tag them
        # so the combined stream is readable.
        sys.stdout.write(f"{timestamp} [{prefix}] {decoded_line}")
        sys.stdout.flush()


async def _spawn(prefix: str, cwd: Path, *args: str) -> asyncio.subprocess.Process:
    log.info("starting %s: %s (cwd=%s)", prefix, " ".join(args), cwd)
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    # Drain stdout in the background so the child's pipe buffer never fills.
    asyncio.create_task(_pipe_to_log(proc.stdout, prefix))
    return proc


async def _terminate(proc: asyncio.subprocess.Process, label: str) -> None:
    if proc.returncode is not None:
        return
    log.info("stopping %s (pid=%s)…", label, proc.pid)
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=TERM_GRACE_S)
    except asyncio.TimeoutError:
        log.warning("%s did not exit within %.0fs, killing", label, TERM_GRACE_S)
        try:
            proc.kill()
        except ProcessLookupError:
            return
        await proc.wait()


async def run_cloud(stop: asyncio.Event) -> None:
    """Keep the cloud uvicorn server running; restart it on crash."""
    while not stop.is_set():
        proc = await _spawn(
            "cloud",
            CLOUD_DIR,
            "uv", "run", "uvicorn", "app:app",
            "--host", CLOUD_HOST,
            "--port", str(CLOUD_PORT),
        )
        # Wait for either the child to exit OR a global stop request.
        wait_child = asyncio.create_task(proc.wait())
        wait_stop = asyncio.create_task(stop.wait())
        done, pending = await asyncio.wait(
            {wait_child, wait_stop},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()

        if stop.is_set():
            await _terminate(proc, "cloud")
            return

        rc = proc.returncode
        log.error("cloud exited unexpectedly with rc=%s; restarting in %.1fs",
                  rc, CLOUD_RESTART_DELAY_S)
        try:
            await asyncio.wait_for(stop.wait(), timeout=CLOUD_RESTART_DELAY_S)
            return  # stop fired during backoff
        except asyncio.TimeoutError:
            pass  # backoff elapsed, loop and restart


async def _run_scraper_once(stop: asyncio.Event) -> None:
    """One scraper invocation; runs to completion or until stop fires."""
    proc = await _spawn("scraper", LOCAL_DIR, "uv", "run", "scraper.py")
    wait_child = asyncio.create_task(proc.wait())
    wait_stop = asyncio.create_task(stop.wait())
    await asyncio.wait(
        {wait_child, wait_stop},
        return_when=asyncio.FIRST_COMPLETED,
    )
    wait_stop.cancel()
    if stop.is_set():
        await _terminate(proc, "scraper")
        return
    wait_child.cancel()  # already done; safe no-op
    rc = proc.returncode
    if rc == 0:
        log.info("scraper finished cleanly")
    else:
        # Scraper failures shouldn't kill the orchestrator — try again next tick.
        log.warning("scraper exited rc=%s; will retry at the next interval", rc)


async def run_scraper(stop: asyncio.Event) -> None:
    """Run the scraper once at startup, then every SCRAPE_INTERVAL_S."""
    while not stop.is_set():
        try:
            await _run_scraper_once(stop)
        except Exception:
            log.exception("scraper invocation raised; continuing")
        if stop.is_set():
            return
        log.info("next scrape in %ds", SCRAPE_INTERVAL_S)
        try:
            await asyncio.wait_for(stop.wait(), timeout=SCRAPE_INTERVAL_S)
            return  # stop fired during the wait
        except asyncio.TimeoutError:
            pass  # interval elapsed, loop


async def main() -> None:
    if not (LOCAL_DIR / "scraper.py").exists():
        sys.exit(f"missing {LOCAL_DIR/'scraper.py'} — run from repo root")
    if not (CLOUD_DIR / "app.py").exists():
        sys.exit(f"missing {CLOUD_DIR/'app.py'} — run from repo root")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop(signame: str) -> None:
        if not stop.is_set():
            log.info("received %s — shutting down", signame)
            stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _request_stop, sig.name)

    cloud_task = asyncio.create_task(run_cloud(stop), name="cloud")
    scraper_task = asyncio.create_task(run_scraper(stop), name="scraper")

    # If either supervisor task crashes hard, take the whole thing down so the
    # process manager (launchd / systemd / cron-wrapper) can restart us clean.
    done, pending = await asyncio.wait(
        {cloud_task, scraper_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    stop.set()
    for t in pending:
        await t
    for t in done:
        exc = t.exception()
        if exc is not None:
            log.error("supervisor task %s raised: %r", t.get_name(), exc)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
