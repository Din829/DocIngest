"""
docingest.postprocess — optional second-pass processing over a knowledge base.

Like docingest.graph, this subpackage is intentionally NOT imported from
``docingest.__init__``: it reaches beyond the core "preprocess only" mandate
(it consumes the produced artefacts and re-runs an LLM over them). Users opt
in explicitly:

    import docingest
    import docingest.postprocess

    docingest.ingest("./docs/", output="./kb/")                  # main pipeline
    docingest.postprocess.run("./kb/", template="doc_summary")   # OPT-IN second pass

What it is:
    A shared skeleton (base.Runner + PostProcessor ABC) for "read the
    produced artefacts → optionally split for the context window → call an
    LLM in parallel → merge → write a new artefact", so refine / graph /
    extraction / future processors stop re-implementing that loop. The first
    (and currently only) processor is template-driven structured extraction.

Public surface:
    run, ExtractResult            — top-level operation + result type
    PostProcessor, Runner         — extension points for new processors
    load_template, build_schema   — template → Pydantic schema helpers

Everything else under docingest.postprocess.* is internal.
"""

from .api import run, ExtractResult
from .base import PostProcessor, Runner, RunResult, UnitResult
from .schema_builder import load_template, build_schema, TemplateError

__all__ = [
    "run",
    "ExtractResult",
    "PostProcessor",
    "Runner",
    "RunResult",
    "UnitResult",
    "load_template",
    "build_schema",
    "TemplateError",
]
