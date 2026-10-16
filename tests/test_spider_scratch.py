import ast
import getpass
import math
import os
import pathlib
import shutil
import sys
import traceback
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
CHIEF_PATH = ROOT / "zslurm_chief"


def load_selected_functions(*names):
    tree = ast.parse(CHIEF_PATH.read_text(encoding="utf-8"))
    selected = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    namespace = {
        "config": {}, "getpass": getpass, "math": math, "os": os, "shutil": shutil,
        "sys": sys, "traceback": traceback, "xtime": lambda: "now",
    }
    exec(compile(module, str(CHIEF_PATH), "exec"), namespace)
    return namespace


class SpiderScratchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace = load_selected_functions(
            "configured_scratch_capacity_gb",
            "_configured_scratch_paths",
            "detect_scratch_path",
            "read_scratch_usage",
        )

    def test_spider_tmpdir_is_detected_inside_slurm_allocation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as scratch:
            detected = self.namespace["detect_scratch_path"](
                "123",
                {"SLURM_JOB_ID": "123", "TMPDIR": scratch},
                allow_tmpdir=True,
            )
        self.assertEqual(detected, os.path.realpath(scratch))

    def test_capacity_is_bounded_per_partial_pilot(self):
        capacity = self.namespace["configured_scratch_capacity_gb"](
            {"scratch_capacity_gb": 2200}, 30
        )
        usage = SimpleNamespace(
            total=12 * 1024 ** 4,
            free=10 * 1024 ** 4,
        )
        with mock.patch.object(shutil, "disk_usage", return_value=usage):
            total, used = self.namespace["read_scratch_usage"](
                "/tmp", capacity
            )
        self.assertEqual(total, 2200.0)
        self.assertEqual(used, 0.0)

    def test_spider_capacity_scales_with_partial_pilot_size(self):
        capacity = self.namespace["configured_scratch_capacity_gb"](
            {"scratch_capacity_gb_per_core": 73}, 2
        )

        self.assertEqual(capacity, 146.0)


if __name__ == "__main__":
    unittest.main()
