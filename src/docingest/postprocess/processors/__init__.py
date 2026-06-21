"""Built-in post-processors. Currently: extract."""

from .extract import ExtractProcessor, resolve_template_path

__all__ = ["ExtractProcessor", "resolve_template_path"]
