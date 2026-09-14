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
        "zslurm_partial_autogrow_under_test", str(ZSLURM_PATH)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class PartialPilotAutogrowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zslurm = load_zslurm()

    def setUp(self):
        z = self.zslurm
        z.gb.lock = threading.RLock()
        z.gb.log_file = io.StringIO()
        z._apply_cluster_config(
            {
                "cluster_site": "spider",
                "autogrow_enable": True,
                "autogrow_max_compute_nodes": 33,
            }
        )
        z.engines = z.EngineManager()
        self.jobs = z.JobManager()
        self.jobs.active_total = 100000
        self.jobs.dcache_total = 100000
        self.jobs.archive_total = 100000
        z.jobs = self.jobs

    def add_job(self, job_id, cores, mem_mb):
        z = self.zslurm
        job = z.Job(
            "job-" + str(job_id), str(job_id), "true", "/tmp", {},
            cores, mem_mb, 3600, 0, None,
            0, 0, 0, 0, 0, 0, "compute", 0, None, "",
        )
        self.jobs.jobs_by_id[job.jobid] = job
        return job

    def plan(self):
        availability = {
            "normal": {
                "scratch": {"states": {"IDLE": 0}},
                "no_scratch": {"states": {"IDLE": 10}},
            }
        }
        with mock.patch.object(
            self.zslurm.zslurm_shared,
            "slurm_partition_state_counts_by_scratch",
            return_value=availability,
        ):
            return self.zslurm.compute_autogrow_plan([], "compute")

    def test_single_eight_gb_job_requests_two_core_pilot(self):
        self.add_job("one", 1, 8000)

        plan = self.plan()

        self.assertEqual(plan["best_nodes"], 1)
        self.assertEqual(plan["engine_cores"], 2)
        self.assertAlmostEqual(
            self.zslurm._partial_engine_schedulable_memory_mb(2), 14628.0
        )

    def test_memory_and_cpu_both_raise_partial_pilot_size(self):
        self.add_job("memory", 1, 16000)
        memory_plan = self.plan()
        self.assertEqual(memory_plan["engine_cores"], 3)

        self.jobs.jobs_by_id.clear()
        self.add_job("cpu", 6, 8000)
        cpu_plan = self.plan()
        self.assertEqual(cpu_plan["engine_cores"], 6)

    def test_multi_pilot_backlog_uses_configured_maximum_per_pilot(self):
        for index in range(40):
            self.add_job(index, 1, 1000)

        plan = self.plan()

        self.assertEqual(plan["raw_best_nodes"], 2)
        self.assertEqual(plan["best_nodes"], 1)
        self.assertEqual(plan["engine_cores"], 30)

    def test_queued_partial_pilot_is_not_counted_as_full_profile(self):
        for index in range(3):
            self.add_job(index, 1, 8000)
        z = self.zslurm
        z.engines.cluster_queued_by_partition = {"normal": 1}
        z.engines.queued_engines_detail = [
            ("queued-two-core", "normal", 60.0, False)
        ]
        z.engines.queued_engine_capacity_by_cid = {
            "queued-two-core": (2.0, 14628.0)
        }

        plan = self.plan()

        self.assertEqual(plan["best_nodes"], 1)
        self.assertEqual(plan["engine_cores"], 2)

    def test_squeue_numcpus_restores_partial_capacity_after_restart(self):
        output = (
            "411|411||test-instance|1-00:00:00|PD||normal|00:01:00||2|Priority\n"
        )
        process = mock.Mock()
        process.returncode = 0
        process.communicate.return_value = (output.encode("utf-8"), b"")
        manager = self.zslurm.EngineManager()

        with mock.patch.object(self.zslurm, "Popen", return_value=process):
            manager._check_cluster_engines()

        self.assertEqual(manager.cluster_queued_by_partition, {"normal": 1})
        self.assertEqual(
            manager.queued_engine_capacity_by_cid["411"], (2.0, 14628.0)
        )

    def test_start_slurm_uses_partial_core_request_and_placeholder_capacity(self):
        z = self.zslurm
        z.status.instance_name = "test-instance"
        z.status.address = "127.0.0.1"
        z.status.port = 12345
        process = mock.Mock()
        process.communicate.return_value = (b"412\n", b"")
        manager = z.EngineManager()

        with mock.patch.object(z, "Popen", return_value=process) as popen, \
                mock.patch.object(z.time, "sleep"):
            started = manager.start_slurm(
                z.gb, 1, "1-00:00:00", "normal", None, 2
            )

        command = popen.call_args.args[0]
        self.assertIn("--cpus-per-task=2", command)
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].cores, 2.0)
        self.assertEqual(started[0].totmem, 14628.0)

    def test_feature_is_opt_in_outside_spider(self):
        z = self.zslurm
        z._apply_cluster_config({"cluster_site": "snellius"})
        self.assertFalse(z.status.autogrow_dynamic_engine_cores)
        self.assertEqual(
            z._autogrow_requested_engine_cores([], "genoa", 1, 8000, 1),
            0,
        )


if __name__ == "__main__":
    unittest.main()
