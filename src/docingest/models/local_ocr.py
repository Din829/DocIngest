"""
Local OCR backend — an opt-in alternative to the cloud Vision provider for the
OCR / page-transcription task ONLY.

Why this exists, and what it is NOT:
  DocIngest's Vision model (Gemini by default) does FOUR things across the
  pipeline: OCR/page-reading (describe_image), per-chunk summary, chunking
  assist, refine, graph extraction. A local OCR model (e.g. baidu/Unlimited-OCR)
  can ONLY do the first — look at a page image and transcribe it. It cannot do
  summary / chunking / refine, which are text-in/text-out tasks routed through
  the entirely separate ``text_completion`` path.

  So this backend hooks in at exactly ONE place: ``describe_image`` /
  ``describe_images_batched`` (the OCR seam). Everything else keeps running on
  the cloud ``primary`` model, untouched. Enabling a local OCR backend swaps
  ONLY who reads the page image — it does not, and cannot, replace the model
  for the other tasks.

Design (deliberately minimal — one function, no registry/ABC):
  There is exactly one local backend today (Unlimited-OCR). A single dispatch
  function with a per-backend branch is enough; a registry/abstract base class
  would be premature abstraction (YAGNI). If a second local model arrives, this
  grows a branch — or gets refactored then, with a real second case to design
  against. Mirrors how the reference projects (claw-code's "generic provider +
  per-model patch") and DocIngest's own chunker factory handle this.

Optional dependency isolation (same rule as graph / azure subpackages):
  ``torch`` / ``transformers`` are imported lazily, INSIDE the loader, so the
  core pipeline never pays for them. A missing dependency fails loud with an
  install hint, never silently.

Singleton model cache:
  The model weights are multi-GB and live on the GPU. We load once per
  (backend, model_id, device) and reuse the handle for every page — reloading
  per call would be unusable. The cache is process-global, matching the
  one-run-at-a-time assumption already used elsewhere in the pipeline.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Singleton model cache — keyed by (backend, model_id, device).
# ---------------------------------------------------------------------------
_MODEL_CACHE: dict[tuple[str, str, str], Any] = {}


class LocalOCRDependencyError(RuntimeError):
    """Raised when a local OCR backend's heavy deps (torch/transformers) are
    missing — fail loud with an actionable install hint rather than degrading
    silently."""


def _resolve_backend_config(model_config: dict[str, Any] | None) -> dict[str, Any]:
    """Pull the ocr_* knobs out of a vision model_config. Returns a small dict
    with backend / model_id / device / keep_boxes, all with sane defaults."""
    mc = model_config or {}
    return {
        "backend": mc.get("ocr_backend"),
        "model_id": mc.get("ocr_model", "baidu/Unlimited-OCR"),
        "device": mc.get("ocr_device", "cuda"),
        "keep_boxes": bool(mc.get("ocr_keep_boxes", False)),
    }


def is_local_ocr_enabled(model_config: dict[str, Any] | None) -> bool:
    """True when this vision model_config asks for a local OCR backend. The
    single check ``describe_image`` uses to decide whether to branch off the
    cloud path — kept here so the provider module stays free of backend
    details."""
    return bool(_resolve_backend_config(model_config)["backend"])


# ---------------------------------------------------------------------------
# Backend: Unlimited-OCR (baidu)
# ---------------------------------------------------------------------------

def _load_unlimited_ocr(model_id: str, device: str) -> Any:
    """Lazy-load Unlimited-OCR onto the GPU. torch/transformers imported here
    only — a missing dep raises LocalOCRDependencyError, not ImportError, so the
    caller sees an actionable message."""
    try:
        import torch
        from transformers import AutoModel, AutoTokenizer
    except ImportError as e:
        raise LocalOCRDependencyError(
            "Local OCR backend 'unlimited_ocr' needs torch + transformers. "
            "Install with: pip install -e '.[local-ocr]'  (GPU required). "
            f"Original error: {e}"
        ) from e

    logger.info(f"Loading local OCR model {model_id} onto {device} (one-time)...")
    tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
    model = AutoModel.from_pretrained(
        model_id, trust_remote_code=True, use_safetensors=True,
        torch_dtype=torch.bfloat16,
    ).eval()
    if device.startswith("cuda"):
        model = model.cuda()
    return {"model": model, "tokenizer": tokenizer}


# Per-backend (loader, infer, infer_multi, to_markdown). Adding a second local
# model = add a key here + its four callables. No framework change.
_BACKENDS: dict[str, dict[str, Any]] = {}


def _get_handle(backend: str, model_id: str, device: str) -> Any:
    """Return the cached model handle for this backend, loading it once."""
    if backend not in _BACKENDS:
        raise ValueError(
            f"Unknown local OCR backend {backend!r}. "
            f"Known: {sorted(_BACKENDS)}"
        )
    key = (backend, model_id, device)
    if key not in _MODEL_CACHE:
        _MODEL_CACHE[key] = _BACKENDS[backend]["load"](model_id, device)
    return _MODEL_CACHE[key]


# ---------------------------------------------------------------------------
# Output adapter — Unlimited-OCR emits "<|det|>label [x,y,x,y]<|/det|>text" plus
# HTML <table>. We turn that into the clean Markdown the rest of the pipeline
# expects, optionally preserving the boxes for downstream consumers.
# ---------------------------------------------------------------------------

_DET_RE = re.compile(r"<\|det\|>([^<]*?)\[[\d,\s]+\]<\|/det\|>")
_DET_ANY_RE = re.compile(r"<\|/?det\|>")


def _unlimited_to_markdown(raw: str, *, keep_boxes: bool) -> str:
    """Strip the <|det|> layout tokens, leaving the transcribed text/tables.

    keep_boxes is accepted for interface parity; box extraction into
    element_boxes is a pipeline-side concern (the raw string still carries the
    coordinates if a caller wants them), so here we always produce clean text.
    """
    if not raw:
        return ""
    # Drop the "<|det|>label [coords]<|/det|>" prefix on each line, keep the
    # text that follows it. Lines without det tags (HTML tables) pass through.
    cleaned = _DET_RE.sub("", raw)
    cleaned = _DET_ANY_RE.sub("", cleaned)
    return cleaned.strip()


def _infer_unlimited(handle: Any, image_path: Path, prompt: str) -> str:
    """Single-image inference. Prompt is passed through but Unlimited-OCR is a
    fixed-behaviour OCR model (it ignores instruction wording — measured), so
    the standard 'document parsing' prompt is what actually drives it."""
    result = handle["model"].infer(
        handle["tokenizer"],
        prompt="<image>document parsing.",
        image_file=str(image_path),
        output_path="",
        base_size=1024, image_size=640, crop_mode=True,
        max_length=32768,
        no_repeat_ngram_size=35, ngram_window=128,
        save_results=False,
        eval_mode=True,
    )
    # model.infer prints to stdout and returns the text in eval_mode; tolerate
    # either by coercing to str.
    return result if isinstance(result, str) else (str(result) if result else "")


def _infer_unlimited_multi(handle: Any, image_paths: list[Path], prompt: str) -> str:
    """Multi-page inference via Unlimited-OCR's native infer_multi (its
    one-shot long-horizon strength)."""
    result = handle["model"].infer_multi(
        handle["tokenizer"],
        prompt="<image>Multi page parsing.",
        image_files=[str(p) for p in image_paths],
        output_path="",
        image_size=1024,
        max_length=32768,
        no_repeat_ngram_size=35, ngram_window=1024,
        save_results=False,
        eval_mode=True,
    )
    return result if isinstance(result, str) else (str(result) if result else "")


_BACKENDS["unlimited_ocr"] = {
    "load": _load_unlimited_ocr,
    "infer": _infer_unlimited,
    "infer_multi": _infer_unlimited_multi,
    "to_markdown": _unlimited_to_markdown,
}


# ---------------------------------------------------------------------------
# Public entry points — called from provider.describe_image(_batched)
# ---------------------------------------------------------------------------

def run_local_ocr(
    image_path: Path | str,
    prompt: str,
    model_config: dict[str, Any] | None,
) -> str:
    """Single-image local OCR → clean Markdown. Mirrors describe_image's
    contract (returns the transcription string)."""
    cfg = _resolve_backend_config(model_config)
    backend = cfg["backend"]
    handle = _get_handle(backend, cfg["model_id"], cfg["device"])
    raw = _BACKENDS[backend]["infer"](handle, Path(image_path), prompt)
    return _BACKENDS[backend]["to_markdown"](raw, keep_boxes=cfg["keep_boxes"])


def run_local_ocr_batched(
    image_paths: list[Path] | list[str],
    prompt: str,
    model_config: dict[str, Any] | None,
) -> str:
    """Multi-image local OCR → clean Markdown. Mirrors describe_images_batched's
    contract."""
    cfg = _resolve_backend_config(model_config)
    backend = cfg["backend"]
    handle = _get_handle(backend, cfg["model_id"], cfg["device"])
    paths = [Path(p) for p in image_paths]
    raw = _BACKENDS[backend]["infer_multi"](handle, paths, prompt)
    return _BACKENDS[backend]["to_markdown"](raw, keep_boxes=cfg["keep_boxes"])
