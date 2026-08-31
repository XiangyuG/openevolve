"""
Tests for code utilities in openevolve.utils.code_utils
"""

import unittest

from openevolve.utils.code_utils import (
    _format_block_lines,
    apply_diff,
    build_heimdall_witness_file,
    extract_diffs,
    format_diff_summary,
)


class TestCodeUtils(unittest.TestCase):
    """Tests for code utilities"""

    def test_extract_diffs(self):
        """Test extracting diffs from a response"""
        diff_text = """
        Let's improve this code:

        <<<<<<< SEARCH
        def hello():
            print("Hello")
        =======
        def hello():
            print("Hello, World!")
        >>>>>>> REPLACE

        Another change:

        <<<<<<< SEARCH
        x = 1
        =======
        x = 2
        >>>>>>> REPLACE
        """

        diffs = extract_diffs(diff_text)
        self.assertEqual(len(diffs), 2)
        self.assertEqual(
            diffs[0][0],
            """        def hello():
            print(\"Hello\")""",
        )
        self.assertEqual(
            diffs[0][1],
            """        def hello():
            print(\"Hello, World!\")""",
        )
        self.assertEqual(diffs[1][0], "        x = 1")
        self.assertEqual(diffs[1][1], "        x = 2")

    def test_apply_diff(self):
        """Test applying diffs to code"""
        original_code = """
        def hello():
            print("Hello")

        x = 1
        y = 2
        """

        diff_text = """
        <<<<<<< SEARCH
        def hello():
            print("Hello")
        =======
        def hello():
            print("Hello, World!")
        >>>>>>> REPLACE

        <<<<<<< SEARCH
        x = 1
        =======
        x = 2
        >>>>>>> REPLACE
        """

        expected_code = """
        def hello():
            print("Hello, World!")

        x = 2
        y = 2
        """

        result = apply_diff(original_code, diff_text)

        # Normalize whitespace for comparison
        self.assertEqual(
            result,
            expected_code,
        )


class TestFormatDiffSummary(unittest.TestCase):
    """Tests for format_diff_summary showing actual diff content"""

    def test_single_line_changes(self):
        """Single-line changes should show inline format"""
        diff_blocks = [("x = 1", "x = 2")]
        result = format_diff_summary(diff_blocks)
        self.assertEqual(result, "Change 1: 'x = 1' to 'x = 2'")

    def test_multi_line_changes_show_actual_content(self):
        """Multi-line changes should show actual SEARCH/REPLACE content"""
        diff_blocks = [
            (
                "def old():\n    return False",
                "def new():\n    return True",
            )
        ]
        result = format_diff_summary(diff_blocks)
        # Should contain actual code, not "2 lines"
        self.assertIn("def old():", result)
        self.assertIn("return False", result)
        self.assertIn("def new():", result)
        self.assertIn("return True", result)
        self.assertIn("Replace:", result)
        self.assertIn("with:", result)
        # Should NOT contain generic line count
        self.assertNotIn("2 lines", result)

    def test_multiple_diff_blocks(self):
        """Multiple diff blocks should be numbered"""
        diff_blocks = [
            ("a = 1", "a = 2"),
            ("def foo():\n    pass", "def bar():\n    return 1"),
        ]
        result = format_diff_summary(diff_blocks)
        self.assertIn("Change 1:", result)
        self.assertIn("Change 2:", result)
        self.assertIn("'a = 1' to 'a = 2'", result)
        self.assertIn("def foo():", result)
        self.assertIn("def bar():", result)

    def test_configurable_max_line_len(self):
        """max_line_len parameter should control line truncation"""
        long_line = "x" * 50
        # Must be multi-line to trigger block format (single-line uses inline format)
        diff_blocks = [(long_line + "\nline2", "short\nline2")]
        # With default (100), no truncation
        result_default = format_diff_summary(diff_blocks)
        self.assertNotIn("...", result_default)
        # With max_line_len=30, should truncate the long line
        result_short = format_diff_summary(diff_blocks, max_line_len=30)
        self.assertIn("...", result_short)

    def test_configurable_max_lines(self):
        """max_lines parameter should control block truncation"""
        many_lines = "\n".join([f"line{i}" for i in range(20)])
        diff_blocks = [(many_lines, "replacement")]
        # With max_lines=10, should truncate
        result = format_diff_summary(diff_blocks, max_lines=10)
        self.assertIn("... (10 more lines)", result)

    def test_block_lines_basic_formatting(self):
        """Lines should be indented with 2 spaces"""
        lines = ["line1", "line2"]
        result = _format_block_lines(lines)
        self.assertEqual(result, "  line1\n  line2")

    def test_block_lines_long_line_truncation(self):
        """Lines over 100 chars should be truncated by default"""
        long_line = "x" * 150
        result = _format_block_lines([long_line])
        self.assertIn("...", result)
        self.assertLess(len(result.split("\n")[0]), 110)

    def test_block_lines_many_lines_truncation(self):
        """More than 30 lines should show truncation message by default"""
        lines = [f"line{i}" for i in range(50)]
        result = _format_block_lines(lines)
        self.assertIn("... (20 more lines)", result)
        self.assertEqual(len(result.split("\n")), 31)

    def test_block_lines_empty_input(self):
        """Empty input should return '(empty)'"""
        result = _format_block_lines([])
        self.assertEqual(result, "  (empty)")


class TestBuildHeimdallWitnessFile(unittest.TestCase):
    """build_heimdall_witness_file() -> heimdall `--witness` schema"""

    def test_empty(self):
        out = build_heimdall_witness_file([])
        self.assertEqual(
            out,
            {
                "witness": {
                    "version": "0.1",
                    "name": "openevolve_transform",
                    "bindings": [],
                    "assumptions": [],
                    "observations": [],
                }
            },
        )

    def test_map_width_change_becomes_map_correspondence(self):
        out = build_heimdall_witness_file(
            [{"index": 1, "map_width_change": {"map": "counts", "old_bytes": 8, "new_bytes": 4}}]
        )
        bindings = out["witness"]["bindings"]
        self.assertEqual(len(bindings), 1)
        b = bindings[0]
        self.assertEqual(b["name"], "counts")
        mc = b["relation"]["map_correspondence"]
        self.assertEqual(mc["original_key"], "k")
        self.assertEqual(mc["optimized_key"], "k")
        self.assertEqual(
            mc["value_relation"]["equal"]["right"],
            {"truncate": {"value": "original.value", "width": 32}},
        )

    def test_non_narrowing_and_other_hints_skipped(self):
        out = build_heimdall_witness_file(
            [
                {"map_width_change": {"map": "a", "old_bytes": 4, "new_bytes": 8}},  # widening
                {"map_width_change": {"map": "b", "old_bytes": 4, "new_bytes": 4}},  # no-op
                {"variable_width_change": {"var": "x", "old_bits": 32, "new_bits": 16}},
                {"map_fusion": {"target": "t", "sources": [{"map": "s", "value_offset_bytes": 0, "value_bytes": 4}]}},
                {"summary": "structural only"},
            ]
        )
        self.assertEqual(out["witness"]["bindings"], [])


if __name__ == "__main__":
    unittest.main()
