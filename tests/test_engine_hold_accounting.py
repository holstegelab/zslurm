import ast
import importlib.machinery
import importlib.util
import io
import pathlib
import threading
import unittest
from unittest import mock


ZSLURM_PATH = pathlib.Path(__file__).resolve().parents[1] / "zslurm"


def load_zslurm():
    loader = importlib.machinery.SourceFileLoader(
        "zslurm_engine_hold_under_test", str(ZSLURM_PATH)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def fake_process(returncode=0, stdout=b"", stderr=b""):
    process = mock.Mock()
    process.returncode = returncode
    process.communicate.return_value = (stdout, stderr)
    process.wait.return_value = returncode
    return process


class EngineHoldAccountingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zslurm = load_zslurm()

    def setUp(self):
        z = self.zslurm
        z.gb.lock = threading.RLock()
        z.gb.log_file = io.StringIO()
        z.gb.wlog = None
        z.status.instance_name = "test-instance"
        z.status.staging_partition = "staging"
        z.status.ssd_feature_name = "scratch-node"

    def test_hold_reason_detection(self):
        self.assertTrue(self.zslurm._slurm_reason_is_hold("JobHeldUser"))
        self.assertTrue(self.zslurm._slurm_reason_is_hold("JobHeldAdmin"))
        self.assertFalse(self.zslurm._slurm_reason_is_hold("Priority"))
        self.assertFalse(self.zslurm._slurm_reason_is_hold(None))

    def test_controller_never_reconciles_queued_engines(self):
        tree = ast.parse(ZSLURM_PATH.read_text())
        controller = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "thread_check_commands"
        )
        calls = [
            node.func.attr
            for node in ast.walk(controller)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        ]

        self.assertNotIn("hold_or_cancel_queued", calls)
        self.assertNotIn("release_held", calls)

    def test_failed_hold_stays_queued_without_cancellation(self):
        manager = self.zslurm.EngineManager()
        engine = self.zslurm.Engine(cluster_id="123_4", partition="compute")
        engine.slurm_job_id = "9004"
        manager.engine_by_clusterid["123_4"] = engine
        failed_hold = fake_process(
            returncode=1, stderr=b"Access/permission denied"
        )

        with mock.patch.object(
            self.zslurm,
            "Popen",
            return_value=failed_hold,
        ) as popen:
            done = manager.hold_or_cancel_queued(
                self.zslurm.gb, ["123_4"], action="hold"
            )

        self.assertEqual(done, [])
        self.assertEqual(manager.held_engine_cids, set())
        self.assertIn("123_4", manager.engine_by_clusterid)
        self.assertEqual(
            [call.args[0] for call in popen.call_args_list],
            [["scontrol", "hold", "9004"]],
        )
        self.assertIn("Access/permission denied", self.zslurm.gb.log_file.getvalue())
        self.assertNotIn("scancel", self.zslurm.gb.log_file.getvalue())

    def test_failed_release_keeps_manager_hold(self):
        manager = self.zslurm.EngineManager()
        manager.held_engine_cids.add("123_4")
        process = fake_process(returncode=1, stderr=b"Access denied")

        with mock.patch.object(self.zslurm, "Popen", return_value=process):
            done = manager.release_held(self.zslurm.gb, ["123_4"])

        self.assertEqual(done, [])
        self.assertEqual(manager.held_engine_cids, {"123_4"})

    def test_successful_hold_and_release_update_manager_ownership(self):
        manager = self.zslurm.EngineManager()
        engine = self.zslurm.Engine(cluster_id="123_4", partition="compute")
        engine.slurm_job_id = "9004"
        manager.engine_by_clusterid["123_4"] = engine

        with mock.patch.object(
            self.zslurm, "Popen", return_value=fake_process()
        ) as popen:
            held = manager.hold_or_cancel_queued(
                self.zslurm.gb, ["123_4"], action="hold"
            )
            released = manager.release_held(self.zslurm.gb, ["123_4"])

        self.assertEqual(held, ["123_4"])
        self.assertEqual(released, ["123_4"])
        self.assertEqual(manager.held_engine_cids, set())
        self.assertEqual(
            [call.args[0] for call in popen.call_args_list],
            [
                ["scontrol", "hold", "9004"],
                ["scontrol", "release", "9004"],
            ],
        )

    def test_squeue_reason_is_authoritative_for_hold_accounting(self):
        manager = self.zslurm.EngineManager()
        manager.held_engine_cids = {"123_0", "123_1"}
        output = "\n".join(
            [
                "9000|123|0|test-instance|5-00:00:00|PD||genoa|01:00:00|scratch-node|192|JobHeldUser",
                "9001|123|1|test-instance|5-00:00:00|PD||genoa|01:00:00|scratch-node|192|Priority",
                "9002|123|2|test-instance|5-00:00:00|PD||genoa|01:00:00|scratch-node|192|JobHeldAdmin",
                "9003|123|3|test-instance|5-00:00:00|R|tcn1|genoa|00:00:00|scratch-node|192|None",
            ]
        )

        with mock.patch.object(
            self.zslurm,
            "Popen",
            return_value=fake_process(stdout=output.encode("utf-8")),
        ):
            manager._check_cluster_engines()

        self.assertEqual(manager.cluster_running_engines, 1)
        self.assertEqual(manager.cluster_queued_engines, 1)
        self.assertEqual(manager.cluster_held_engines, 2)
        self.assertEqual(manager.cluster_held_by_partition, {"genoa": 2})
        self.assertEqual(manager.held_engine_cids, {"123_0"})
        self.assertEqual(manager.actual_held_engine_cids, {"123_0", "123_2"})
        self.assertEqual(
            [row[0] for row in manager.held_engines_detail], ["123_0"]
        )
        self.assertEqual(
            [row[0] for row in manager.external_held_engines_detail], ["123_2"]
        )
        self.assertEqual(
            [row[0] for row in manager.queued_engines_detail], ["123_1"]
        )


if __name__ == "__main__":
    unittest.main()
