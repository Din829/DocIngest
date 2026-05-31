"""
Resource root resolution — one place that answers "where do bundled static
resources (config/default.yaml, skills/*.SKILL.md) live", working both in
dev and inside a PyInstaller-frozen exe.

Why this exists: code that locates package-adjacent resources via
`Path(__file__).parent...` breaks once frozen — `__file__` then points into
the temporary _MEIPASS extraction dir, not the source tree. Every such site
(config loader, refine skill loader, future ones) should derive its root from
`resource_root()` instead of re-deriving from `__file__`, so the frozen-vs-dev
branch lives in exactly one place.

NOTE: getting the path right is necessary but not sufficient — the packaging
step must also `--add-data` the resources (config/, skills/) into _MEIPASS,
or the resolved path will be correct yet empty. See GUI/packaging docs.
"""

from __future__ import annotations

import sys
from pathlib import Path


def resource_root() -> Path:
    """Root under which bundled resources (`config/`, `skills/`) are found.

    - Frozen (PyInstaller exe): `sys._MEIPASS`, where `--add-data` unpacks them.
    - Dev / normal install: the project root (the dir containing `config/` and
      `skills/`), i.e. three levels up from this file
      (utils -> docingest -> src -> <project root>).

    Returns the same project root as the previous `Path(__file__)...` logic in
    dev, so behaviour is unchanged there; only the frozen case is new.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent.parent.parent
