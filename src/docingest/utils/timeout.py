"""
Wall-clock timeout wrapper for long-running calls.

Used to bound parser and Vision API invocations so a single hung call
cannot stall the whole pipeline. Cross-platform — relies on
`concurrent.futures.ThreadPoolExecutor`, not `signal.SIGALRM` (which is
Unix-only and incompatible with non-main threads).

Caveat: Python threads cannot be force-killed. If the wrapped callable
ignores the timeout, it keeps running until it finishes naturally; the
caller still returns promptly with TimeoutError so pipeline progress is
preserved. The orphan thread costs RAM/CPU until it ends — acceptable for
the rare hung-parse / hung-API case versus the alternative of an unbounded
hang.
"""

from __future__ import annotations

import concurrent.futures as cf
from typing import Callable, TypeVar

T = TypeVar("T")


def run_with_timeout(fn: Callable[[], T], timeout_sec: float | None) -> T:
    """
    Execute `fn` with a wall-clock cap.

    timeout_sec is None or <= 0 → run inline, no wrapping (zero overhead
    when timeouts are disabled).

    Raises:
        TimeoutError: when the callable does not finish within `timeout_sec`.
        Anything else `fn` raises propagates unchanged.
    """
    if timeout_sec is None or timeout_sec <= 0:
        return fn()
    with cf.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(fn)
        try:
            return future.result(timeout=timeout_sec)
        except cf.TimeoutError as e:
            raise TimeoutError(f"timed out after {timeout_sec}s") from e
