"""
Sensitive data sanitization hook — replaces PII patterns with placeholders.

Design principles:
  - Precision over recall: only mask patterns with very low false-positive
    rates. It's better to miss a phone number than to destroy a product
    code or invoice number.
  - Default OFF: must be explicitly enabled via config (sanitize.enabled).
  - Rules are config-driven: users can override/extend the default rule set.
  - No NER / no name detection: names are impossible to reliably detect
    with regex, and NER models have unacceptable false-positive rates
    for document ingestion (especially Japanese names).

Default rules (high precision only):
  - Email addresses (@ sign is a strong discriminator)
  - URLs (https:// prefix is unambiguous)
  - Credit card numbers (16 digits + Luhn checksum validation)
  - IPv4 addresses (dotted quad with value range check)
  - Japanese phone numbers (strict format with area code patterns)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from ..config import get_nested
from ..parsers.base import ParseResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Luhn checksum (credit card validation to reduce false positives)
# ---------------------------------------------------------------------------

def _luhn_check(digits: str) -> bool:
    """Validate a digit string using the Luhn algorithm."""
    if not digits.isdigit() or len(digits) < 13:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


# ---------------------------------------------------------------------------
# Default sanitization rules (high precision, low false-positive)
# ---------------------------------------------------------------------------

def _build_default_rules() -> list[dict[str, str]]:
    """
    Return default rules as list of {pattern, replacement, name} dicts.

    These are intentionally conservative — each rule is chosen because
    its pattern has a strong structural discriminator (@ sign, https://,
    dotted quad range, etc.) that minimizes false positives.
    """
    return [
        {
            "name": "email",
            "pattern": r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
            "replacement": "[EMAIL]",
        },
        {
            "name": "url",
            "pattern": r"https?://[A-Za-z0-9.\-]+(:\d+)?(/\S*)?",
            "replacement": "[URL]",
        },
        {
            "name": "credit_card",
            # 13-19 digits with optional separators (space or dash)
            "pattern": r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{1,7}\b",
            "replacement": "[CREDIT_CARD]",
            # Post-validation: Luhn check (handled in apply logic)
        },
        {
            "name": "ipv4",
            # Strict: each octet 0-255, not just any dotted quad
            "pattern": r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b",
            "replacement": "[IP_ADDRESS]",
        },
        {
            "name": "phone_jp",
            # Japanese phone: 0X0-XXXX-XXXX or (0X0)XXXX-XXXX or 0120-XXX-XXX
            "pattern": r"\b(?:0\d{1,4}[\-\s()]{1,2}\d{1,4}[\-\s]\d{3,4})\b",
            "replacement": "[PHONE]",
        },
    ]


# ---------------------------------------------------------------------------
# Core sanitization logic
# ---------------------------------------------------------------------------

def _apply_rules(
    text: str,
    rules: list[dict[str, Any]],
) -> tuple[str, int]:
    """
    Apply sanitization rules to text. Returns (sanitized_text, match_count).

    Credit card rule gets extra Luhn validation to avoid false positives.
    """
    total_matches = 0

    for rule in rules:
        name = rule.get("name", "")
        pattern = rule.get("pattern", "")
        replacement = rule.get("replacement", "[REDACTED]")

        if not pattern:
            continue

        try:
            compiled = re.compile(pattern)
        except re.error:
            logger.warning(f"Invalid sanitize pattern '{name}': {pattern}")
            continue

        if name == "credit_card":
            # Credit card: regex match + Luhn validation
            def _cc_replace(m: re.Match) -> str:
                nonlocal total_matches
                digits = re.sub(r"[\s\-]", "", m.group())
                if _luhn_check(digits):
                    total_matches += 1
                    return replacement
                return m.group()  # Not a valid card number, leave as-is
            text = compiled.sub(_cc_replace, text)
        else:
            matches = compiled.findall(text)
            if matches:
                total_matches += len(matches)
                text = compiled.sub(replacement, text)

    return text, total_matches


# ---------------------------------------------------------------------------
# Hook entry point
# ---------------------------------------------------------------------------

def sanitize_hook(
    file_path: Path,
    parse_result: ParseResult,
    config: dict[str, Any],
) -> None:
    """
    Pre-write hook: sanitize sensitive data in markdown content.

    Only runs when sanitize.enabled is true (default: false).
    Rules come from config (sanitize.rules) or built-in defaults.

    Raises HookNoOp when the feature is off OR no patterns matched —
    both cases leave markdown unchanged, so the hook did not contribute
    to the provenance trail and should not appear in lineage.
    """
    from . import HookNoOp

    if not get_nested(config, "sanitize.enabled", False):
        raise HookNoOp

    # Load rules: config overrides defaults entirely if provided
    config_rules = get_nested(config, "sanitize.rules", None)
    if isinstance(config_rules, list) and config_rules:
        rules = config_rules
    else:
        rules = _build_default_rules()

    sanitized, count = _apply_rules(parse_result.markdown, rules)

    if count == 0:
        # Feature enabled but no sensitive content found — markdown
        # wasn't modified, so no lineage entry either.
        raise HookNoOp

    parse_result.markdown = sanitized
    logger.info(f"Sanitized {count} sensitive pattern(s) in {file_path.name}")
