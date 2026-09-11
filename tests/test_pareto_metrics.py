"""
Tests for opt-in Pareto (per-metric, lower-is-better) dominance comparison.

Regression coverage for: a program that improves one metric (e.g. ns_per_run__vfs_read_entry)
while regressing another (ns_per_run__vfs_write_entry) should NOT be treated as "better" when
`database.pareto_metric_prefixes` is configured - it should replace neither the MAP-Elites cell
owner, the archive, nor the tracked best program. Only a strictly-dominating candidate (<=
everywhere, < somewhere) should win. With the default empty `pareto_metric_prefixes`, behavior
must be unchanged (single combined_score comparison).
"""

import unittest

from openevolve.config import Config
from openevolve.database import Program, ProgramDatabase


def _make_program(pid, metrics, parent_id=None):
    return Program(id=pid, code=f"# program {pid}", language="python", metrics=metrics, parent_id=parent_id)


class TestParetoIsBetter(unittest.TestCase):
    def setUp(self):
        config = Config()
        config.database.in_memory = True
        config.database.num_islands = 1
        config.database.population_size = 100
        config.database.pareto_metric_prefixes = ["ns_per_run__"]
        self.db = ProgramDatabase(config.database)

    def test_dominating_candidate_is_better(self):
        original = _make_program("original", {"ns_per_run__read": 1000.0, "ns_per_run__write": 500.0})
        candidate = _make_program("candidate", {"ns_per_run__read": 900.0, "ns_per_run__write": 500.0})
        self.assertTrue(self.db._is_better(candidate, original))
        self.assertFalse(self.db._is_better(original, candidate))

    def test_mixed_result_is_not_better_either_way(self):
        original = _make_program("original", {"ns_per_run__read": 1039.76, "ns_per_run__write": 533.49})
        candidate = _make_program("candidate", {"ns_per_run__read": 921.03, "ns_per_run__write": 765.30})
        self.assertFalse(self.db._is_better(candidate, original))
        self.assertFalse(self.db._is_better(original, candidate))

    def test_identical_metrics_is_not_better(self):
        a = _make_program("a", {"ns_per_run__read": 1000.0})
        b = _make_program("b", {"ns_per_run__read": 1000.0})
        self.assertFalse(self.db._is_better(a, b))
        self.assertFalse(self.db._is_better(b, a))

    def test_missing_metric_falls_back_to_scalar(self):
        # Simulates a compile failure: no ns_per_run__* metrics at all, only combined_score.
        failed = _make_program("failed", {"combined_score": 0.0})
        compiled = _make_program("compiled", {"ns_per_run__read": 1000.0, "combined_score": 0.8})
        self.assertTrue(self.db._is_better(compiled, failed))
        self.assertFalse(self.db._is_better(failed, compiled))

    def test_default_config_is_scalar_only(self):
        config = Config()
        config.database.in_memory = True
        db = ProgramDatabase(config.database)  # pareto_metric_prefixes defaults to []
        original = _make_program("original", {"combined_score": 0.7, "ns_per_run__read": 1039.76, "ns_per_run__write": 533.49})
        candidate = _make_program("candidate", {"combined_score": 0.76, "ns_per_run__read": 921.03, "ns_per_run__write": 765.30})
        # Higher combined_score wins outright, mixed per-function results are irrelevant.
        self.assertTrue(db._is_better(candidate, original))


class TestParetoBestProgramTracking(unittest.TestCase):
    """End-to-end: reproduce the reported bpf_compile scenario through database.add()."""

    def setUp(self):
        config = Config()
        config.database.in_memory = True
        config.database.num_islands = 1
        config.database.population_size = 100
        config.database.pareto_metric_prefixes = ["ns_per_run__"]
        self.db = ProgramDatabase(config.database)

    def test_original_survives_mixed_challenger(self):
        original = _make_program(
            "original", {"ns_per_run__read": 1039.76, "ns_per_run__write": 533.49, "combined_score": 0.7452}
        )
        self.db.add(original)
        self.db.initial_program_id = original.id

        mixed_child = _make_program(
            "mixed_child",
            {"ns_per_run__read": 921.03, "ns_per_run__write": 765.30, "combined_score": 0.7603},
            parent_id=original.id,
        )
        self.db.add(mixed_child, iteration=1)

        # Despite a *higher* combined_score, the mixed challenger regresses write - original
        # must remain both the tracked best program and still present in the population.
        self.assertEqual(self.db.best_program_id, original.id)
        self.assertIn(original.id, self.db.programs)
        self.assertIn(mixed_child.id, self.db.programs)

    def test_dominating_child_replaces_original(self):
        original = _make_program("original", {"ns_per_run__read": 1039.76, "ns_per_run__write": 533.49})
        self.db.add(original)
        self.db.initial_program_id = original.id

        better_child = _make_program(
            "better_child", {"ns_per_run__read": 1000.0, "ns_per_run__write": 500.0}, parent_id=original.id
        )
        self.db.add(better_child, iteration=1)

        self.assertEqual(self.db.best_program_id, better_child.id)

    def test_pareto_front_protected_from_population_pruning(self):
        config = Config()
        config.database.in_memory = True
        config.database.num_islands = 1
        config.database.population_size = 2  # tiny, forces pruning
        config.database.pareto_metric_prefixes = ["ns_per_run__"]
        db = ProgramDatabase(config.database)

        read_champion = _make_program("read_champion", {"ns_per_run__read": 100.0, "ns_per_run__write": 900.0})
        write_champion = _make_program("write_champion", {"ns_per_run__read": 900.0, "ns_per_run__write": 100.0})
        db.add(read_champion)
        db.add(write_champion)
        # A third, dominated program should be free to be pruned; the two champions
        # (a genuine non-dominated front) must both survive even though population_size=2.
        dominated = _make_program("dominated", {"ns_per_run__read": 950.0, "ns_per_run__write": 950.0})
        db.add(dominated)

        self.assertIn(read_champion.id, db.programs)
        self.assertIn(write_champion.id, db.programs)


if __name__ == "__main__":
    unittest.main()
