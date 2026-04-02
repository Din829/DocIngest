"""
Vision module — AI-powered image/chart description.

Handles the decision logic for when to use Vision (page_strategy),
image filtering (size/dimensions), and calls the model provider
with caching.

Design:
  - page_strategy "auto": per-page decision based on image area
  - Filtering by size/dimensions avoids wasting API calls on icons
  - Results cached by image content hash (same image → same description)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import get_nested
from ..models.provider import describe_image
from ..models.cache import AICache, content_hash_file


def should_describe_image(
    image_path: Path,
    config: dict[str, Any],
) -> bool:
    """
    Decide whether an image should be sent to Vision model.

    Based on config's vision.min_image_size_kb and vision.min_dimensions.
    Also checks vision.enabled and vision.page_strategy.

    Args:
        image_path: Path to the extracted image file.
        config: Full config dict.

    Returns:
        True if the image should be described by Vision model.
    """
    vision_cfg = get_nested(config, "parsing.vision", {})

    if not vision_cfg.get("enabled", True):
        return False

    strategy = vision_cfg.get("page_strategy", "auto")
    if strategy == "never":
        return False
    if strategy == "all_pages":
        return True

    # "auto" and "images_only" modes: apply size/dimension filters
    min_size_kb = vision_cfg.get("min_image_size_kb", 20)
    min_dims = vision_cfg.get("min_dimensions", [200, 200])

    # Check file size
    file_size_kb = image_path.stat().st_size / 1024
    if file_size_kb < min_size_kb:
        return False

    # Check dimensions (if PIL available)
    try:
        from PIL import Image
        with Image.open(image_path) as img:
            w, h = img.size
            if w < min_dims[0] and h < min_dims[1]:
                return False
    except ImportError:
        # PIL not available → skip dimension check, rely on file size only
        pass
    except Exception:
        # Can't read image dimensions → allow (don't block on this)
        pass

    return True


def describe_image_cached(
    image_path: Path,
    config: dict[str, Any],
    cache: AICache | None = None,
) -> str:
    """
    Get AI description of an image, with caching.

    Args:
        image_path: Path to the image file.
        config: Full config dict.
        cache: Optional AICache instance. If None, no caching.

    Returns:
        Text description of the image. Empty string if Vision disabled
        or image filtered out.
    """
    if not should_describe_image(image_path, config):
        return ""

    vision_model_config = get_nested(config, "models.vision", {})

    # Build model name for cache key
    primary = vision_model_config.get("primary", {})
    model_name = f"{primary.get('provider', 'openai')}/{primary.get('model', 'gpt-5.4-mini')}"

    prompt = (
        "Describe this image in detail. "
        "If it contains a chart, graph, or table, extract all data points, "
        "labels, values, and trends. Be precise with numbers."
    )

    if cache:
        img_hash = content_hash_file(image_path)
        return cache.get_or_call(
            model_name=model_name,
            content_hash=img_hash,
            call_fn=lambda: describe_image(image_path, prompt, vision_model_config),
            extra_key="vision_describe",
        )
    else:
        return describe_image(image_path, prompt, vision_model_config)
