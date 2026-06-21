"""
AzureDIParser — Azure Document Intelligence as a DocIngest parsing backend.

Why this exists: docling's PDF backend (docling-parse) has a Windows
``std::bad_alloc`` regression that DocIngest works around with batched parsing
(see docs/docling_parse_OOM_Windows_长期监控.md). Azure DI sidesteps the bug
entirely by doing the parse in the cloud — the local machine only uploads bytes
and polls for a result, so there is no local C++ memory pressure at all.

This is an OPT-IN plugin backend, selected with ``parsing.engine: azure_di``.
It implements the same BaseParser → ParseResult contract as DoclingParser, so
every downstream Phase (write / chunk / index / knowledge_map / graph) reuses
unchanged. Nothing in the core pipeline imports this module; the azure SDK is
imported lazily inside parse() so installing DocIngest without the ``[azure]``
extra is unaffected.

Boundaries (system-edge defence only; trust internal contracts):
  - Credentials are validated at parse() entry → fail loud with an actionable
    message rather than a cryptic SDK error deep in the call.
  - The network call is the system edge: SDK / HTTP errors are caught and
    returned as ParseResult(success=False) so one bad file doesn't crash the
    run — same contract BaseParser documents ("should NOT raise").
"""

from __future__ import annotations

import logging
import os
from io import BytesIO
from pathlib import Path
from typing import Any

from ..config import get_nested
from ..parsers.base import BaseParser, ParseResult
from . import converter

logger = logging.getLogger(__name__)

# Formats Azure DI prebuilt-layout accepts. PDF/image are the ones that
# actually matter here (they're the formats that hit the docling-parse bug);
# Office/HTML are accepted by DI too and listed so supported_extensions()
# reports honestly.
_SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf",
    ".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".heif",
    ".docx", ".xlsx", ".pptx",
    ".html", ".htm",
})


class AzureDIParser(BaseParser):
    """Parse documents via the Azure Document Intelligence cloud service."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._client: Any = None  # lazy — built on first parse()

    # -- credential / client plumbing (system edge) -------------------------

    def _resolve_credentials(self) -> tuple[str, str]:
        """Resolve (endpoint, api_key). Order: config → env.

        A DocIntelligenceProvider passed by a library caller lands in config
        via config_overrides before we get here, so reading config covers both
        the provider path and plain YAML. Env is the final fallback.

        Raises ValueError (fail loud) when either is missing — a half-set
        credential is a configuration error the user must see now, not a
        silent empty-result later.
        """
        endpoint = (
            get_nested(self.config, "parsing.azure_di.endpoint", None)
            or os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
        )
        api_key = (
            get_nested(self.config, "parsing.azure_di.api_key", None)
            or os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_KEY")
        )
        missing = [
            name for name, val in (("endpoint", endpoint), ("api_key", api_key))
            if not val
        ]
        if missing:
            raise ValueError(
                f"Azure DI engine selected (parsing.engine=azure_di) but "
                f"{missing} not set. Provide via DocIntelligenceProvider, "
                f"parsing.azure_di.{{endpoint,api_key}} in config, or env "
                f"AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT / "
                f"AZURE_DOCUMENT_INTELLIGENCE_KEY."
            )
        return endpoint, api_key

    def _get_client(self) -> Any:
        """Lazy-init the DI client. azure SDK imported here so the [azure]
        extra is only required when this backend actually runs."""
        if self._client is not None:
            return self._client
        try:
            from azure.ai.documentintelligence import DocumentIntelligenceClient
            from azure.core.credentials import AzureKeyCredential
        except ImportError as e:
            raise ImportError(
                "parsing.engine=azure_di requires the azure extra. "
                "Install with: pip install -e \".[azure]\" "
                "(or pip install azure-ai-documentintelligence)."
            ) from e

        endpoint, api_key = self._resolve_credentials()
        self._client = DocumentIntelligenceClient(endpoint, AzureKeyCredential(api_key))
        return self._client

    # -- BaseParser interface ----------------------------------------------

    def parse(
        self,
        file_path: Path,
        *,
        override_stream: BytesIO | None = None,
    ) -> ParseResult:
        suffix = file_path.suffix.lower()
        source_format = suffix.lstrip(".")
        model_id = get_nested(self.config, "parsing.azure_di.model_id", "prebuilt-layout")

        try:
            client = self._get_client()
        except (ImportError, ValueError) as e:
            # Config / install errors are not per-file failures — re-raise so
            # the run stops with the actionable message instead of marking
            # every file failed with the same SDK/credential error.
            raise

        try:
            from azure.ai.documentintelligence.models import DocumentContentFormat

            body = override_stream if override_stream is not None else file_path.read_bytes()
            if isinstance(body, BytesIO):
                body = body.getvalue()

            poller = client.begin_analyze_document(
                model_id,
                body=body,
                output_content_format=DocumentContentFormat.MARKDOWN,
            )
            result = poller.result()
        except Exception as e:
            # The network call is the system edge — a transient API failure,
            # timeout, or unsupported file must not crash the whole run. Return
            # a failed ParseResult; the pipeline records it in errors.json and
            # moves on (fail loud at file granularity, not silent).
            logger.warning(
                f"Azure DI parse failed for {file_path.name} "
                f"({type(e).__name__}: {e})"
            )
            return ParseResult(
                markdown="",
                success=False,
                error=f"Azure DI parse failed: {e}",
                metadata={"error_type": "parse_error", "format": source_format},
            )

        converted = converter.convert(
            result, file_path=file_path, source_format=source_format
        )
        parse_result = ParseResult(
            markdown=converted["markdown"],
            metadata=converted["metadata"],
            pages=converted["pages"],
            success=True,
        )
        parse_result.transformations.append(
            {"step": "parse", "name": "azure_di", "model_id": model_id}
        )
        return parse_result

    def supported_extensions(self) -> set[str]:
        return set(_SUPPORTED_EXTENSIONS)
