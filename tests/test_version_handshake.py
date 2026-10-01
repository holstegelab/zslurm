import ast
import importlib.machinery
import importlib.util
import io
import pathlib
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest import mock

from zslurm_version import VERSION


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_manager():
    loader = importlib.machinery.SourceFileLoader("zslurm_version_under_test", str(ROOT / "zslurm"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_chief_registration():
    tree = ast.parse((ROOT / "zslurm_chief").read_text(encoding="utf-8"))
    selected = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "register_with_manager"
        or isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_warned_legacy_manager"
            for target in node.targets
        )
    ]
    namespace = {"VERSION": VERSION, "sys": types.SimpleNamespace(stderr=io.StringIO())}
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    exec(compile(module, str(ROOT / "zslurm_chief"), "exec"), namespace)
    return namespace


class VersionHandshakeTests(unittest.TestCase):
    def setUp(self):
        self.manager = load_manager()
        self.manager.gb.lock = threading.RLock()
        self.manager.gb.log_file = io.StringIO()
        self.manager.gb.wlog = None
        self.manager.status.address = "manager"
        self.manager.status.instance_name = "test"
        self.manager.status.handover_mode = "ACTIVE"
        self.manager.engines = self.manager.EngineManager()

    def register(self, chief_version=None):
        args = ("node1", 2, 16000, "compute", "123", 100.0, 0.0, "test")
        if chief_version is None:
            return self.manager.register(*args)
        return self.manager.register_versioned(*(args + (chief_version,)))

    def test_version_cli_exits_without_starting_manager_or_chief(self):
        for script in ("zslurm", "zslurm_chief"):
            with self.subTest(script=script):
                result = subprocess.run(
                    [sys.executable, str(ROOT / script), "--version"],
                    capture_output=True, text=True, check=True,
                )
                self.assertEqual(result.stdout.strip(), f"{script} {VERSION}")

    def test_matching_chief_registers_without_warning(self):
        self.assertEqual(self.register(VERSION), "node1")
        self.assertNotIn("WARNING", self.manager.gb.log_file.getvalue())
        self.assertEqual(self.manager.engines.list_nodes()[0][-1], VERSION)
        self.assertEqual(self.manager.ping()["version"], VERSION)

    def test_legacy_chief_warns_and_remains_registered(self):
        self.assertEqual(self.register(), "node1")
        log = self.manager.gb.log_file.getvalue()
        self.assertIn("WARNING: engine node1", log)
        self.assertIn("did not report a version", log)
        self.assertIsNone(self.manager.engines.list_nodes()[0][-1])

    def test_mismatched_chief_warns_and_remains_registered(self):
        self.assertEqual(self.register("0.1.0"), "node1")
        log = self.manager.gb.log_file.getvalue()
        self.assertIn("chief version 0.1.0 differs", log)
        self.assertIn(f"manager version {VERSION}", log)

    def test_slurm_worker_on_manager_host_is_not_a_local_engine(self):
        for hostname, cluster_id in (
            ("wn-dc-14.spider.surfsara.nl", "41383005_4"),
            ("gcn42.snellius.surf.nl", "123456"),
        ):
            with self.subTest(hostname=hostname):
                self.manager.engines = self.manager.EngineManager()
                self.manager.status.address = hostname
                engine_id = self.manager.engines.register(
                    hostname, 30, 240000, "compute", cluster_id, 0, 0,
                    "original-instance", VERSION,
                )
                engine = self.manager.engines.engine_by_id[engine_id]
                self.assertEqual(engine.cluster_id, cluster_id)
                self.assertIs(
                    self.manager.engines.engine_by_clusterid[cluster_id], engine
                )
                self.assertTrue(engine.managed)
                self.assertEqual(engine.instance, "original-instance")

    def test_locality_depends_on_absent_allocation_not_hostname(self):
        for cluster_id in (None, "", False):
            with self.subTest(cluster_id=cluster_id):
                self.manager.engines = self.manager.EngineManager()
                engine_id = self.manager.engines.register(
                    "different-host", 2, 16000, "compute", cluster_id,
                    0, 0, "test", VERSION,
                )
                self.assertIs(
                    self.manager.engines.engine_by_id[engine_id].cluster_id,
                    False,
                )
                self.assertFalse(self.manager.engines.engine_by_clusterid)

    def test_late_migration_registration_refreshes_heartbeat_without_losing_state(self):
        z = self.manager
        z.status.handover_mode = "TARGET_MIGRATING"
        engine = z.Engine(
            "node1", 30, 240000, "compute", cluster_id="41383005_2",
            managed=True,
        )
        engine.lastseen = time.time() - z.TIMEOUT - 100
        engine.jobs = {"running-job"}
        engine.res_cpu_reserved = 8.0
        engine.res_mem_reserved_mb = 24000.0
        engine.res_ssd_reserved_gb = 7.0
        engine.pending_commands = [("existing-command", None)]
        z.engines.engine_by_clusterid[engine.cluster_id] = engine
        z.engines.last_observed_cids = {engine.cluster_id}
        before = time.time()

        engine_id = z.engines.register(
            "node1", 30, 240000, "compute", "41383005_2", 100, 10,
            "original-instance", VERSION,
        )

        self.assertIs(z.engines.engine_by_id[engine_id], engine)
        with mock.patch.object(z.engines, "cancel_cid") as cancel:
            self.assertFalse(z.engines.reconcile_timed_out_engine(engine, True))
        cancel.assert_not_called()
        self.assertGreaterEqual(engine.lastseen, before)
        self.assertEqual(engine.jobs, {"running-job"})
        self.assertEqual(engine.res_cpu_reserved, 8.0)
        self.assertEqual(engine.res_mem_reserved_mb, 24000.0)
        self.assertEqual(engine.res_ssd_reserved_gb, 7.0)
        self.assertEqual(engine.pending_commands, [("existing-command", None)])

    def test_registration_does_not_remove_an_existing_stop_fence(self):
        z = self.manager
        engine = z.Engine(
            "node1", 30, 240000, "compute", cluster_id="123", managed=True,
        )
        engine.stopping = True
        z.engines.engine_by_clusterid["123"] = engine
        engine_id = self.register(VERSION)
        self.assertIs(z.engines.engine_by_id[engine_id], engine)
        self.assertTrue(engine.stopping)

    def test_new_chief_uses_versioned_registration(self):
        namespace = load_chief_registration()
        calls = []
        proxy = types.SimpleNamespace(
            register_versioned=lambda *args: calls.append(("new", args)) or "node1",
            register=lambda *args: calls.append(("old", args)) or "node1",
        )
        namespace["manager_supports_method"] = lambda _proxy, _method: True

        self.assertEqual(namespace["register_with_manager"](proxy, "node1", 2), "node1")
        self.assertEqual(calls, [("new", ("node1", 2, VERSION))])

    def test_new_chief_falls_back_to_old_manager_and_warns_once(self):
        namespace = load_chief_registration()
        calls = []
        proxy = types.SimpleNamespace(register=lambda *args: calls.append(args) or "node1")
        namespace["manager_supports_method"] = lambda _proxy, _method: False

        self.assertEqual(namespace["register_with_manager"](proxy, "node1", 2), "node1")
        self.assertEqual(namespace["register_with_manager"](proxy, "node1", 2), "node1")
        self.assertEqual(calls, [("node1", 2), ("node1", 2)])
        self.assertEqual(namespace["sys"].stderr.getvalue().count("WARNING"), 1)


if __name__ == "__main__":
    unittest.main()
