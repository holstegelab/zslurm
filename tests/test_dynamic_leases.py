import errno
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

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

    def test_usage_is_reported_per_lease_phase(self):
        status, controller, _ = self.make_controller()
        with mock.patch.object(zslurm_lease.time, "time", return_value=100.0):
            env = self.start_job(status, controller)
        token = env[zslurm_lease.ENV_TOKEN]

        controller.record_usage("job-1", 4.0, 8000.0, sampled_at=105.0)
        with mock.patch.object(zslurm_lease.time, "time", return_value=110.0):
            response = controller.set_target(
                "job-1",
                token,
                cores=2,
                mem_mb=12000,
                phase="tail",
            )
        self.assertEqual(response["phase_name"], "tail")
        controller.record_usage("job-1", 1.0, 2000.0, sampled_at=115.0)
        with mock.patch.object(zslurm_lease.time, "time", return_value=120.0):
            report = controller.finish_job("job-1", 24, 64000)

        self.assertEqual(report["phase_count"], 2)
        initial, tail = report["phases"]
        self.assertEqual(initial["phase_name"], "initial")
        self.assertEqual(initial["event"], "initial")
        self.assertEqual(initial["lease_epoch"], 0)
        self.assertEqual(initial["duration_s"], 10.0)
        self.assertEqual(initial["avg_cpu_cores"], 4.0)
        self.assertEqual(initial["avg_pss_mb"], 8000.0)
        self.assertEqual(initial["reserved_core_seconds"], 240.0)
        self.assertEqual(initial["used_core_seconds"], 40.0)

        self.assertEqual(tail["phase_name"], "tail")
        self.assertEqual(tail["event"], "set")
        self.assertEqual(tail["lease_epoch"], 1)
        self.assertEqual(tail["duration_s"], 10.0)
        self.assertEqual(tail["held_cores"], 2.0)
        self.assertEqual(tail["held_mem_mb"], 12000.0)
        self.assertEqual(tail["avg_cpu_cores"], 1.0)
        self.assertEqual(tail["avg_pss_mb"], 2000.0)
        self.assertAlmostEqual(tail["cpu_efficiency"], 0.5)
        self.assertAlmostEqual(tail["memory_efficiency"], 1.0 / 6.0)
        self.assertEqual(report["reserved_core_seconds"], 260.0)
        self.assertEqual(report["used_core_seconds"], 50.0)

    def test_named_noop_starts_phase_but_plain_noop_does_not(self):
        status, controller, calls = self.make_controller()
        with mock.patch.object(zslurm_lease.time, "time", return_value=100.0):
            env = self.start_job(status, controller)
        token = env[zslurm_lease.ENV_TOKEN]

        with mock.patch.object(zslurm_lease.time, "time", return_value=110.0):
            named = controller.set_target(
                "job-1", token, cores=24, mem_mb=64000, phase="work"
            )
        plain = controller.set_target(
            "job-1", token, cores=24, mem_mb=64000
        )

        self.assertEqual(named["status"], "phase-changed")
        self.assertEqual(named["epoch"], 0)
        self.assertEqual(named["phase_index"], 1)
        self.assertEqual(plain["status"], "unchanged")
        self.assertEqual(plain["phase_index"], 1)
        self.assertEqual(calls, [])

    def test_parallel_releases_create_intervals_in_one_semantic_phase(self):
        status, controller, _ = self.make_controller(cpu=16, mem_mb=64000)
        env = self.start_job(status, controller, cpu=12, mem_mb=12000)
        token = env[zslurm_lease.ENV_TOKEN]
        controller.mark_phase("job-1", token, "qc")
        controller.release_resources(
            "job-1", token, cores=2, mem_mb=1000, release_id="task-a"
        )
        controller.release_resources(
            "job-1", token, cores=2, mem_mb=1000, release_id="task-b"
        )
        report = controller.finish_job("job-1", 12, 12000)

        self.assertEqual(
            [phase["event"] for phase in report["phases"]],
            ["initial", "phase", "release", "release"],
        )
        self.assertEqual(
            [phase["phase_name"] for phase in report["phases"]],
            ["initial", "qc", "qc", "qc"],
        )
        self.assertEqual(
            [phase["transition_id"] for phase in report["phases"][-2:]],
            ["task-a", "task-b"],
        )

    def test_relative_release_without_effective_change_has_no_new_interval(self):
        status, controller, calls = self.make_controller(
            cpu=1, mem_mb=1000, min_cpu=0.1
        )
        env = self.start_job(status, controller, cpu=0.1, mem_mb=100)
        response = controller.release_resources(
            "job-1",
            env[zslurm_lease.ENV_TOKEN],
            cores=1,
            release_id="already-at-floor",
        )
        report = controller.finish_job("job-1", 0.1, 100)

        self.assertEqual(response["status"], "unchanged")
        self.assertEqual(response["released_cores"], 0.0)
        self.assertEqual(calls, [])
        self.assertEqual(report["phase_count"], 1)

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
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(status.current_cpu, 30)
        self.assertEqual(status.current_mem, 116000)
        self.assertEqual(len(calls), 1)

    def test_parallel_relative_releases_are_atomic(self):
        status, controller, calls = self.make_controller(cpu=16, mem_mb=64000)
        env = self.start_job(status, controller, cpu=12, mem_mb=12000)
        token = env[zslurm_lease.ENV_TOKEN]
        barrier = threading.Barrier(6)
        responses = []
        response_lock = threading.Lock()

        def release(index):
            barrier.wait()
            response = controller.release_resources(
                "job-1",
                token,
                cores=2,
                mem_mb=1000,
                release_id=f"consumer-{index}",
            )
            with response_lock:
                responses.append(response)

        threads = [threading.Thread(target=release, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(responses), 6)
        self.assertTrue(all(response["ok"] for response in responses))
        self.assertEqual(len(calls), 6)
        current = controller.status_for("job-1", token)
        self.assertAlmostEqual(current["held_cores"], 0.1)
        self.assertEqual(current["held_mem_mb"], 6000)
        self.assertAlmostEqual(status.current_cpu, 15.9)
        self.assertEqual(status.current_mem, 58000)

    def test_relative_release_id_is_idempotent(self):
        status, controller, calls = self.make_controller(cpu=16, mem_mb=64000)
        env = self.start_job(status, controller, cpu=12, mem_mb=12000)
        token = env[zslurm_lease.ENV_TOKEN]

        first = controller.release_resources(
            "job-1", token, cores=2, mem_mb=1000, release_id="verifybamid"
        )
        duplicate = controller.release_resources(
            "job-1", token, cores=2, mem_mb=1000, release_id="verifybamid"
        )
        conflicting = controller.release_resources(
            "job-1", token, cores=1, mem_mb=1000, release_id="verifybamid"
        )

        self.assertTrue(first["ok"])
        self.assertFalse(first["duplicate"])
        self.assertTrue(duplicate["ok"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["status"], "already-released")
        self.assertFalse(conflicting["ok"])
        self.assertEqual(conflicting["status"], "invalid")
        self.assertEqual(len(calls), 1)
        self.assertEqual(status.current_cpu, 6)
        self.assertEqual(status.current_mem, 53000)

    def test_relative_release_respects_observed_memory_floor(self):
        status, controller, _ = self.make_controller(
            headroom_fraction=0.25, headroom_mb=512
        )
        env = self.start_job(status, controller)
        status.current_mem_usage["job-1"] = 8000

        response = controller.release_resources(
            "job-1",
            env[zslurm_lease.ENV_TOKEN],
            cores=2,
            mem_mb=60000,
            release_id="memory-heavy-consumer",
        )

        self.assertTrue(response["ok"])
        self.assertTrue(response["safety"]["adjusted"])
        self.assertEqual(response["held_mem_mb"], 10512)
        self.assertEqual(response["released_mem_mb"], 53488)

    def test_retried_job_id_releases_each_process_attempt_once(self):
        status, controller, _ = self.make_controller(cpu=16, mem_mb=64000)
        running_processes = {}
        first_process = object()
        second_process = object()

        self.start_job(
            status, controller, job_id="retry-job", cpu=4, mem_mb=8000
        )
        running_processes["retry-job"] = first_process
        self.assertTrue(
            zslurm_lease.claim_finished_attempt(
                running_processes, "retry-job", first_process
            )
        )
        controller.finish_job("retry-job", 4, 8000)
        self.assertEqual(status.current_cpu, 16)
        self.assertEqual(status.current_mem, 64000)

        self.start_job(
            status, controller, job_id="retry-job", cpu=4, mem_mb=16000
        )
        running_processes["retry-job"] = second_process
        self.assertFalse(
            zslurm_lease.claim_finished_attempt(
                running_processes, "retry-job", first_process
            )
        )
        self.assertIs(running_processes["retry-job"], second_process)
        self.assertTrue(
            zslurm_lease.claim_finished_attempt(
                running_processes, "retry-job", second_process
            )
        )
        controller.finish_job("retry-job", 4, 16000)
        self.assertEqual(status.current_cpu, 16)
        self.assertEqual(status.current_mem, 64000)
        self.assertFalse(
            zslurm_lease.claim_finished_attempt(
                running_processes, "retry-job", second_process
            )
        )

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

    def test_unix_socket_backlog_handles_engine_start_bursts(self):
        self.assertGreaterEqual(
            zslurm_lease._ThreadingUnixServer.request_queue_size, 128
        )

    def test_client_retries_transient_connect_failure_before_sending(self):
        class FakeSocket:
            def __init__(self, connect_error=None, response=b""):
                self.connect_error = connect_error
                self.response = response
                self.sent = []
                self.closed = False

            def settimeout(self, _timeout):
                pass

            def connect(self, _socket_path):
                if self.connect_error is not None:
                    raise self.connect_error

            def sendall(self, payload):
                self.sent.append(payload)

            def recv(self, _size):
                response, self.response = self.response, b""
                return response

            def close(self):
                self.closed = True

        first = FakeSocket(BlockingIOError(errno.EAGAIN, "backlog full"))
        second = FakeSocket(response=b'{"ok": true}\n')
        with mock.patch.object(
            zslurm_lease.socket, "socket", side_effect=[first, second]
        ), mock.patch.object(zslurm_lease.time, "sleep"):
            response = zslurm_lease.send_request(
                "/tmp/chief.sock", {"action": "status"}, timeout_s=1.0
            )

        self.assertTrue(response["ok"])
        self.assertTrue(first.closed)
        self.assertEqual(first.sent, [])
        self.assertTrue(second.closed)
        self.assertEqual(len(second.sent), 1)

    def test_relative_release_cli_round_trip(self):
        status, controller, _ = self.make_controller()
        env = self.start_job(status, controller)
        with tempfile.TemporaryDirectory() as tempdir:
            socket_path = os.path.join(tempdir, "chief.sock")
            server = zslurm_lease.LeaseServer(socket_path, controller).start()
            try:
                child_env = os.environ.copy()
                child_env.update(env)
                child_env[zslurm_lease.ENV_SOCKET] = socket_path
                process = subprocess.run(
                    [
                        str(ROOT / "zslurm_lease"),
                        "--json",
                        "release",
                        "--cores",
                        "2",
                        "--mem-mb",
                        "1000",
                        "--release-id",
                        "cli-consumer",
                    ],
                    cwd=ROOT,
                    env=child_env,
                    check=False,
                    capture_output=True,
                    text=True,
                )
            finally:
                server.close()

        self.assertEqual(process.returncode, 0, process.stderr)
        response = json.loads(process.stdout)
        self.assertTrue(response["ok"])
        self.assertEqual(response["status"], "released")
        self.assertEqual(response["release_id"], "cli-consumer")
        self.assertEqual(response["held_cores"], 22)
        self.assertEqual(response["held_mem_mb"], 63000)

    def test_phase_cli_round_trip(self):
        status, controller, _ = self.make_controller()
        env = self.start_job(status, controller)
        with tempfile.TemporaryDirectory() as tempdir:
            socket_path = os.path.join(tempdir, "chief.sock")
            server = zslurm_lease.LeaseServer(socket_path, controller).start()
            try:
                child_env = os.environ.copy()
                child_env.update(env)
                child_env[zslurm_lease.ENV_SOCKET] = socket_path
                process = subprocess.run(
                    [
                        str(ROOT / "zslurm_lease"),
                        "--json",
                        "phase",
                        "sort",
                    ],
                    cwd=ROOT,
                    env=child_env,
                    check=False,
                    capture_output=True,
                    text=True,
                )
            finally:
                server.close()

        self.assertEqual(process.returncode, 0, process.stderr)
        response = json.loads(process.stdout)
        self.assertEqual(response["status"], "phase-changed")
        self.assertEqual(response["phase_name"], "sort")
        self.assertEqual(response["phase_index"], 1)


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

    def add_job(self, cpu=8, mem_mb=32000, requeue=0):
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
            requeue,
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
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(first["epoch"], second["epoch"])
        self.assertFalse(too_large["ok"])
        self.assertEqual(self.engine.res_cpu_reserved, 2)
        self.assertEqual(self.engine.res_mem_reserved_mb, 8000)

    def test_retry_transitions_clear_stale_live_usage(self):
        job = self.add_job()
        job.current_cpu_usage = 3.5
        job.current_mem_usage = 12000

        job.done(1, "failed", "REQUEUED")
        self.assertEqual(job.current_cpu_usage, 0.0)
        self.assertEqual(job.current_mem_usage, 0.0)

        job.current_cpu_usage = 2.0
        job.current_mem_usage = 4000
        job.assigned("test-node")
        self.assertEqual(job.current_cpu_usage, 0.0)
        self.assertEqual(job.current_mem_usage, 0.0)

        job.current_cpu_usage = 1.0
        job.current_mem_usage = 2000
        job.started("test-node")
        self.assertEqual(job.current_cpu_usage, 0.0)
        self.assertEqual(job.current_mem_usage, 0.0)

    def test_phase_report_row_matches_declared_schema(self):
        z = self.zslurm
        phase = {
            "phase_index": 1,
            "phase_name": "tail",
            "event": "set",
            "transition_id": "request-1",
            "lease_epoch": 1,
            "started_at_epoch": 100.0,
            "ended_at_epoch": 110.0,
            "requested_cores": 2.0,
            "requested_mem_mb": 12000.0,
            "held_cores": 2.0,
            "held_mem_mb": 12000.0,
            "duration_s": 10.0,
            "sample_count": 2,
            "sampled_duration_s": 10.0,
            "avg_cpu_cores": 1.0,
            "avg_pss_mb": 2000.0,
            "cpu_cores_percentiles": [1.0] * 7,
            "pss_mb_percentiles": [2000.0] * 7,
            "reserved_core_seconds": 20.0,
            "sampled_reserved_core_seconds": 20.0,
            "used_core_seconds": 10.0,
            "reserved_mem_mb_seconds": 120000.0,
            "sampled_reserved_mem_mb_seconds": 120000.0,
            "pss_mb_seconds": 20000.0,
            "cpu_efficiency": 0.5,
            "memory_efficiency": 1.0 / 6.0,
            "sample_coverage": 1.0,
        }
        row = z._phase_report_row("job", "sample", 0, "node", "42", phase)
        parsed = dict(zip(z.LEASE_PHASE_REPORT_FIELDS, row))

        self.assertEqual(len(row), len(z.LEASE_PHASE_REPORT_FIELDS))
        self.assertEqual(parsed["phase_name"], "tail")
        self.assertEqual(parsed["cores_reserved"], "2.0")
        self.assertEqual(parsed["pss_95"], "2000.0")
        self.assertEqual(parsed["lease_cpu_efficiency"], "0.5")

    def test_report_files_include_dynamic_summary_and_phase_schema(self):
        z = self.zslurm
        with tempfile.TemporaryDirectory() as tempdir:
            report_prefix = os.path.join(tempdir, "report")
            phase_prefix = os.path.join(tempdir, "phases")
            z._open_report_files(
                {
                    "reports_file_prefix": report_prefix,
                    "lease_phase_reports_file_prefix": phase_prefix,
                    "node_reports_enable": False,
                }
            )
            try:
                report_path = next(pathlib.Path(tempdir).glob("report-*.tsv"))
                phase_path = next(pathlib.Path(tempdir).glob("phases-*.tsv"))
                report_header = report_path.read_text().splitlines()[0].split("\t")
                phase_header = phase_path.read_text().splitlines()[0].split("\t")
            finally:
                z.status.reports_file.close()
                z.status.phase_reports_file.close()
                z.status.reports_file = None
                z.status.phase_reports_file = None

        self.assertIn("lease_reserved_core_seconds", report_header)
        self.assertIn("lease_memory_efficiency", report_header)
        self.assertEqual(phase_header, z.LEASE_PHASE_REPORT_FIELDS)

    def test_cancel_with_retry_left_is_removed_from_active_jobs(self):
        job = self.add_job(requeue=1)

        self.jobs.cancel_job(job.jobid, requeue=False)

        self.assertNotIn(job.jobid, self.jobs.jobs_by_id)
        self.assertEqual(job.state, "CANCELLED")
        self.assertEqual(job.requeue, 0)
        self.assertIn(job, self.jobs.finished_jobs_by_owner[None])


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
        self, jobid, name, priority=100, cpu=1, mem_mb=1000,
        partition="compute", ssd_use="no", dcache_transfer_slots=0,
        dcache_download_slots=0, dcache_upload_slots=0,
    ):
        z = self.zslurm
        job = z.Job(
            name, jobid, "true", "/tmp", {}, cpu, mem_mb, 3600, 0, None,
            0, 0, 0, 0, 0, 0, partition, 0, None, "",
            ssd_use=ssd_use,
            dcache_transfer_slots=dcache_transfer_slots,
            dcache_download_slots=dcache_download_slots,
            dcache_upload_slots=dcache_upload_slots,
            owner="pipeline-" + str(priority),
            priority=priority,
        )
        self.jobs.jobs_by_id[job.jobid] = job
        return job

    def dispatch_one(self, partition="compute", mem_mb=128000):
        return self.jobs.request_jobs(
            self.engine.engine_id, 1, mem_mb, partition
        )

    def test_default_priority_is_100(self):
        job = self.add_job("1", "default")
        self.assertEqual(job.priority, 100)
        self.assertEqual(self.zslurm.DEFAULT_JOB_PRIORITY, 100)

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

    def test_active_views_exclude_lingering_terminal_jobs(self):
        pending = self.add_job("1", "pending")
        cancelled = self.add_job("2", "cancelled")
        failed = self.add_job("3", "failed")
        cancelled.state = "CANCELLED"
        failed.state = "FAILED"

        self.assertEqual(
            [row[0] for row in self.jobs.list_jobs()], [pending.jobid]
        )
        stats = self.jobs.queue_stats()
        self.assertEqual(stats["states"], {"PENDING": 1})
        self.assertEqual(stats["total"]["pending"]["jobs"], 1)

    def test_priority_applies_to_archive_transfer_queue(self):
        self.engine.partition = "archive"
        self.jobs.dcache_download_total = 1
        low = self.add_job(
            "1", "low-download", priority=0, partition="archive",
            dcache_download_slots=1,
        )
        high = self.add_job(
            "2", "high-download", priority=25, partition="archive",
            dcache_download_slots=1,
        )

        assigned = self.dispatch_one(partition="archive")

        self.assertEqual(assigned[0][0], high.jobid)
        self.assertEqual(low.state, "PENDING")
        self.assertEqual(self.jobs.dcache_download_inuse, 1)
        self.assertEqual(self.jobs.dcache_upload_inuse, 0)

    def test_download_and_upload_pools_are_independent(self):
        self.jobs.dcache_download_total = 1
        self.jobs.dcache_upload_total = 1
        download = self.add_job("1", "download", dcache_download_slots=1)
        upload = self.add_job("2", "upload", dcache_upload_slots=1)
        another_download = self.add_job(
            "3", "another-download", dcache_download_slots=1
        )

        self.assertTrue(self.jobs._dcache_transfer_fits_locked(download))
        self.jobs._reserve_dcache_transfer_locked(download)
        self.assertTrue(self.jobs._dcache_transfer_fits_locked(upload))
        self.assertFalse(self.jobs._dcache_transfer_fits_locked(another_download))
        self.jobs._reserve_dcache_transfer_locked(upload)
        self.assertEqual(self.jobs.dcache_download_inuse, 1)
        self.assertEqual(self.jobs.dcache_upload_inuse, 1)

    def test_legacy_transfer_slot_reserves_both_pools(self):
        legacy = self.add_job("1", "legacy", dcache_transfer_slots=1)
        self.jobs._reserve_dcache_transfer_locked(legacy)
        self.assertEqual(self.jobs.dcache_download_inuse, 1)
        self.assertEqual(self.jobs.dcache_upload_inuse, 1)
        self.jobs._release_dcache_transfer_locked(legacy)
        self.assertEqual(self.jobs.dcache_download_inuse, 0)
        self.assertEqual(self.jobs.dcache_upload_inuse, 0)

    def test_directional_metadata_overrides_legacy_upgrade_fallback(self):
        job = self.add_job(
            "1", "rolling-upgrade", dcache_transfer_slots=3,
            dcache_download_slots=1,
        )
        self.jobs._reserve_dcache_transfer_locked(job)
        self.assertEqual(self.jobs.dcache_download_inuse, 1)
        self.assertEqual(self.jobs.dcache_upload_inuse, 0)

    def test_transfer_limit_configuration_and_runtime_control(self):
        self.jobs.configure_transfer_limits({"dcache_transfer_slots": 7})
        self.assertEqual(self.jobs.dcache_download_total, 7)
        self.assertEqual(self.jobs.dcache_upload_total, 7)
        result = self.jobs.set_transfer_limits(download_total=2, upload_total=5)
        self.assertEqual(result["download"]["total_slots"], 2)
        self.assertEqual(result["upload"]["total_slots"], 5)

    def test_submit_api_defaults_and_exposes_priority(self):
        jobid = self.jobs.submit_job(
            job_name="submitted", cmd="true", cwd="/tmp", env={
                "ZSLURM_DCACHE_DOWNLOAD_SLOTS": "1",
                "ZSLURM_DCACHE_UPLOAD_SLOTS": "2",
                "ZSLURM_DCACHE_TRANSFER_SLOTS": "2",
            },
            ncpu=1, mem=1000, reqtime=60, requeue=0, dependency=None,
            arch_use_add=0, arch_use_remove=0, dcache_use_add=0,
            dcache_use_remove=0, active_use_add=0, active_use_remove=0,
            partition="compute", info_input_mb=0, info_output_file=None,
            owner="pipeline-a", priority=12,
        )

        job = self.jobs.jobs_by_id[jobid]
        self.assertEqual(job.priority, 12)
        self.assertEqual(job.dcache_download_slots, 1)
        self.assertEqual(job.dcache_upload_slots, 2)
        self.assertEqual(self.jobs._dcache_slot_needs(job), (1, 2))
        detailed = self.jobs.list_jobs_detailed()[0]
        self.assertEqual(detailed["priority"], 12)
        self.assertEqual(detailed["dcache_download_slots"], 1)
        self.assertEqual(detailed["dcache_upload_slots"], 2)
        self.assertEqual(len(self.jobs.list_jobs()), 1)
        self.assertEqual(len(self.jobs.list_jobs()[0]), 14)
        self.assertEqual(self.jobs.list_jobs(None, True)[0][-1], 12)


if __name__ == "__main__":
    unittest.main()
