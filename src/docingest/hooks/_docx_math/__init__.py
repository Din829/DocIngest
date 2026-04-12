"""
OMML (Office Math Markup Language) → LaTeX conversion.

Ported verbatim from Microsoft MarkItDown's converter_utils/docx/math/
module (itself adapted from xiilei/dwml). MIT licensed.

Source:
  https://github.com/microsoft/markitdown/tree/main/packages/markitdown/src/markitdown/converter_utils/docx/math

Used by the docx_omml pre-parse hook to rewrite math equations inside
a .docx file before Docling parses it, so formulas are preserved as
LaTeX in the resulting Markdown instead of being lost or garbled.
"""
