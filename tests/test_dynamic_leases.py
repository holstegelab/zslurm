import importlib.machinery
import importlib.util
import io
import os
import pathlib
import tempfile
import threading
import time
import unittest

import zslurm_lease


ROOT = pathlib.Path(__file__).resolve().parents[1]
ZSLURM_PATH = ROOT / "zslurm"


def load_zslurm():
    loader = importlib.machinery.SourceFileLoader(
        "zslurm_lease_manager_under_test", str(ZSLURM_PATH)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeChiefStatus:
    def __init__(self, cpu=32.0, mem_mb=128000.0):
        self.lock = threading.RLock()
        self.current_cpu = cpu
        self.current_mem = mem_mb
        self.current_mem_usage = {}


class ChiefLeaseControllerTests(unittest.TestCase):
    def make_controller(
        self,
        cpu=32.0,
        mem_mb=128000.0,
        min_cpu=0.1,
        headroom_fraction=0.0,
        headroom_mb=0.0,
    ):
        status = FakeChiefStatus(cpu, mem_mb)
        manager_calls = []

        def manager_resize(job_id, cores, target_mem_mb):
            manager_calls.append((job_id, cores, target_mem_mb))
            return {"ok": True, "epoch": len(manager_calls)}

        controller = zslurm_lease.ChiefLeaseController(
            status=status,
            total_cpu=cpu,
            total_mem_mb=mem_mb,
            manager_resize=manager_resize,
            memory_available=lambda: mem_mb,
            min_cpu=min_cpu,
            memory_headroom_fraction=headroom_fraction,
            memory_headroom_mb=headroom_mb,
        )
        return status, controller, manager_calls

    def start_job(self, status, controller, job_id="job-1", cpu=24, mem_mb=64000):
        env = controller.prepare_job(job_id, cpu, mem_mb)
        status.current_cpu -= cpu
        status.current_mem -= mem_mb
        return env

    def test_release_and_completion_return_only_current_holding(self):
        status, controller, calls = self.make_controller()
        env = self.start_job(status, controller)

        response = controller.set_target(
            "job-1", env[zslurm_lease.ENV_TOKEN], cores=2, mem_mb=12000
        )

        self.assertTrue(response["ok"])
        self.assertEqual(response["held_cores"], 2)
        self.assertEqual(response["held_mem_mb"], 12000)
        self.assertEqual(status.current_cpu, 30)
        self.assertEqual(status.current_mem, 116000)
        self.assertEqual(calls, [("job-1", 2, 12000)])

        released = controller.finish_job("job-1", 24, 64000)
        self.assertTrue(released["dynamic"])
        self.assertEqual(released["held_cpu"], 2)
        self.assertEqual(released["held_mem_mb"], 12000)
        self.assertEqual(status.current_cpu, 32)
        self.assertEqual(status.current_mem, 128000)

    def test_absolute_target_retry_does_not_double_release(self):
        status, controller, calls = self.make_controller()
        env = self.start_job(status, controller)
        token = env[zslurm_lease.ENV_TOKEN]

        first = controller.set_target(
            "job-1", token, cores=2, mem_mb=12000
        )
        second = controller.set_target(
            "job-1", token, cores=2, mem_mb=12000
        )

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(status.current_cpu, 30)
        self.assertEqual(status.current_mem, 116000)
        self.assertEqual(len(calls), 2)

    def test_growth_waits_fifo_and_raises_admission_barrier(self):
        status, controller, _ = self.make_controller(cpu=16, mem_mb=64000)
        env = self.start_job(
            status, controller, cpu=12, mem_mb=32000
        )
        token = env[zslurm_lease.ENV_TOKEN]
        controller.set_target(
            "job-1", token, cores=2, mem_mb=8000
        )
        # Simulate unrelated child jobs consuming released capacity.
        status.current_cpu = 3
        status.current_mem = 9000
        result = {}

        def acquire():
            result.update(
                controller.set_target(
                    "job-1",
                    token,
                    cores=12,
                    mem_mb=32000,
                    timeout_s=2,
                )
            )

        thread = threading.Thread(target=acquire)
        thread.start()
        deadline = time.time() + 1
        while not controller.has_pending_acquire() and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(controller.has_pending_acquire())
        self.assertTrue(thread.is_alive())

        with status.lock:
            status.current_cpu += 7
            status.current_mem += 15000
        controller.notify_capacity_change()
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertTrue(result["ok"])
        self.assertEqual(result["held_cores"], 12)
        self.assertEqual(result["held_mem_mb"], 32000)
        self.assertFalse(controller.has_pending_acquire())
        self.assertEqual(status.current_cpu, 0)
        self.assertEqual(status.current_mem, 0)

    def test_growth_timeout_leaves_current_lease_unchanged(self):
        status, controller, _ = self.make_controller(cpu=16, mem_mb=64000)
        env = self.start_job(
            status, controller, cpu=12, mem_mb=32000
        )
        token = env[zslurm_lease.ENV_TOKEN]
        controller.set_target(
            "job-1", token, cores=2, mem_mb=8000
        )
        status.current_cpu = 0
        status.current_mem = 0

        response = controller.set_target(
            "job-1",
            token,
            cores=12,
            mem_mb=32000,
            timeout_s=0.05,
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], "timeout")
        current = controller.status_for("job-1", token)
        self.assertEqual(current["held_cores"], 2)
        self.assertEqual(current["held_mem_mb"], 8000)
        self.assertFalse(controller.has_pending_acquire())

    def test_observed_memory_floor_prevents_unsafe_release(self):
        status, controller, calls = self.make_controller(
            headroom_fraction=0.25, headroom_mb=512
        )
        env = self.start_job(status, controller)
        status.current_mem_usage["job-1"] = 8000

        response = controller.set_target(
            "job-1",
            env[zslurm_lease.ENV_TOKEN],
            cores=2,
            mem_mb=1000,
        )

        self.assertTrue(response["ok"])
        self.assertTrue(response["safety"]["adjusted"])
        self.assertEqual(response["held_mem_mb"], 10512)
        self.assertEqual(calls[-1][2], 10512)

    def test_invalid_token_is_rejected(self):
        status, controller, calls = self.make_controller()
        self.start_job(status, controller)

        response = controller.set_target(
            "job-1", "not-the-token", cores=2, mem_mb=12000
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], "denied")
        self.assertEqual(calls, [])

    def test_unix_socket_round_trip(self):
        status, controller, _ = self.make_controller()
        env = self.start_job(status, controller)
        with tempfile.TemporaryDirectory() as tempdir:
            socket_path = os.path.join(tempdir, "chief.sock")
            server = zslurm_lease.LeaseServer(
                socket_path, controller
            ).start()
            try:
                response = zslurm_lease.send_request(
                    socket_path,
                    {
                        "version": 1,
                        "action": "status",
                        "job_id": "job-1",
                        "token": env[zslurm_lease.ENV_TOKEN],
                    },
                )
            finally:
                server.close()

        self.assertTrue(response["ok"])
        self.assertEqual(response["held_cores"], 24)


class ManagerLeaseAccountingTests(unittest.TestCase):
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
        z.jobs = self.jobs

    def add_job(self, cpu=8, mem_mb=32000):
        z = self.zslurm
        job = z.Job(
            "lease-job",
            "1",
            "true",
            "/tmp",
            {},
            cpu,
            mem_mb,
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
        )
        self.jobs.jobs_by_id[job.jobid] = job
        return job

    def test_manager_resize_updates_engine_and_completion_uses_held(self):
        job = self.add_job()
        assigned = self.jobs.request_jobs(
            "test-node", 32, 128000, "compute"
        )
        self.assertEqual(len(assigned), 1)
        self.assertEqual(job.state, "RUNNING")
        self.assertEqual(self.engine.res_cpu_reserved, 8)
        self.assertEqual(self.engine.res_mem_reserved_mb, 32000)

        response = self.jobs.resize_running_job(
            "test-node", job.jobid, 2, 8000
        )
        self.assertTrue(response["ok"])
        self.assertEqual(self.engine.res_cpu_reserved, 2)
        self.assertEqual(self.engine.res_mem_reserved_mb, 8000)

        detailed = self.jobs.list_jobs_detailed()
        self.assertEqual(detailed[0]["cores"], 2)
        self.assertEqual(detailed[0]["max_cores"], 8)
        self.assertEqual(detailed[0]["mem_reserved_mb"], 8000)
        self.assertEqual(detailed[0]["max_mem_mb"], 32000)

        self.jobs.job_done(job.jobid, self.zslurm.RC_SUCCESS)
        self.assertEqual(self.engine.res_cpu_reserved, 0)
        self.assertEqual(self.engine.res_mem_reserved_mb, 0)

    def test_manager_target_is_idempotent_and_bounded(self):
        job = self.add_job()
        self.jobs.request_jobs("test-node", 32, 128000, "compute")

        first = self.jobs.resize_running_job(
            "test-node", job.jobid, 2, 8000
        )
        second = self.jobs.resize_running_job(
            "test-node", job.jobid, 2, 8000
        )
        too_large = self.jobs.resize_running_job(
            "test-node", job.jobid, 9, 8000
        )

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertFalse(too_large["ok"])
        self.assertEqual(self.engine.res_cpu_reserved, 2)
        self.assertEqual(self.engine.res_mem_reserved_mb, 8000)


class SchedulerPriorityTests(unittest.TestCase):
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
            engine_id="priority-node",
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
        z.jobs = self.jobs

    def add_job(
        self, jobid, name, priority=0, cpu=1, mem_mb=1000,
        partition="compute", ssd_use="no", dcache_transfer_slots=0,
    ):
        z = self.zslurm
        job = z.Job(
            name, jobid, "true", "/tmp", {}, cpu, mem_mb, 3600, 0, None,
            0, 0, 0, 0, 0, 0, partition, 0, None, "",
            ssd_use=ssd_use,
            dcache_transfer_slots=dcache_transfer_slots,
            owner="pipeline-" + str(priority),
            priority=priority,
        )
        self.jobs.jobs_by_id[job.jobid] = job
        return job

    def dispatch_one(self, partition="compute", mem_mb=128000):
        return self.jobs.request_jobs(
            self.engine.engine_id, 1, mem_mb, partition
        )

    def test_higher_pipeline_priority_precedes_earlier_job(self):
        low = self.add_job("1", "low", priority=0)
        high = self.add_job("2", "high", priority=100)

        assigned = self.dispatch_one()

        self.assertEqual(assigned[0][0], high.jobid)
        self.assertEqual(high.state, "RUNNING")
        self.assertEqual(low.state, "PENDING")

    def test_memory_packer_cannot_cross_priority_band(self):
        high = self.add_job("1", "high-awkward-fit", priority=50, mem_mb=100)
        low = self.add_job("2", "low-perfect-fit", priority=0, mem_mb=4000)

        assigned = self.dispatch_one(mem_mb=4000)

        self.assertEqual(assigned[0][0], high.jobid)
        self.assertEqual(low.state, "PENDING")

    def test_equal_priority_keeps_fifo_and_compute_lifo(self):
        first = self.add_job("1", "first", priority=7)
        second = self.add_job("2", "second", priority=7)
        self.assertEqual(self.dispatch_one()[0][0], first.jobid)

        self.setUp()
        z = self.zslurm
        z.status.lastin_first = True
        self.add_job("1", "first", priority=7)
        second = self.add_job("2", "second", priority=7)
        self.assertEqual(self.dispatch_one()[0][0], second.jobid)

    def test_ineligible_high_priority_job_does_not_block_lower_work(self):
        self.add_job("1", "ssd-only", priority=100, ssd_use="required")
        low = self.add_job("2", "regular", priority=0)

        assigned = self.dispatch_one()

        self.assertEqual(assigned[0][0], low.jobid)

    def test_priority_applies_to_archive_transfer_queue(self):
        self.engine.partition = "archive"
        self.jobs.dcache_transfer_total = 1
        low = self.add_job(
            "1", "low-download", priority=0, partition="archive",
            dcache_transfer_slots=1,
        )
        high = self.add_job(
            "2", "high-download", priority=25, partition="archive",
            dcache_transfer_slots=1,
        )

        assigned = self.dispatch_one(partition="archive")

        self.assertEqual(assigned[0][0], high.jobid)
        self.assertEqual(low.state, "PENDING")
        self.assertEqual(self.jobs.dcache_transfer_inuse, 1)

    def test_submit_api_defaults_and_exposes_priority(self):
        jobid = self.jobs.submit_job(
            job_name="submitted", cmd="true", cwd="/tmp", env={},
            ncpu=1, mem=1000, reqtime=60, requeue=0, dependency=None,
            arch_use_add=0, arch_use_remove=0, dcache_use_add=0,
            dcache_use_remove=0, active_use_add=0, active_use_remove=0,
            partition="compute", info_input_mb=0, info_output_file=None,
            owner="pipeline-a", priority=12,
        )

        self.assertEqual(self.jobs.jobs_by_id[jobid].priority, 12)
        self.assertEqual(self.jobs.list_jobs_detailed()[0]["priority"], 12)


if __name__ == "__main__":
    unittest.main()
