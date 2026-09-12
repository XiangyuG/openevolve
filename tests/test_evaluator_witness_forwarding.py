"""
Tests for Evaluator.evaluate_program's optional witnesses/wit_path forwarding.

Regression coverage for the single-pass equivalence check redesign: evaluate_program
gained optional `witnesses`/`wit_path` kwargs, forwarded to the evaluation module's
`evaluate()` only when its signature declares them (mirrors the same
inspect.signature-based pattern already used elsewhere for optional hooks). Every
existing single-arg `evaluate(program_path)` evaluator must be completely unaffected.
"""

import asyncio
import os
import tempfile
import unittest

from openevolve.config import EvaluatorConfig
from openevolve.evaluator import Evaluator


class TestEvaluatorWitnessForwarding(unittest.TestCase):
    def _make_evaluator(self, source: str) -> Evaluator:
        eval_file = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
        eval_file.write(source)
        eval_file.close()
        self.addCleanup(os.unlink, eval_file.name)

        config = EvaluatorConfig()
        config.cascade_evaluation = False
        config.max_retries = 0
        return Evaluator(config=config, evaluation_file=eval_file.name)

    def test_forwards_witnesses_and_wit_path_when_declared(self):
        evaluator = self._make_evaluator(
            "def evaluate(program_path, witnesses=None, wit_path=None):\n"
            "    return {\n"
            "        'saw_witnesses': float(bool(witnesses)),\n"
            "        'saw_wit_path': float(bool(wit_path)),\n"
            "    }\n"
        )

        async def run_test():
            return await evaluator.evaluate_program(
                "code",
                "id1",
                witnesses=[{"summary": "narrow a map"}],
                wit_path="/tmp/does-not-need-to-exist.wit",
            )

        result = asyncio.run(run_test())
        self.assertEqual(result["saw_witnesses"], 1.0)
        self.assertEqual(result["saw_wit_path"], 1.0)

    def test_plain_single_arg_evaluator_is_unaffected(self):
        # No witnesses/wit_path parameter at all - must not raise a TypeError when
        # evaluate_program is called with those kwargs anyway.
        evaluator = self._make_evaluator(
            "def evaluate(program_path):\n" "    return {'score': 1.0}\n"
        )

        async def run_test():
            return await evaluator.evaluate_program(
                "code",
                "id2",
                witnesses=[{"summary": "narrow a map"}],
                wit_path="/tmp/does-not-need-to-exist.wit",
            )

        result = asyncio.run(run_test())
        self.assertEqual(result["score"], 1.0)

    def test_no_witnesses_passed_is_a_no_op(self):
        evaluator = self._make_evaluator(
            "def evaluate(program_path, witnesses=None, wit_path=None):\n"
            "    return {\n"
            "        'saw_witnesses': float(bool(witnesses)),\n"
            "        'saw_wit_path': float(bool(wit_path)),\n"
            "    }\n"
        )

        async def run_test():
            return await evaluator.evaluate_program("code", "id3")

        result = asyncio.run(run_test())
        self.assertEqual(result["saw_witnesses"], 0.0)
        self.assertEqual(result["saw_wit_path"], 0.0)


if __name__ == "__main__":
    unittest.main()
