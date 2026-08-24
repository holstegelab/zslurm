import importlib.machinery
import importlib.util
import io
import pathlib
import threading
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
ZSLURM_PATH = ROOT / "zslurm"


def load_zslurm():
    loader = importlib.machinery.SourceFileLoader(
        "zslurm_autogrow_under_test", str(ZSLURM_PATH)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class QueuedScratchCapacityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zslurm = load_zslurm()

    def setUp(self):
        z = self.zslurm
        z.gb.lock = threading.RLock()
        z.gb.log_file = io.StringIO()
        z.status.config = {}
        z.status.node_profiles = {
            "genoa": {"cores": 192, "mem_gb": 336},
        }
        z.status.autogrow_prefer_partitions = [("genoa", True)]
        z.status.autogrow_fallback_partition = "genoa"
        z.status.autogrow_fat_partitions = []
        z.status.default_partition = "genoa"
        z.engines = z.EngineManager()
        self.jobs = z.JobManager()
        self.jobs.active_total = 100000
        self.jobs.dcache_total = 100000
        self.jobs.archive_total = 100000
        z.jobs = self.jobs
        self.add_ssd_job()

    def add_ssd_job(self):
        z = self.zslurm
        job = z.Job(
            "ssd-job", "1", "true", "/tmp", {},
            192, 336 * 1024, 3600, 0, None,
            0, 0, 0, 0, 0, 0, "compute", 0, None, "",
            ssd_use="required", ssd_gb=100,
        )
        self.jobs.jobs_by_id[job.jobid] = job
        return job

    def plan(self):
        availability = {
            "genoa": {
                "scratch": {"states": {"IDLE": 10}},
                "no_scratch": {"states": {"IDLE": 0}},
            }
        }
        with mock.patch.object(
            self.zslurm.zslurm_shared,
            "slurm_partition_state_counts_by_scratch",
            return_value=availability,
        ):
            return self.zslurm.compute_autogrow_plan([], "compute")

    def test_queued_scratch_engine_satisfies_ssd_residual(self):
        self.zslurm.engines.cluster_queued_by_partition = {"genoa": 1}
        self.zslurm.engines.queued_engines_detail = [
            ("queued-scratch", "genoa", 120.0, True),
        ]

        plan = self.plan()

        self.assertTrue(plan["has_eligible"])
        self.assertTrue(plan["plan_need_ssd"])
        self.assertEqual(plan["raw_best_nodes"], 0)
        self.assertEqual(plan["best_nodes"], 0)

    def test_queued_plain_engine_does_not_satisfy_ssd_residual(self):
        self.zslurm.engines.cluster_queued_by_partition = {"genoa": 1}
        self.zslurm.engines.queued_engines_detail = [
            ("queued-plain", "genoa", 120.0, False),
        ]

        plan = self.plan()

        self.assertTrue(plan["has_eligible"])
        self.assertTrue(plan["plan_need_ssd"])
        self.assertEqual(plan["raw_best_nodes"], 1)
        self.assertEqual(plan["best_nodes"], 1)


if __name__ == "__main__":
    unittest.main()
