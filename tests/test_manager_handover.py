import importlib.machinery
import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
import xmlrpc.client
from unittest import mock

import yaml
import zslurm_shared


ROOT = pathlib.Path(__file__).resolve().parents[1]
ZSLURM_PATH = ROOT / "zslurm"


def load_zslurm():
    loader = importlib.machinery.SourceFileLoader(
        "zslurm_handover_under_test", str(ZSLURM_PATH)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class HandoverSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zslurm = load_zslurm()

    def setUp(self):
        z = self.zslurm
        z.gb.lock = threading.RLock()
        z.gb.log_file = None
        z.jobs = z.JobManager()
        z.engines = z.EngineManager()
        z.status.instance_name = "source"
        z.status.manager_uuid = "source-uuid"
        z.status.handover_mode = "ACTIVE"
        z.status.handover_target = None
        z.status.handover_owned_aliases = set()
        z.status.lastin_first = True
        z.status.prio_fillmem_context = 500
        z.status.autogrow_enable = True
        z.status.autoconsolidate_enable = True
        z.status.config = {"autogrow_max_compute_nodes": 40}

    def add_job(self, jobid, state, active_add):
        z = self.zslurm
        job = z.Job(
            "job-" + jobid,
            jobid,
            "true",
            "/tmp",
            {},
            2,
            4000,
            3600,
            0,
            None,
            0,
            0,
            0,
            0,
            active_add,
            0,
            "compute",
            0,
            None,
            "",
        )
        job.state = state
        job.node_id = "node1" if state in ("RUNNING", "ASSIGNED") else None
        if state == "RUNNING":
            job.held_ncpu = 1.25
            job.held_mem = 2500.0
            job.lease_epoch = 3
        z.jobs.jobs_by_id[jobid] = job
        return job

    def test_snapshot_requeues_assigned_and_preserves_running_lease(self):
        z = self.zslurm
        running = self.add_job("1", "RUNNING", 20)
        assigned = self.add_job("2", "ASSIGNED", 10)
        z.jobs.active_total = 1000
        z.jobs.active_inuse = 30
        z.jobs.total_jobs = {"compute": 2}
        z.jobs.running_jobs = {"compute": 1}

        engine = z.Engine(
            "node1", 192, 344064, "compute",
            cluster_id="12345", managed=True,
        )
        engine.jobs = {running.jobid}
        engine.res_cpu_reserved = running.held_ncpu
        engine.res_mem_reserved_mb = running.held_mem
        z.engines.engine_by_id[engine.engine_id] = engine
        z.engines.engine_by_clusterid[engine.cluster_id] = engine

        descriptor = {
            "instance": "target",
            "manager_uuid": "target-uuid",
        }
        source_descriptor = {
            "instance": "source",
            "manager_uuid": "source-uuid",
        }
        with mock.patch.object(
            z, "_manager_descriptor", return_value=source_descriptor
        ):
            snapshot = z._build_handover_snapshot("handover-1", descriptor)

        self.assertEqual(snapshot["manager"]["active_inuse"], 20.0)
        by_id = {item["jobid"]: item for item in snapshot["jobs"]}
        self.assertEqual(by_id[assigned.jobid]["state"], "PENDING")
        self.assertIsNone(by_id[assigned.jobid]["node_id"])
        self.assertEqual(by_id[running.jobid]["state"], "RUNNING")
        self.assertEqual(by_id[running.jobid]["held_ncpu"], 1.25)
        self.assertEqual(by_id[running.jobid]["lease_epoch"], 3)

        z.jobs = z.JobManager()
        z.engines = z.EngineManager()
        z.status.instance_name = "target"
        z._apply_handover_snapshot(snapshot)

        self.assertEqual(list(z.jobs.jobs_by_id), ["1", "2"])
        self.assertEqual(z.jobs.jobs_by_id["2"].state, "PENDING")
        self.assertEqual(z.jobs.jobs_by_id["1"].held_mem, 2500.0)
        self.assertEqual(z.jobs.active_inuse, 20.0)
        self.assertFalse(z.engines.engine_by_id)
        self.assertIn("12345", z.engines.engine_by_clusterid)
        self.assertEqual(
            z.engines.engine_by_clusterid["12345"].instance, "source"
        )
        self.assertEqual(z.status.handover_expected_cluster_ids, {"12345"})

    def test_committed_source_poll_only_retries_migration(self):
        z = self.zslurm
        z.status.handover_mode = "SOURCE_COMMITTED"
        z.status.handover_target = {
            "instance": "target",
            "manager_url": "http://target:39000/rpc",
            "job_url": "http://target:39001/rpc",
            "handover_secret": "must-not-reach-chief",
        }

        with mock.patch.object(z.engines, "poll") as local_poll:
            commands = z.poll("node1")

        local_poll.assert_not_called()
        self.assertEqual(commands[0][0], z.zslurm_shared.MIGRATE_MANAGER)
        self.assertEqual(commands[0][1]["instance"], "target")
        self.assertNotIn("handover_secret", commands[0][1])

    def test_receive_rejects_nonempty_target(self):
        z = self.zslurm
        self.add_job("1", "PENDING", 0)
        snapshot = {
            "schema_version": z.HANDOVER_SCHEMA_VERSION,
            "target_manager_uuid": z.status.manager_uuid,
        }

        result = z.receive_handover(
            z.status.handover_secret, "handover-2", snapshot
        )

        self.assertFalse(result["ok"])
        self.assertIn("not empty", result["error"])

    def test_activation_restores_scheduling_before_all_chiefs_arrive(self):
        z = self.zslurm
        z.status.handover_mode = "TARGET_IMPORTED"
        z.status.handover_saved_autogrow = True
        z.status.handover_saved_autoconsolidate = True
        z.status.handover_expected_cluster_ids = {"late-chief"}
        z.status.handover_registered_cluster_ids = set()

        result = z.activate_received_handover(
            z.status.handover_secret, "handover-3"
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["remaining_chiefs"], 1)
        self.assertEqual(z.status.handover_mode, "TARGET_MIGRATING")
        self.assertTrue(z.status.autogrow_enable)
        self.assertTrue(z.status.autoconsolidate_enable)

    def test_late_chief_keeps_original_slurm_instance_name(self):
        z = self.zslurm
        engine_id = z.engines.register(
            "node1", 192, 344064, "compute", "12345", 0, 0,
            "old-instance",
        )

        self.assertEqual(
            z.engines.engine_by_id[engine_id].instance, "old-instance"
        )


class HeadlessHandoverIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="zslurm-handover-test.")
        self.workdir = pathlib.Path(self.temp.name)
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.workdir)
        self.env["PYTHONPATH"] = str(ROOT) + os.pathsep + self.env.get(
            "PYTHONPATH", ""
        )
        # Never let an integration test discover or mutate real Slurm jobs.
        fake_bin = self.workdir / "fake-bin"
        fake_bin.mkdir()
        for command in ("squeue", "scancel", "scontrol", "sbatch"):
            script = fake_bin / command
            script.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            script.chmod(0o755)
        self.env["PATH"] = str(fake_bin) + os.pathsep + self.env.get("PATH", "")
        self.instance_prefix = "zslurm_handover_test_" + uuid.uuid4().hex
        self.processes = []

    def tearDown(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        self.temp.cleanup()

    @property
    def instance_dir(self):
        return self.workdir / ".zslurm" / "instances"

    def configs(self):
        result = {}
        if not self.instance_dir.exists():
            return result
        for path in self.instance_dir.glob("*.yaml"):
            with path.open() as handle:
                config = yaml.safe_load(handle) or {}
            result[str(config.get("name"))] = config
        return result

    @staticmethod
    def url(config, worker=False):
        host = config.get("advertise_host") or config.get("bind_host")
        port = int(config["base_port"]) + (0 if worker else 1)
        return f"http://{host}:{port}/{config['rpcpath']}"

    def wait_for_instances(self, count, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            configs = self.configs()
            if len(configs) >= count:
                all_ready = True
                for config in configs.values():
                    try:
                        proxy = xmlrpc.client.ServerProxy(
                            self.url(config), allow_none=True
                        )
                        if not proxy.health().get("ok"):
                            all_ready = False
                    except Exception:
                        all_ready = False
                if all_ready:
                    return configs
            time.sleep(0.2)
        errors = []
        for process in self.processes:
            if process.poll() is not None:
                try:
                    errors.append(process.stderr.read())
                except Exception:
                    pass
            else:
                errors.append(f"pid {process.pid} still running")
        self.fail(f"managers did not become ready: {errors}")

    def start_manager(self, instance_name):
        process = subprocess.Popen(
            [
                sys.executable,
                str(ZSLURM_PATH),
                "--headless",
                "--no-autogrow",
                "--no-autoconsolidate",
                "--enable-control",
                "--control-token",
                "test-control-token",
                "--instance-name",
                instance_name,
            ],
            cwd=str(self.workdir),
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.processes.append(process)
        return process

    def test_new_manager_explicitly_takes_over_old_instance(self):
        self.start_manager(self.instance_prefix + "_source")
        first = self.wait_for_instances(1)
        source_name, source_config = next(iter(first.items()))
        source_raw_config = dict(source_config)

        source = xmlrpc.client.ServerProxy(
            self.url(source_config), allow_none=True
        )
        source_worker = xmlrpc.client.ServerProxy(
            self.url(source_config, worker=True), allow_none=True
        )
        engine_id = source_worker.register(
            "fake-node", 4, 8000, "compute", "fake-cluster-id", 0, 0,
            source_name,
        )
        jobid = source.submit_job(
            "handover-job", "true", str(self.workdir), {},
            1, 1000, 3600, 0, None,
            0, 0, 0, 0, 0, 0, "compute", 0, None, "", "no", 0,
        )

        self.start_manager(self.instance_prefix + "_target")
        both = self.wait_for_instances(2)
        target_name = next(name for name in both if name != source_name)
        target_config = both[target_name]
        target = xmlrpc.client.ServerProxy(
            self.url(target_config), allow_none=True
        )

        result = target.handover_from(
            "test-control-token", source_name
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source_instance"], source_name)
        self.assertEqual(result["target_instance"], target_name)
        self.assertEqual(result["chiefs"], 1)
        self.assertEqual(target.list_jobs()[0][0], jobid)
        self.assertEqual(target.health()["handover_mode"], "TARGET_MIGRATING")

        commands = source_worker.poll(
            engine_id, 0, 0, 0, 1, 0, {}, {}, 0, 0, 0, 0,
        )
        self.assertEqual(commands[0][0], zslurm_shared.MIGRATE_MANAGER)
        migration = commands[0][1]
        self.assertNotIn("handover_secret", migration)
        migrated_worker = xmlrpc.client.ServerProxy(
            migration["manager_url"], allow_none=True
        )
        migrated_id = migrated_worker.register(
            "fake-node", 4, 8000, "compute", "fake-cluster-id", 0, 0,
            source_name,
        )
        self.assertEqual(migrated_id, engine_id)
        self.assertEqual(target.health()["handover_mode"], "TARGET_ACTIVE")

        aliases = self.configs()
        source_alias = aliases[source_name]
        self.assertEqual(source_alias["handover_state"], "ALIAS")
        self.assertEqual(source_alias["alias_of"], target_name)
        self.assertEqual(
            source_alias["manager_uuid"], target_config["manager_uuid"]
        )
        alias_proxy = xmlrpc.client.ServerProxy(
            self.url(source_alias), allow_none=True
        )
        self.assertEqual(alias_proxy.list_jobs()[0][0], jobid)

        old_raw = xmlrpc.client.ServerProxy(
            self.url(source_raw_config), allow_none=True
        )
        self.assertEqual(
            old_raw.health()["handover_mode"], "SOURCE_COMMITTED"
        )

        # Graceful source shutdown must leave the alias owned by the target;
        # graceful target shutdown removes both its primary and inherited name.
        self.processes[0].terminate()
        self.processes[0].wait(timeout=10)
        self.assertIn(source_name, self.configs())
        self.processes[1].terminate()
        self.processes[1].wait(timeout=10)
        deadline = time.time() + 5
        while time.time() < deadline and self.configs():
            time.sleep(0.1)
        self.assertEqual(self.configs(), {})


if __name__ == "__main__":
    unittest.main()
