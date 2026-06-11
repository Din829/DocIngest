"""
Wall-clock timeout wrapper for long-running calls.

Used to bound parser and Vision API invocations so a single hung call
cannot stall the whole pipeline. Cross-platform — relies on
`concurrent.futures.ThreadPoolExecutor`, not `signal.SIGALRM` (which is
Unix-only and incompatible with non-main threads).

Caveat: Python threads cannot be force-killed, and the `with` executor
exits via shutdown(wait=True) — so on timeout the caller does NOT return
promptly: TimeoutError propagates only after the wrapped callable finishes
naturally (measured: a 5s sleep under a 1s cap raises after 5.0s, not 1s).
The timeout therefore marks the call as failed and bounds what the
pipeline ACCEPTS, not how long it physically waits. No orphan thread ever
outlives the call — which also means a caller holding a lock around this
wrapper never leaks that lock to a still-running zombie.
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
