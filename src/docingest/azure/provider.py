"""
DocIntelligenceProvider — credential injection for the Azure DI parser.

Mirrors the dataclass-Provider style of ``docingest.providers`` but lives in
the azure plugin because Azure DI is a PARSING backend, not an LLM provider:
it does not belong to the Vision/Audio/Text provider families (those map to
``models.*`` config and dispatch through litellm). Azure DI maps to
``parsing.azure_di`` and dispatches through its own SDK client.

Usage (library):

    import docingest
    from docingest.azure import DocIntelligenceProvider

    docingest.ingest(
        "./docs/", output="./kb/",
        config_overrides={"parsing.engine": "azure_di"},
        # credentials either here via the provider, or via env / YAML:
        **DocIntelligenceProvider(
            endpoint="https://<resource>.cognitiveservices.azure.com/",
            api_key="...",
        ).as_ingest_kwargs(),
    )

Credentials resolution (highest wins), enforced in di_parser._resolve_credentials:
    1. DocIntelligenceProvider passed explicitly
    2. config parsing.azure_di.{endpoint,api_key}
    3. env AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT / _KEY
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class DocIntelligenceProvider:
    """Azure Document Intelligence credentials + model selection.

    Fields:
      endpoint:  https://<resource>.cognitiveservices.azure.com/ (no trailing path)
      api_key:   the resource key (or rely on env / YAML / ambient AAD later)
      model_id:  DI model id; "prebuilt-layout" is the right default for
                 generic document → markdown extraction.
    """

    endpoint: str | None = None
    api_key: str | None = None
    model_id: str = "prebuilt-layout"

    def to_config(self) -> dict[str, Any]:
        """Return the ``parsing.azure_di`` config fragment.

        Only emits fields that were set, so passing just an endpoint (key via
        env) does not blank out an env-provided key downstream.
        """
        cfg: dict[str, Any] = {"model_id": self.model_id}
        if self.endpoint:
            cfg["endpoint"] = self.endpoint
        if self.api_key:
            cfg["api_key"] = self.api_key
        return cfg

    def as_ingest_kwargs(self) -> dict[str, Any]:
        """Shape for splatting into ``ingest(config_overrides=...)``.

        Returns ``{"config_overrides": {"parsing": {"azure_di": {...}}}}`` so
        the caller can merge it. Kept separate from to_config() so callers who
        already manage config_overrides can use to_config() directly instead.
        """
        return {"config_overrides": {"parsing": {"azure_di": self.to_config()}}}
