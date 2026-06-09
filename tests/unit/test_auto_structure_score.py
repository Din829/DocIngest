"""
Unit tests for AutoChunker's structure scoring.

These focus on the routing signal, not on chunk emission. The important case
is CJK-heavy sections: len(text)//4 used to undercount them enough to make
valid heading sections look "too small".
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from docingest.chunkers import compute_structure_score


class TestAutoStructureScore(unittest.TestCase):
    def test_cjk_section_sizes_use_project_token_estimator(self):
        markdown = (
            "# First\n"
            + ("漢字" * 80)
            + "\n\n# Second\n"
            + ("漢字" * 80)
        )
        config = {
            "chunking": {
                "auto": {
                    "min_headings": 999,
                    "max_heading_gap_levels": 2,
                    "min_section_tokens": 100,
                    "max_section_tokens": 2000,
                    "section_size_pass_ratio": 0.5,
                }
            }
        }

        self.assertEqual(compute_structure_score(markdown, config), 2)

    def test_section_size_thresholds_are_configurable(self):
        markdown = (
            "# First\n"
            + ("漢字" * 80)
            + "\n\n# Second\n"
            + ("漢字" * 80)
        )
        config = {
            "chunking": {
                "auto": {
                    "min_headings": 999,
                    "max_heading_gap_levels": 2,
                    "min_section_tokens": 100,
                    "max_section_tokens": 200,
                    "section_size_pass_ratio": 0.5,
                }
            }
        }

        self.assertEqual(compute_structure_score(markdown, config), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
