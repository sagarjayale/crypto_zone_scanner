"""Scans run in separate worker processes on MySQL, so several coins are scanned at the same time on several CPU cores,
and the web server's own process stays free to answer the pages (a scan in a thread of the same process competes with
the pages for Python's one interpreter lock).

Workers: SCAN_WORKERS in .env, or the number of CPU cores less one, at least 1 and at most 4. On SQLite (and in the
tests) scans run in a thread instead: SQLite lets only one writer in at a time, so processes would only wait for each
other. One coin is never scanned twice at the same time: a second request for the same coin waits for the first."""
import asyncio
import logging
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime

from ..db import IS_SQLITE
from ..engine import scanner

log = logging.getLogger(__name__)
_pool: ProcessPoolExecutor | None = None
_heavy: ProcessPoolExecutor | None = None      # backtest checks: their own workers, so live scans never wait behind them
_locks: dict[str, asyncio.Lock] = {}


def workers() -> int:
    env = os.getenv("SCAN_WORKERS")
    if env not in (None, ""):
        return max(0, int(env))
    if IS_SQLITE:
        return 0
    return max(1, min(4, (os.cpu_count() or 2) - 1))


def _pool_or_none() -> ProcessPoolExecutor | None:
    global _pool
    if _pool is None and workers() > 0:
        # "spawn" starts each worker as a clean Python process: it never inherits the web server's threads, sockets or
        # database connections. The workers start once and are reused for every scan.
        _pool = ProcessPoolExecutor(max_workers=workers(), mp_context=multiprocessing.get_context("spawn"))
    return _pool


async def scan(key: str, force: bool = True, now: datetime | None = None, timeframes: list[str] | None = None) -> list[dict]:
    """Scan one coin (scanner.scan_key) in a worker process, or in a thread on SQLite."""
    global _pool
    async with _locks.setdefault(key, asyncio.Lock()):
        pool = _pool_or_none()
        if pool is None:
            return await asyncio.to_thread(scanner.scan_key, key, force, now, timeframes)
        try:
            return await asyncio.get_running_loop().run_in_executor(pool, scanner.scan_key, key, force, now, timeframes)
        except BrokenProcessPool:          # a worker died (out of memory, killed): start fresh ones, scan this coin here
            log.warning("scan workers stopped unexpectedly; starting new ones")
            _pool = None
            return await asyncio.to_thread(scanner.scan_key, key, force, now, timeframes)


async def map_heavy(fn, calls: list[tuple]) -> list:
    """fn(*args) for every args in calls, several at once in worker processes (in order), or one after another in a
    thread on SQLite. Used by the backtest page: each coin is checked on its own CPU core."""
    global _heavy
    if workers() == 0:
        return await asyncio.to_thread(lambda: [fn(*a) for a in calls])
    if _heavy is None:
        _heavy = ProcessPoolExecutor(max_workers=workers(), mp_context=multiprocessing.get_context("spawn"))
    loop = asyncio.get_running_loop()
    try:
        return list(await asyncio.gather(*(loop.run_in_executor(_heavy, fn, *a) for a in calls)))
    except BrokenProcessPool:
        log.warning("backtest workers stopped unexpectedly; finishing in a thread")
        _heavy = None
        return await asyncio.to_thread(lambda: [fn(*a) for a in calls])


def shutdown() -> None:
    global _pool, _heavy
    for p in (_pool, _heavy):
        if p is not None:
            p.shutdown(wait=False, cancel_futures=True)
    _pool = _heavy = None
