import ast
import math
import os
import pathlib
import re
import subprocess
import unittest
from unittest import mock


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
    namespace = {"math": math, "os": os, "re": re, "subprocess": subprocess}
    exec(compile(module, str(CHIEF_PATH), "exec"), namespace)
    return namespace


class ChiefMemoryCapacityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace = load_selected_functions(
            "parse_slurm_memory_mb",
            "_slurm_allocation_memory_mb",
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

    def test_nested_spider_per_node_manager_limit_is_not_pilot_limit(self):
        get_limit = self.namespace['get_slurm_memory_limit_mb']
        env = dict(SLURM_JOB_ID='123', SLURM_MEM_PER_NODE='8192', SLURM_MEM_PER_CPU='8000')
        reply = mock.Mock(stdout='JobId=123 NumNodes=1 NumCPUs=8 MinMemoryCPU=8000M')
        with mock.patch.object(subprocess, 'run', return_value=reply) as run:
            self.assertEqual(get_limit(8, env), 64000)
        self.assertEqual(run.call_args.kwargs['timeout'], 5)
        self.assertEqual(env['SLURM_MEM_PER_NODE'], '8192')

    def test_nested_snellius_per_cpu_parent_does_not_override_node_grant(self):
        get_limit = self.namespace['get_slurm_memory_limit_mb']
        env = dict(SLURM_JOB_ID='456', SLURM_MEM_PER_NODE='344064', SLURM_MEM_PER_CPU='8000')
        reply = mock.Mock(stdout='JobId=456 NumNodes=1 NumCPUs=192 MinMemoryNode=336G')
        with mock.patch.object(subprocess, 'run', return_value=reply):
            self.assertEqual(get_limit(192, env), 344064)

    def test_base_array_job_selects_its_exact_record_not_last_sibling(self):
        env = dict(SLURM_JOB_ID='123', SLURM_MEM_PER_NODE='8192', SLURM_MEM_PER_CPU='8000')
        reply = mock.Mock(stdout='JobId=123 NumNodes=1 NumCPUs=8 MinMemoryCPU=8000M\n'
                         'JobId=124 NumNodes=1 NumCPUs=32 MinMemoryNode=256G\n')
        with mock.patch.object(subprocess, 'run', return_value=reply):
            self.assertEqual(self.namespace['get_slurm_memory_limit_mb'](8, env), 64000)

    def test_conflicting_modes_fail_conservatively_when_lookup_unavailable(self):
        get_limit = self.namespace['get_slurm_memory_limit_mb']
        for node, cpu, expected in [('8192', '8000', 8192), ('64000', '512', 4096)]:
            env = dict(SLURM_JOB_ID='123', SLURM_MEM_PER_NODE=node, SLURM_MEM_PER_CPU=cpu)
            with mock.patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('scontrol', 5)):
                self.assertEqual(get_limit(8, env), expected)

    def test_wrong_or_multinode_allocation_cannot_inflate_memory(self):
        get_limit = self.namespace['get_slurm_memory_limit_mb']
        env = dict(SLURM_JOB_ID='123', SLURM_MEM_PER_NODE='8192', SLURM_MEM_PER_CPU='8000')
        for fields in ('JobId=999 NumNodes=1 NumCPUs=8 MinMemoryCPU=8000M',
                       'JobId=123 NumNodes=2 NumCPUs=16 MinMemoryCPU=8000M',
                       'JobId=123 NumNodes=1 NumCPUs=4 MinMemoryCPU=8000M'):
            with mock.patch.object(subprocess, 'run', return_value=mock.Mock(stdout=fields)):
                self.assertEqual(get_limit(8, env), 8192)

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
