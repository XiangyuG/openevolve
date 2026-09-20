"""
Tests for examples/bpf_compile/evaluator.py's _save_candidate() and _log().

examples/bpf_compile/evaluator.py isn't a package (no __init__.py) and pulls in
heimdall/BTF-specific globals at import time, so it's loaded directly from its
file path rather than via a normal package import. Both functions under test
have no external dependencies (no clang, no BTF) -- plain file I/O / logging --
so they're safe to exercise in isolation.

Regression coverage for two things:
  * _save_candidate(): filenames gained an `iterNNNN_` prefix when an
    `iteration` number is available (forwarded from OpenEvolve via
    evaluate()'s `iteration` param -- see test_evaluator_witness_forwarding.py),
    so a developer can tell which iteration produced a saved candidate
    without opening its .json sidecar.
  * _log(): now routed through `logging.getLogger("bpf_eval")` instead of a
    bare print() -- print() only ever reached the terminal's live scrollback,
    never OpenEvolve's own <output_dir>/logs/openevolve_*.log file, so a
    run's per-iteration eval progress ("compiling...", "checking entry
    X...", timeouts) was unrecoverable once the terminal was gone.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
_EVALUATOR_PATH = os.path.join(HERE, "..", "examples", "bpf_compile", "evaluator.py")

_spec = importlib.util.spec_from_file_location("bpf_compile_evaluator_under_test", _EVALUATOR_PATH)
bpf_evaluator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bpf_evaluator)


class TestSaveCandidate(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.save_dir = Path(self.tmp_dir) / "generated_programs" / "filetop"
        self._patches = [
            patch.object(bpf_evaluator, "SAVE_DIR", self.save_dir),
            patch.object(bpf_evaluator, "SAVE_PROGRAMS", True),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_filename_prefixed_with_iteration_when_known(self):
        bpf_evaluator._save_candidate("int main(){}", {"score": 1.0}, iteration=7)

        names = [p.name for p in self.save_dir.glob("*.bpf.c")]
        self.assertEqual(len(names), 1, names)
        self.assertTrue(names[0].startswith("iter0007_"), names[0])

    def test_filename_zero_padded_for_larger_iteration_numbers(self):
        bpf_evaluator._save_candidate("int main(){}", {"score": 1.0}, iteration=123)

        names = [p.name for p in self.save_dir.glob("*.bpf.c")]
        self.assertEqual(len(names), 1, names)
        self.assertTrue(names[0].startswith("iter0123_"), names[0])

    def test_no_iteration_prefix_when_iteration_unknown(self):
        """Matches direct/manual invocation of evaluate() outside OpenEvolve's
        own iteration loop (see the README's "Smoke-test the evaluator
        directly" usage) -- historical filename shape, no "iterNNNN_" prefix."""
        bpf_evaluator._save_candidate("int main(){}", {"score": 1.0})

        names = [p.name for p in self.save_dir.glob("*.bpf.c")]
        self.assertEqual(len(names), 1, names)
        self.assertFalse(names[0].startswith("iter"), names[0])

    def test_json_sidecar_gets_the_same_stem(self):
        bpf_evaluator._save_candidate("int main(){}", {"score": 1.0}, iteration=2)

        c_files = list(self.save_dir.glob("*.bpf.c"))
        json_files = list(self.save_dir.glob("*.json"))
        self.assertEqual(len(c_files), 1)
        self.assertEqual(len(json_files), 1)
        # Same stem before the ".bpf.c"/".json" suffix.
        self.assertEqual(c_files[0].name[: -len(".bpf.c")], json_files[0].name[: -len(".json")])

    def test_save_programs_disabled_writes_nothing(self):
        with patch.object(bpf_evaluator, "SAVE_PROGRAMS", False):
            bpf_evaluator._save_candidate("int main(){}", {"score": 1.0}, iteration=1)
        self.assertFalse(self.save_dir.exists())


class TestLogRoutedThroughLogging(unittest.TestCase):
    """_log() must go through `logging`, not print() -- see module docstring.

    Records land on a logger named "bpf_eval" (not the root logger directly)
    so a caller can attach/inspect handlers without fighting over root; in
    the real run, propagation (on by default) carries them up to whatever
    handlers OpenEvolve's own controller._setup_logging() already attached
    to root (console + <output_dir>/logs/openevolve_*.log).
    """

    def test_log_emits_an_info_record_on_the_bpf_eval_logger(self):
        with self.assertLogs("bpf_eval", level="INFO") as cm:
            bpf_evaluator._log("compiling candidate with clang -target bpf ...")
        self.assertEqual(
            cm.output,
            ["INFO:bpf_eval:[bpf_eval] compiling candidate with clang -target bpf ..."],
        )

    def test_log_silent_when_verbose_disabled(self):
        with patch.object(bpf_evaluator, "_VERBOSE", False):
            with self.assertRaises(AssertionError):
                # assertLogs itself raises AssertionError when nothing was logged.
                with self.assertLogs("bpf_eval", level="INFO"):
                    bpf_evaluator._log("should not appear")


if __name__ == "__main__":
    unittest.main()
