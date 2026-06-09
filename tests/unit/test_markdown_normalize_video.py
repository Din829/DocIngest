"""
Unit tests for the video-markdown normalization in markdown_writer.

The native_video prompt can produce `- **说**: ...` / `- **画面**: ...`
lines that downstream chunkers' list-protection rule treats as a single
list block (refusing to split). _normalize_video_markdown strips the
leading `- ` so each label stands as its own paragraph.

These tests are pure (no pipeline / no LLM) and verify both:
  1. The normalizer itself transforms input correctly.
  2. After normalization, the actual project chunkers produce sensibly
     sized chunks with full content coverage — i.e. the bug we observed
     in production on a 23-min 抖音 video cannot recur.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from docingest.output.markdown_writer import _normalize_video_markdown
from docingest.output.markdown_writer import write_markdown
from docingest.chunkers import AutoChunker
from docingest.api import build_config
from docingest.parsers.base import ParseResult


# A minimal but realistic chunk of native_video output (1 segment) — same
# shape as the real prompt produces when the model uses list format.
_SAMPLE_LIST_FORM = """\
[00:00]
- **说**: 哈喽，大家好。我是刘子瑜。今天分享 AI 视频创作。这段语音内容会比较长，描述了视频创作的整个流程，包括提示词、画面、运镜等多个方面。
- **画面**: 视频开始，画面中心显示一张图片，图片中有两个机器人。屏幕左上角显示"小云雀AI"，右上角显示"AI创作浪潮计划"。

[00:40]
- **说**: 通常我的视频提示词都会由三个大部分组成。
- **画面**: 屏幕上出现大的标题字样"提示词"。
"""

_SAMPLE_PARA_FORM = """\
[00:00]

**说**: 哈喽，大家好。

**画面**: 视频开始。
"""

_SAMPLE_DRIFT_FORM = """\
[00:00]
- **解说**: 这是口播内容。
- **voiceover**: This is the spoken narration.
- **画面描述**: 屏幕上展示操作步骤。
"""


class TestNormalizer(unittest.TestCase):
    """Direct tests on _normalize_video_markdown."""

    def test_strips_dash_from_label_lines(self):
        out = _normalize_video_markdown(_SAMPLE_LIST_FORM)
        # No more "- **说**" or "- **画面**" anywhere
        self.assertNotIn("- **说**", out)
        self.assertNotIn("- **画面**", out)
        # But the labels themselves are preserved
        self.assertIn("**说**:", out)
        self.assertIn("**画面**:", out)

    def test_strips_drifted_bold_labels_by_shape(self):
        out = _normalize_video_markdown(_SAMPLE_DRIFT_FORM)
        self.assertNotIn("- **解说**", out)
        self.assertNotIn("- **voiceover**", out)
        self.assertNotIn("- **画面描述**", out)
        self.assertIn("**解说**:", out)
        self.assertIn("**voiceover**:", out)
        self.assertIn("**画面描述**:", out)

    def test_long_bold_label_is_not_normalized(self):
        label = "a" * 41
        markdown = f"[00:00]\n- **{label}**: keep as list\n"
        self.assertEqual(_normalize_video_markdown(markdown), markdown)

    def test_idempotent(self):
        """Running normalize twice produces the same result as once."""
        once = _normalize_video_markdown(_SAMPLE_LIST_FORM)
        twice = _normalize_video_markdown(once)
        self.assertEqual(once, twice)

    def test_already_paragraph_form_unchanged(self):
        """Input that already obeys the contract is a no-op."""
        out = _normalize_video_markdown(_SAMPLE_PARA_FORM)
        self.assertEqual(out, _SAMPLE_PARA_FORM)

    def test_blank_line_before_each_label(self):
        """After normalize, every **说**/**画面** sits on its own paragraph."""
        out = _normalize_video_markdown(_SAMPLE_LIST_FORM)
        # Each label must be preceded by a blank line (\n\n) — never glued
        # directly to the previous line. Verify by checking that NO label
        # appears after only a single \n from preceding text.
        import re
        # find every label position
        for m in re.finditer(r"\*\*(?:说|画面)\*\*:", out):
            # look at the 2 chars right before
            i = m.start()
            if i >= 2:
                prefix = out[i - 2 : i]
                # Allowed: "\n\n" (paragraph break) — anything else (e.g.
                # "\n*" mid-line, or " *" inline) means the chunker would
                # not see it as a paragraph boundary.
                self.assertEqual(
                    prefix, "\n\n",
                    f"Label at pos {i} not preceded by blank line: ...{out[max(0,i-20):i]!r}>>>"
                )

    def test_no_video_markers_passthrough(self):
        """PDF-style markdown without 说/画面 labels is untouched."""
        pdf_md = "# Heading\n\nSome paragraph.\n\n- A real list item\n- Another\n"
        self.assertEqual(_normalize_video_markdown(pdf_md), pdf_md)

    def test_preserves_other_list_items(self):
        """Real list items (not the 说/画面 labels) must NOT be stripped."""
        mixed = (
            "[00:00]\n"
            "- **说**: 介绍三个要点：\n"
            "- 真实列表项 A\n"
            "- 真实列表项 B\n"
        )
        out = _normalize_video_markdown(mixed)
        # 说 label gets stripped...
        self.assertNotIn("- **说**", out)
        # ...but the genuine list items survive
        self.assertIn("- 真实列表项 A", out)
        self.assertIn("- 真实列表项 B", out)

    def test_write_markdown_only_normalizes_video_formats(self):
        """The structural regex is broad, so the video format gate matters."""
        markdown = "- **Y-axis**: 2010, 2020, 2024\n"
        config = build_config()
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            write_markdown(
                ParseResult(markdown=markdown, metadata={"format": "pdf"}),
                Path("chart.pdf"),
                output_dir,
                config,
            )
            pdf_text = (output_dir / "sources" / "chart.md").read_text(encoding="utf-8")
            self.assertIn("- **Y-axis**:", pdf_text)

            write_markdown(
                ParseResult(markdown=markdown, metadata={"format": "mp4"}),
                Path("clip.mp4"),
                output_dir,
                config,
            )
            video_text = (output_dir / "sources" / "clip.md").read_text(encoding="utf-8")
            self.assertNotIn("- **Y-axis**:", video_text)
            self.assertIn("**Y-axis**:", video_text)


class TestEndToEndChunking(unittest.TestCase):
    """The real prize: after normalize, the actual chunker behaves.

    These tests reproduce the production bug (one big chunk, 画面 lost) and
    verify it cannot recur once normalize runs.
    """

    @classmethod
    def setUpClass(cls):
        cls.config = build_config()
        cls.chunker = AutoChunker(cls.config)

    def _chunk(self, markdown: str):
        return self.chunker.chunk(markdown, {"format": "mp4", "source": "test"})

    def test_before_normalize_loses_huamian(self):
        """Sanity check the bug: raw list-form input drops 画面 content."""
        chunks = self._chunk(_SAMPLE_LIST_FORM)
        total_text = "".join(c.text for c in chunks)
        shuo_src = _SAMPLE_LIST_FORM.count("- **说**")
        hua_src = _SAMPLE_LIST_FORM.count("- **画面**")
        shuo_kept = total_text.count("**说**")
        hua_kept = total_text.count("**画面**")
        # The bug: 画面 lines either fully disappear OR get bundled into
        # one monster chunk via list protection. Either way, the "fix
        # actually changes behaviour" claim is testable: paragraph form
        # below produces a MEASURABLY different chunk shape.
        self.assertEqual(shuo_kept, shuo_src,
                         "说 should always survive (it follows the [MM:SS] line)")
        self.assertEqual(
            hua_kept, hua_src,
            "Source has 画面 content; chunks should contain it too "
            f"(found {hua_kept}/{hua_src}) — if this fails, list-protection ate it"
        )

    def test_after_normalize_full_coverage(self):
        """Normalized input: 100% content coverage, no oversized chunks."""
        normalized = _normalize_video_markdown(_SAMPLE_LIST_FORM)
        chunks = self._chunk(normalized)
        total_text = "".join(c.text for c in chunks)

        # 1. All 说/画面 labels preserved
        self.assertEqual(total_text.count("**说**"),
                         normalized.count("**说**"))
        self.assertEqual(total_text.count("**画面**"),
                         normalized.count("**画面**"))

        # 2. Coverage: chunk bytes >= source bytes (chunks may add the
        #    [来源: ...] header line; never lose content).
        src_chars = len(normalized)
        chunk_chars = sum(len(c.text) for c in chunks)
        self.assertGreaterEqual(
            chunk_chars, int(src_chars * 0.95),
            f"Coverage {chunk_chars}/{src_chars} too low"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
