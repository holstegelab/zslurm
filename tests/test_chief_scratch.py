import ast
import getpass
import os
import pathlib
import shutil
import tempfile
import unittest
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
        "getpass": getpass,
        "os": os,
        "shutil": shutil,
        "tempfile": tempfile,
        "sys": mock.Mock(),
        "traceback": __import__("traceback"),
        "xtime": lambda: "now",
    }
    exec(compile(module, str(CHIEF_PATH), "exec"), namespace)
    return namespace


class ChiefScratchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace = load_selected_functions(
            "detect_scratch_path",
            "configured_scratch_capacity_gb",
            "read_scratch_usage",
            "prepare_job_scratch",
            "cleanup_job_scratch",
            "configure_job_scratch_environment",
        )

    def test_spider_tmpdir_requires_slurm_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            detect = self.namespace["detect_scratch_path"]
            self.assertEqual(
                detect(
                    "41279611",
                    {"SLURM_JOB_ID": "41279611", "TMPDIR": temporary},
                    allow_tmpdir=True,
                ),
                os.path.realpath(temporary),
            )
            self.assertIsNone(detect(
                "41279611",
                {"SLURM_JOB_ID": "41279611", "TMPDIR": temporary},
            ))
            self.assertIsNone(detect("", {"TMPDIR": temporary}))

    def test_explicit_engine_scratch_wins(self):
        with tempfile.TemporaryDirectory() as explicit, \
                tempfile.TemporaryDirectory() as native:
            detect = self.namespace["detect_scratch_path"]
            self.assertEqual(
                detect("1", {
                    "SLURM_JOB_ID": "1",
                    "ZSLURM_ENGINE_SCRATCH_DIR": explicit,
                    "TMPDIR": native,
                }),
                os.path.realpath(explicit),
            )

    def test_partial_pilot_capacity_is_core_bounded(self):
        capacity = self.namespace["configured_scratch_capacity_gb"]
        self.assertEqual(
            capacity({"scratch_capacity_gb_per_core": 100}, 30), 3000.0
        )
        self.assertEqual(
            capacity({
                "scratch_capacity_gb": 2500,
                "scratch_capacity_gb_per_core": 100,
            }, 30),
            2500.0,
        )

    def test_child_directories_are_unique_and_cleanup_is_scoped(self):
        prepare = self.namespace["prepare_job_scratch"]
        cleanup = self.namespace["cleanup_job_scratch"]
        with tempfile.TemporaryDirectory() as root:
            first = prepare(root, "job/one")
            second = prepare(root, "job-two")
            pathlib.Path(first, "data").write_text("one", encoding="utf-8")
            pathlib.Path(second, "data").write_text("two", encoding="utf-8")

            cleanup(root, first)

            self.assertFalse(os.path.exists(first))
            self.assertTrue(os.path.isfile(os.path.join(second, "data")))
            with self.assertRaisesRegex(RuntimeError, "unsafe scratch cleanup"):
                cleanup(root, root)

    def test_repeated_job_id_gets_distinct_attempt_directories(self):
        prepare = self.namespace["prepare_job_scratch"]
        cleanup = self.namespace["cleanup_job_scratch"]
        with tempfile.TemporaryDirectory() as root:
            first = prepare(root, "same-id")
            pathlib.Path(first, "still-running").write_text(
                "first", encoding="utf-8"
            )
            second = prepare(root, "same-id")

            self.assertNotEqual(first, second)
            self.assertTrue(os.path.isfile(os.path.join(first, "still-running")))
            cleanup(root, first)
            cleanup(root, second)

    def test_child_environment_uses_only_its_private_directory(self):
        configure = self.namespace["configure_job_scratch_environment"]
        cleanup = self.namespace["cleanup_job_scratch"]
        with tempfile.TemporaryDirectory() as root:
            environment = {"TMPDIR": "/controller/tmp"}
            child = configure(environment, root, "42")

            self.assertEqual(environment["ZSLURM_SCRATCH_ROOT"], root)
            self.assertEqual(environment["ZSLURM_SCRATCH_DIR"], child)
            for name in ("TMPDIR", "TMP", "TEMP", "TEMPDIR"):
                self.assertEqual(environment[name], child)
            self.assertEqual(os.stat(child).st_mode & 0o777, 0o700)
            cleanup(root, child)


if __name__ == "__main__":
    unittest.main()
