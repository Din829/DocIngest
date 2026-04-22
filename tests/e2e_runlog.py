"""
End-to-end test for run_log: drive real run_pipeline across 4 scenarios
and verify log.md reflects each.

Scenarios:
  1. Initial run           → both files "added"
  2. No-change re-run      → both "cached", body empty, section still there
  3. Config change re-run  → both "updated" with "config changed" reason
  4. New file + --force    → all "forced" + the newcomer also listed

Vision is disabled to keep the test fast and offline.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# Environment BEFORE importing docingest — env overrides must be set early.
os.environ["DOCINGEST__parsing__vision__enabled"] = "false"
os.environ["DOCINGEST__knowledge_map__enabled"] = "false"  # skip LLM summary call

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from docingest.config import load_config
from docingest.parsers import create_parser
from docingest.chunkers import create_chunker
from docingest.pipeline import run_pipeline


_BASE = Path(__file__).resolve().parent.parent / "_runlog_e2e_tmp"
DOCS = _BASE / "docs"
KB = _BASE / "kb"
LOG = KB / "log.md"


def _setup() -> None:
    """Fresh workspace + copy small fixtures into DOCS."""
    import shutil
    if _BASE.exists():
        shutil.rmtree(_BASE, ignore_errors=True)
    DOCS.mkdir(parents=True, exist_ok=True)
    KB.mkdir(parents=True, exist_ok=True)

    fixtures = Path(__file__).resolve().parent / "fixtures"
    for name in ("test_omml.docx", "test_chart.pptx"):
        shutil.copy2(fixtures / name, DOCS / name)
    print(f"Workspace ready at {_BASE}")
    print(f"  docs: {sorted(p.name for p in DOCS.iterdir())}")


_setup()


def _run(tag: str, *, force: bool = False, extra_overrides: dict | None = None) -> None:
    print(f"\n=== RUN {tag} (force={force}) ===")
    overrides: dict[str, Any] = {"output": {"dir": str(KB)}}
    if force:
        overrides["incremental"] = {"force": True}
    if extra_overrides:
        from docingest.config import deep_merge
        overrides = deep_merge(overrides, extra_overrides)
    config = load_config(cli_overrides=overrides)
    parser = create_parser(config)
    chunker = create_chunker(config)
    result = run_pipeline([DOCS], config, parser, chunker)
    print(f"  total={result.total_files} ok={result.successful} fail={result.failed} chunks={result.total_chunks}")
    for fr in result.files:
        print(f"    {fr.status:8s} {Path(fr.original_file).name:20s} chunks={fr.chunks_count} reason={fr.cache_reason!r}")


def _read_log() -> str:
    return LOG.read_text(encoding="utf-8") if LOG.exists() else "<no log.md>"


def _count_sections(text: str) -> int:
    return sum(1 for ln in text.splitlines() if ln.startswith("## ["))


# -----------------------------------------------------------------
# Scenario 1: initial run
# -----------------------------------------------------------------
print("\n################ SCENARIO 1: initial run ################")
assert not LOG.exists(), "KB must be empty at start"
_run("#1 initial")

log1 = _read_log()
print("\n----- log.md after run #1 -----")
print(log1)
print("----- end -----")
assert LOG.exists(), "log.md should be created"
assert log1.startswith("# DocIngest Run Log"), "missing top title"
assert _count_sections(log1) == 1, "exactly one section"
assert "added: test_omml.docx" in log1, "omml should be tagged added"
assert "added: test_chart.pptx" in log1, "chart should be tagged added"
assert "2 added" in log1
print("  PASS: initial run shows 2 added")

# -----------------------------------------------------------------
# Scenario 2: no-change re-run
# -----------------------------------------------------------------
print("\n################ SCENARIO 2: no-change re-run ################")
_run("#2 no-change")

log2 = _read_log()
print("\n----- log.md after run #2 (last section) -----")
print("\n".join(log2.splitlines()[-10:]))
print("----- end -----")
assert _count_sections(log2) == 2, f"two sections, got {_count_sections(log2)}"
assert "no changes" in log2.splitlines()[-2], "last section summary should be 'no changes'"
# No "added"/"updated"/"failed" bullets introduced in this section
last_section_body = log2.split("## [")[-1]
assert "- added:" not in last_section_body
assert "- updated:" not in last_section_body
assert "- failed:" not in last_section_body
print("  PASS: no-change run produces empty-body section")

# -----------------------------------------------------------------
# Scenario 3: config change → both updated
# -----------------------------------------------------------------
print("\n################ SCENARIO 3: config change ################")
# chunking.max_tokens is in _RELEVANT_CONFIG_PATHS, so changing it
# invalidates every cache entry → both files must re-run as "updated".
_run("#3 config-change", extra_overrides={"chunking": {"max_tokens": 1024}})

log3 = _read_log()
last_section_body = log3.split("## [")[-1]
print("\n----- log.md last section -----")
print("## [" + last_section_body)
print("----- end -----")
assert _count_sections(log3) == 3
assert "updated: test_omml.docx (config changed)" in last_section_body
assert "updated: test_chart.pptx (config changed)" in last_section_body
assert "2 updated" in last_section_body
print("  PASS: config change shows 'updated (config changed)' for both files")

# -----------------------------------------------------------------
# Scenario 4: new file + --force
# -----------------------------------------------------------------
print("\n################ SCENARIO 4: new file + --force ################")
# Add a fresh plain-text file and force a full rebuild.
new_file = DOCS / "fresh_note.txt"
new_file.write_text("Hello, this is a new note.\n\nSome content.", encoding="utf-8")
_run("#4 force+new", force=True)

log4 = _read_log()
last_section_body = log4.split("## [")[-1]
print("\n----- log.md last section -----")
print("## [" + last_section_body)
print("----- end -----")
assert _count_sections(log4) == 4
assert "(forced)" in last_section_body, "header should say (forced)"
assert "rebuilt" in last_section_body, "summary should say 'rebuilt'"
# Every file appears as forced (the new one included)
assert "forced: test_omml.docx" in last_section_body
assert "forced: test_chart.pptx" in last_section_body
assert "forced: fresh_note.txt" in last_section_body
print("  PASS: --force produces 'rebuilt' summary + all files marked forced")

# -----------------------------------------------------------------
# Grep-friendliness check
# -----------------------------------------------------------------
print("\n################ GREP CHECK ################")
headers = [ln for ln in log4.splitlines() if ln.startswith("## [")]
print(f"  Found {len(headers)} grep-matching headers:")
for h in headers:
    print(f"    {h}")
assert len(headers) == 4, f"Expected 4 headers, got {len(headers)}"
print("  PASS: headers match '^## \\[' pattern")

# -----------------------------------------------------------------
# Final full log for visual review
# -----------------------------------------------------------------
print("\n################ FULL log.md ################")
print(_read_log())

print("\n################ ALL SCENARIOS PASSED ################")
