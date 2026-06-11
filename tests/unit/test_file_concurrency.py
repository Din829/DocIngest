# -*- coding: utf-8 -*-
"""File-level concurrency (performance.file_concurrency > 1).

Three guarantees under test:
1. Concurrent outputs are EQUIVALENT to sequential outputs — same file order
   in the result and index.json, same chunk texts in the same order. Uses
   real small .md files (text path, no Vision → no LLM cost).
2. Aggregation order: even when later files FINISH first (stubbed skew),
   results are aggregated in input order.
3. Interrupt: a stop request mid-run skips not-yet-started files and marks
   the run interrupted, like the sequential path.
"""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import docingest.pipeline as pipeline_mod
from docingest.config import load_config
from docingest.parsers import create_parser
from docingest.chunkers import create_chunker
from docingest.pipeline import FileResult, run_pipeline, request_stop


def _make_inputs(n: int) -> Path:
    inputdir = Path(tempfile.mkdtemp())
    for i in range(n):
        (inputdir / f"doc{i:02d}.md").write_text(
            f"# Doc {i:02d}\n\nParagraph for document {i:02d}.\n",
            encoding="utf-8",
        )
    return inputdir


def _run(inputdir: Path, file_concurrency: int):
    outdir = tempfile.mkdtemp()
    config = load_config(cli_overrides={
        "output": {"dir": outdir},
        "incremental": {"enabled": False},
        "performance": {"file_concurrency": file_concurrency},
    })
    parser = create_parser(config)
    chunker = create_chunker(config)
    result = run_pipeline([inputdir], config, parser, chunker=chunker)
    return result, Path(outdir)


def test_concurrent_outputs_match_sequential():
    inputdir = _make_inputs(4)

    res_seq, out_seq = _run(inputdir, file_concurrency=1)
    res_con, out_con = _run(inputdir, file_concurrency=3)

    assert res_seq.successful == res_con.successful == 4

    # Result file order matches input order in both modes.
    seq_names = [Path(f.original_file).name for f in res_seq.files]
    con_names = [Path(f.original_file).name for f in res_con.files]
    assert seq_names == con_names

    # index.json file order identical.
    def index_names(out):
        data = json.loads((out / "index.json").read_text(encoding="utf-8"))
        return [f["path"] for f in data["files"]]
    assert index_names(out_seq) == index_names(out_con)

    # chunks.jsonl texts identical and in the same order.
    def chunk_texts(out):
        lines = (out / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
        return [json.loads(line)["text"] for line in lines]
    assert chunk_texts(out_seq) == chunk_texts(out_con)


def test_aggregation_order_survives_reversed_completion(monkeypatch):
    inputdir = _make_inputs(4)
    seen_order: list[str] = []

    def stub(file_path, parser, chunker, config, output_dir,
             existing_names=None, on_file_progress=None):
        # Later files finish FIRST: doc00 sleeps longest.
        idx = int(file_path.stem[-2:])
        time.sleep((3 - idx) * 0.15)
        seen_order.append(file_path.name)
        # Failed results exercise ordering without touching index/meta paths.
        return FileResult(
            original_file=str(file_path), success=False,
            error="stub", error_type="parse_error",
        ), []

    monkeypatch.setattr(pipeline_mod, "process_single_file", stub)
    res, _ = _run(inputdir, file_concurrency=4)

    # Completion really was (mostly) reversed…
    assert seen_order[0] == "doc03.md"
    # …but aggregation is in input order.
    names = [Path(f.original_file).name for f in res.files]
    assert names == [f"doc{i:02d}.md" for i in range(4)]
    assert res.failed == 4


def test_interrupt_skips_pending_files(monkeypatch):
    inputdir = _make_inputs(4)
    events: list[dict] = []

    def stub(file_path, parser, chunker, config, output_dir,
             existing_names=None, on_file_progress=None):
        if file_path.name == "doc00.md":
            request_stop()          # stop after the first file starts
        time.sleep(0.1)
        return FileResult(
            original_file=str(file_path), success=False,
            error="stub", error_type="parse_error",
        ), []

    monkeypatch.setattr(pipeline_mod, "process_single_file", stub)

    outdir = tempfile.mkdtemp()
    config = load_config(cli_overrides={
        "output": {"dir": outdir},
        "incremental": {"enabled": False},
        "performance": {"file_concurrency": 2},
    })
    parser = create_parser(config)
    res = run_pipeline([inputdir], config, parser, chunker=None,
                       on_progress=events.append)

    assert res.interrupted is True
    skipped = [e for e in events if e.get("status") == "skipped"]
    processed = [f for f in res.files]
    # Workers already started may finish; queued ones must be skipped.
    assert len(skipped) >= 1
    assert len(processed) + len(skipped) == 4
