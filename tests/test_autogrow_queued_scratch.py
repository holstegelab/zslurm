import importlib.machinery
import importlib.util
import io
import pathlib
import threading
from types import SimpleNamespace
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
        z.engines.cluster_queued_by_partition = {}
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

    def test_busy_cluster_still_queues_demand_backed_pilots(self):
        z = self.zslurm
        with mock.patch.object(z.zslurm_shared, 'slurm_partition_state_counts_by_scratch', return_value={}):
            plan = z.compute_autogrow_plan([], 'compute')
            self.assertEqual(plan['best_nodes'], 1)
            z.status.config['autogrow_require_idle_nodes'] = True
            plan = z.compute_autogrow_plan([], 'compute')
            self.assertEqual(plan['plan_nodes'], 0)
            self.assertIn('no idle nodes', plan['plan_reason'])

    def test_subnode_backlog_grows_once_and_not_when_already_covered(self):
        z = self.zslurm
        z.status.node_profiles = {'normal': {'cores': 30, 'mem_gb': 240}}
        z.status.autogrow_prefer_partitions = [('normal', False)]
        z.status.autogrow_fallback_partition = 'normal'
        self.jobs.jobs_by_id.clear()
        job = z.Job('audit', 'small', 'true', '/tmp', {}, 4, 32000, 3600,
                    0, None, 0, 0, 0, 0, 0, 0, 'compute', 0, None, '')
        self.jobs.jobs_by_id[job.jobid] = job
        fleet = [SimpleNamespace(partition='compute', cores=30, totmem=220708,
                 res_cpu_reserved=27, res_mem_reserved_mb=208896, has_ssd=False)]
        with mock.patch.object(z.zslurm_shared, 'slurm_partition_state_counts_by_scratch', return_value={}):
            plan = z.compute_autogrow_plan(fleet, 'compute')
            self.assertEqual(plan['plan_nodes'], 1)
            z.engines.cluster_queued_by_partition = {'normal': 1}
            self.assertEqual(z.compute_autogrow_plan(fleet, 'compute')['plan_nodes'], 0)
            z.engines.cluster_queued_by_partition = {}
            fleet[0].res_cpu_reserved = 0
            fleet[0].res_mem_reserved_mb = 0
            self.assertEqual(z.compute_autogrow_plan(fleet, 'compute')['plan_nodes'], 0)
            self.jobs.jobs_by_id.clear()
            self.assertEqual(z.compute_autogrow_plan(fleet, 'compute')['plan_nodes'], 0)

    def test_spider_plain_only_fleet_has_no_implicit_scratch_fraction_cap(self):
        z = self.zslurm
        z.status.node_profiles = {'normal': {'cores': 30, 'mem_gb': 240}}
        z.status.autogrow_prefer_partitions = [('normal', False)]
        z.status.autogrow_fallback_partition = 'normal'
        self.jobs.jobs_by_id.clear()
        for i in range(20):
            job = z.Job('pav', str(i), 'true', '/tmp', {}, 9, 69632, 176400,
                        0, None, 0, 0, 0, 0, 0, 0, 'compute', 0, None, '')
            self.jobs.jobs_by_id[job.jobid] = job
        fleet = [SimpleNamespace(partition='compute', cores=30, totmem=220708,
                 res_cpu_reserved=0, res_mem_reserved_mb=0, has_ssd=False) for _ in range(3)]
        with mock.patch.object(z.zslurm_shared, 'slurm_partition_state_counts_by_scratch', return_value={}):
            plan = z.compute_autogrow_plan(fleet, 'compute')
            self.assertGreater(plan['best_nodes'], 0)
            self.assertEqual(plan['best_part'], 'normal')
            z.engines.cluster_queued_by_partition = {'normal': 7}
            plan = z.compute_autogrow_plan(fleet, 'compute')
            self.assertEqual(plan.get('best_nodes', 0), 0)


if __name__ == "__main__":
    unittest.main()
