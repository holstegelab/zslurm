import ast
import math
import os
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CHIEF_PATH = ROOT / "zslurm_chief"


def load_selected_functions(*names):
    tree = ast.parse(CHIEF_PATH.read_text(encoding="utf-8"))
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    namespace = {"math": math, "os": os}
    exec(compile(module, str(CHIEF_PATH), "exec"), namespace)
    return namespace


class ChiefMemoryCapacityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace = load_selected_functions(
            "parse_slurm_memory_mb",
            "get_slurm_memory_limit_mb",
            "compute_engine_memory_capacity_mb",
        )

    def test_slurm_memory_parser_accepts_mib_and_suffixes(self):
        parse = self.namespace["parse_slurm_memory_mb"]

        self.assertEqual(parse("344064"), 344064.0)
        self.assertEqual(parse("336G"), 344064.0)
        self.assertEqual(parse("1.5TB"), 1.5 * 1024.0 ** 2)
        self.assertIsNone(parse("N/A"))
        self.assertIsNone(parse("broken"))

    def test_per_node_slurm_limit_is_authoritative(self):
        get_limit = self.namespace["get_slurm_memory_limit_mb"]

        self.assertEqual(
            get_limit(
                192,
                {
                    "SLURM_MEM_PER_NODE": "344064",
                    "SLURM_MEM_PER_CPU": "1792",
                },
            ),
            344064.0,
        )

    def test_per_cpu_slurm_limit_is_valid_fallback(self):
        get_limit = self.namespace["get_slurm_memory_limit_mb"]

        self.assertEqual(
            get_limit(192, {"SLURM_MEM_PER_CPU": "1792"}),
            344064.0,
        )

    def test_incident_shape_is_capped_below_slurm_cgroup(self):
        compute = self.namespace["compute_engine_memory_capacity_mb"]

        total, schedulable, hard_limit = compute(
            physical_memory_mb=386474.96,
            cgroup_limit_mb=386474.96,
            slurm_limit_mb=344064.0,
            static_reserve_mb=100.0,
            headroom_fraction=0.08,
            cap_fraction=0.99,
        )

        self.assertEqual(hard_limit, 344064.0)
        self.assertEqual(total, 343964.0)
        self.assertAlmostEqual(schedulable, 316446.88)
        self.assertLess(schedulable, 344064.0)
        self.assertLess(schedulable, 353850.0)

    def test_tightest_of_explicit_cgroup_and_slurm_limits_wins(self):
        compute = self.namespace["compute_engine_memory_capacity_mb"]

        _, _, hard_limit = compute(
            physical_memory_mb=500000,
            explicit_memory_mb=330000,
            cgroup_limit_mb=320000,
            slurm_limit_mb=344064,
        )

        self.assertEqual(hard_limit, 320000.0)


if __name__ == "__main__":
    unittest.main()
