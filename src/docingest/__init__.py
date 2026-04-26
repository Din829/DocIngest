"""DocIngest — universal document preprocessing for RAG and Agentic Search.

Public Python API. Import from ``docingest`` directly:

    import docingest
    result = docingest.ingest(
        "./docs/",
        output="./kb/",
        outputs=["markdown", "chunks"],
        vision=docingest.GeminiProvider(api_key="..."),
    )
    print(result.stats["successful"], "files processed")
    for md in result.markdown_files:
        print(md["path"], "->", len(md["content"]), "chars")

The names re-exported below form the stable surface. Everything else
(``docingest.pipeline``, ``docingest.parsers``, ``docingest.chunkers``, ...)
is internal and may change between releases without notice.
"""

from .api import ingest, inspect, refine, IngestResult, build_config
from .providers import (
    VisionProvider,
    AudioProvider,
    TextProvider,
    GeminiProvider,
    OpenAIProvider,
    AnthropicProvider,
    DashScopeProvider,
    WhisperProvider,
)

__version__ = "0.1.0"

__all__ = [
    # Core API
    "ingest",
    "inspect",
    "refine",
    "IngestResult",
    "build_config",
    # Provider base classes (for typing / custom subclasses)
    "VisionProvider",
    "AudioProvider",
    "TextProvider",
    # Concrete providers
    "GeminiProvider",
    "OpenAIProvider",
    "AnthropicProvider",
    "DashScopeProvider",
    "WhisperProvider",
    # Metadata
    "__version__",
]
