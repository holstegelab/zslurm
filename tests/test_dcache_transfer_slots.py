import importlib.machinery
import importlib.util
import io
import pathlib
import threading
import unittest


ZSLURM_PATH = pathlib.Path(__file__).resolve().parents[1] / "zslurm"


def load_zslurm():
    loader = importlib.machinery.SourceFileLoader("zslurm_under_test", str(ZSLURM_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class DcacheTransferSlotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zslurm = load_zslurm()

    def setUp(self):
        z = self.zslurm
        z.gb.lock = threading.RLock()
        z.gb.log_file = io.StringIO()
        z.status.config = {"job_walltime_buffer_sec": 0}
        z.status.lastin_first = False
        z.status.prio_fillmem_context = 20
        z.engines = z.EngineManager()
        z.engines.cluster_queued_by_partition = {}
        self.engine = z.Engine(
            engine_id="test-node",
            cores=32,
            totmem=128000,
            partition="compute",
            cluster_id=False,
            managed=True,
        )
        z.engines.engine_by_id[self.engine.engine_id] = self.engine
        self.jobs = z.JobManager()
        self.jobs.active_total = 100000
        self.jobs.dcache_total = 100000
        self.jobs.archive_total = 100000
        self.jobs.configure_transfer_limits({"dcache_transfer_slots": 4})
        z.jobs = self.jobs

    def add_job(self, name, slots):
        z = self.zslurm
        jobid = str(len(self.jobs.jobs_by_id) + 1)
        job = z.Job(
            name,
            jobid,
            "true",
            "/tmp",
            {},
            1,
            100,
            3600,
            0,
            None,
            0,
            0,
            0,
            0,
            0,
            0,
            "compute",
            0,
            None,
            "",
            dcache_transfer_slots=slots,
        )
        self.jobs.jobs_by_id[jobid] = job
        return job

    def test_fifth_transfer_waits_until_a_slot_is_released(self):
        transfer_jobs = [self.add_job(f"transfer-{i}", 1) for i in range(5)]

        assigned = self.jobs.request_jobs("test-node", 32, 128000, "compute")

        self.assertEqual(len(assigned), 4)
        self.assertEqual(self.jobs.dcache_download_inuse, 4)
        self.assertEqual(self.jobs.dcache_upload_inuse, 4)
        self.assertEqual(transfer_jobs[4].state, "PENDING")

        self.jobs.job_done(transfer_jobs[0].jobid, self.zslurm.RC_SUCCESS)
        assigned = self.jobs.request_jobs("test-node", 28, 127600, "compute")

        self.assertEqual(len(assigned), 1)
        self.assertEqual(assigned[0][0], transfer_jobs[4].jobid)
        self.assertEqual(self.jobs.dcache_download_inuse, 4)
        self.assertEqual(self.jobs.dcache_upload_inuse, 4)

    def test_non_transfer_job_is_not_blocked_by_full_transfer_pool(self):
        for i in range(4):
            self.add_job(f"transfer-{i}", 1)
        normal_job = self.add_job("normal", 0)

        assigned = self.jobs.request_jobs("test-node", 32, 128000, "compute")

        self.assertEqual(len(assigned), 5)
        self.assertEqual(normal_job.state, "RUNNING")
        self.assertEqual(self.jobs.dcache_download_inuse, 4)
        self.assertEqual(self.jobs.dcache_upload_inuse, 4)

    def test_assigned_job_releases_slot_when_engine_disappears(self):
        transfer = self.add_job("transfer", 1)
        assigned = self.jobs.request_jobs("test-node", 0, 0, "compute")

        self.assertEqual(len(assigned), 1)
        self.assertEqual(transfer.state, "ASSIGNED")
        self.assertEqual(self.jobs.dcache_download_inuse, 1)
        self.assertEqual(self.jobs.dcache_upload_inuse, 1)

        self.jobs.engine_removed("test-node")

        self.assertEqual(transfer.state, "PENDING")
        self.assertIsNone(transfer.node_id)
        self.assertEqual(self.jobs.dcache_download_inuse, 0)
        self.assertEqual(self.jobs.dcache_upload_inuse, 0)

    def test_submit_reads_slot_metadata_without_changing_rpc_signature(self):
        jobid = self.jobs.submit_job(
            job_name="submitted-transfer",
            cmd="true",
            cwd="/tmp",
            env={"ZSLURM_DCACHE_TRANSFER_SLOTS": "1"},
            ncpu=1,
            mem=100,
            reqtime=3600,
            requeue=0,
            dependency=None,
            arch_use_add=0,
            arch_use_remove=0,
            dcache_use_add=0,
            dcache_use_remove=0,
            active_use_add=0,
            active_use_remove=0,
            partition="compute",
            info_input_mb=0,
            info_output_file=None,
            owner="test-owner",
        )

        submitted = self.jobs.jobs_by_id[jobid]
        self.assertEqual(submitted.dcache_transfer_slots, 1)
        self.assertEqual(submitted.owner, "test-owner")

    def test_autogrow_counts_only_the_transfer_jobs_that_can_run(self):
        for i in range(20):
            self.add_job(f"transfer-{i}", 1)

        plan = self.zslurm.compute_autogrow_plan([self.engine], "compute")

        self.assertTrue(plan["has_eligible"])
        self.assertEqual(plan["eligible_n"], 4)


if __name__ == "__main__":
    unittest.main()
