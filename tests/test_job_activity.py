"""Wait metadata is observability, not a new scheduler state or reservation."""
import ast
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import zslurm_lease as lease
import zslurm_shared as shared
from tests.test_dynamic_leases import FakeChiefStatus, load_zslurm


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def chief():
    status = FakeChiefStatus(8.0, 8000.0)
    calls = []

    def resize(job_id, cpu, memory):
        calls.append((job_id, cpu, memory))
        return {"ok": True, "epoch": len(calls)}

    controller = lease.ChiefLeaseController(
        status, 8.0, 8000.0, resize,
        memory_headroom_mb=0.0, memory_headroom_fraction=0.0,
    )
    env = controller.prepare_job("J1", 8.0, 8000.0)
    status.current_cpu -= 8.0
    status.current_mem -= 8000.0
    return controller, status, env, calls


def test_wait_annotation_is_idempotent_and_metadata_only(chief, monkeypatch):
    controller, status, env, calls = chief
    monkeypatch.setattr(lease.time, "time", lambda: 100.0)
    token = env[lease.ENV_TOKEN]
    first = controller.set_target("J1", token, wait_reason="quota")
    assert first["status"] == "activity-changed"
    assert first["wait_reason_supported"] is True
    assert first["wait_since"] == 100.0
    assert controller.is_intentionally_waiting("J1")
    monkeypatch.setattr(lease.time, "time", lambda: 200.0)
    retry = controller.set_target("J1", token, wait_reason="quota")
    assert retry["wait_since"] == 100.0
    assert retry["status"] == "unchanged"
    assert status.current_cpu == status.current_mem == 0.0
    assert first["held_cores"] == 8.0 and first["held_mem_mb"] == 8000.0
    assert calls == []


def test_omitting_reason_preserves_it_and_explicit_none_clears(chief):
    controller, _, env, _ = chief
    token = env[lease.ENV_TOKEN]
    controller.set_target("J1", token, cores=0.1, mem_mb=1000.0,
                          phase="quota_wait", wait_reason="quota")
    preserved = controller.set_target("J1", token, phase="new_phase")
    assert preserved["wait_reason"] == "quota"
    cleared = controller.set_target("J1", token, wait_reason="none")
    assert cleared["wait_reason"] == "" and cleared["wait_since"] is None
    assert not controller.is_intentionally_waiting("J1")


@pytest.mark.parametrize("invalid", ["arbitrary", 5, {"reason": "quota"}, "quota\n"])
def test_invalid_annotations_have_no_side_effects(chief, invalid):
    controller, _, env, calls = chief
    result = controller.set_target("J1", env[lease.ENV_TOKEN], cores=1.0, wait_reason=invalid)
    assert result["code"] == 2
    assert not controller.is_intentionally_waiting("J1")
    assert calls == []


def test_denied_or_invalid_target_cannot_annotate(chief):
    controller, _, env, _ = chief
    assert controller.set_target("J1", "invalid-token", wait_reason="quota")["code"] == 3
    assert controller.set_target("J1", env[lease.ENV_TOKEN], cores=99,
                                 wait_reason="quota")["code"] == 2
    assert not controller.is_intentionally_waiting("J1")


def test_phase_timestamp_does_not_reset_on_retry_or_relative_release(chief, monkeypatch):
    controller, _, env, _ = chief
    token = env[lease.ENV_TOKEN]
    monkeypatch.setattr(lease.time, "time", lambda: 100.0)
    controller.set_target("J1", token, phase="quota_wait", wait_reason="quota")
    monkeypatch.setattr(lease.time, "time", lambda: 200.0)
    controller.release_resources("J1", token, cores=1.0, release_id="consumer")
    snapshot = controller.activity_snapshot()["J1"]
    assert snapshot["phase_since"] == 100.0
    assert snapshot["wait_since"] == 100.0
    assert "token" not in json.dumps(snapshot)
    assert env[lease.ENV_TOKEN] not in json.dumps(snapshot)


def test_pending_growth_is_visible_as_lease_wait_then_clears(chief):
    controller, status, env, _ = chief
    token = env[lease.ENV_TOKEN]
    controller.set_target("J1", token, cores=1.0, mem_mb=1000.0, wait_reason="quota")
    # Another job consumes the released capacity.
    second = controller.prepare_job("J2", 7.0, 7000.0)
    status.current_cpu -= 7.0
    status.current_mem -= 7000.0
    results = []
    worker = threading.Thread(target=lambda: results.append(controller.set_target(
        "J1", token, cores=8.0, mem_mb=8000.0, timeout_s=5.0,
        phase="copy", wait_reason="none",
    )))
    worker.start()
    try:
        deadline = time.monotonic() + 2.0
        while not controller.has_pending_acquire() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert controller.has_pending_acquire()
        snapshot = controller.activity_snapshot()["J1"]
        assert snapshot["wait_reason"] == "lease"
        assert snapshot["wait_since"] is not None
        assert controller.is_intentionally_waiting("J1")
        controller.finish_job("J2", 7.0, 7000.0)
    finally:
        worker.join(timeout=6.0)
    assert not worker.is_alive()
    assert results[0]["ok"] is True
    assert results[0]["wait_reason"] == "" and results[0]["phase_name"] == "copy"
    assert not controller.is_intentionally_waiting("J1")
    assert second[lease.ENV_TOKEN] != token


def test_explicit_lease_wait_survives_timeout_but_completion_removes_it(chief):
    controller, status, env, _ = chief
    token = env[lease.ENV_TOKEN]
    controller.set_target("J1", token, cores=1.0, mem_mb=1000.0)
    status.current_cpu = status.current_mem = 0.0
    result = controller.set_target("J1", token, cores=8.0, timeout_s=0.0, wait_reason="lease")
    assert result["status"] == "timeout"
    assert controller.activity_snapshot()["J1"]["wait_reason"] == "lease"
    controller.finish_job("J1", 8.0, 8000.0)
    assert "J1" not in controller.activity_snapshot()
    assert not controller.is_intentionally_waiting("J1")


def test_lease_cli_roundtrip_and_selective_protocol_v2(chief, monkeypatch):
    controller, _, env, _ = chief
    # Pytest's GPFS scratch prefix can exceed the AF_UNIX path-length limit.
    with tempfile.TemporaryDirectory(prefix="zwait.", dir="/tmp") as temporary:
        server = lease.LeaseServer(str(Path(temporary) / "lease.sock"), controller).start()
        child_env = dict(os.environ, **env, ZSLURM_LEASE_SOCKET=server.socket_path)
        try:
            for reason in ("quota", "none"):
                result = subprocess.run(
                    [sys.executable, str(ROOT / "zslurm_lease"), "--json", "set", "--wait-reason", reason],
                    cwd=ROOT, env=child_env, capture_output=True, text=True, timeout=10,
                )
                assert result.returncode == 0, result.stderr
                response = json.loads(result.stdout)
                assert response["wait_reason"] == ("quota" if reason == "quota" else "")
        finally:
            server.close()
    # Old chiefs reject v2 before changing anything; ordinary calls stay v1.
    requests = []
    for key, value in child_env.items():
        if key.startswith("ZSLURM_"):
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(lease, "send_request", lambda _socket, request, **_k: requests.append(request))
    lease.request_from_environment("set", cores=1.0)
    lease.request_from_environment("set", cores=0.1, wait_reason="quota")
    assert [request["version"] for request in requests] == [1, 2]


@pytest.fixture(scope="module")
def zmodule():
    return load_zslurm()


@pytest.fixture
def manager(zmodule):
    z = zmodule
    z.gb.lock = threading.RLock()
    z.gb.log_file = io.StringIO()
    z.gb.wlog = None
    z.status.config = {}
    z.status.handover_mode = "ACTIVE"
    z.status.node_reports_file = None
    z.engines = z.EngineManager()
    z.jobs = z.JobManager()
    engine = z.Engine("node1", 8, 8000, "compute", cluster_id=False, managed=False)
    engine.res_cpu_reserved = 8.0
    engine.res_mem_reserved_mb = 8000.0
    z.engines.engine_by_id["node1"] = engine
    job = z.Job("example", "J1", "true", "/tmp", {}, 8, 8000, 3600, 0, None,
                0, 0, 0, 0, 0, 0, "compute", 0, None, "", owner="test")
    job.state, job.node_id, job.starttime = "RUNNING", "node1", 100.0
    z.jobs.jobs_by_id["J1"] = job
    engine.jobs = {"J1"}
    z.jobs.running_jobs = {"compute": 1}
    return z, job, engine


ACTIVITY = dict(phase_name="quota_wait", phase_since=120.0, wait_reason="quota", wait_since=123.0)


def test_heartbeat_updates_metadata_without_changing_reservations_or_state(manager, monkeypatch):
    z, job, engine = manager
    monkeypatch.setattr(z.time, "time", lambda: 200.0)
    args = ("node1", 0.0, 1000.0, 0.0, shared.RUNNING, 0.0, {"J1": 0.0}, {"J1": 1000.0},
            0.0, 0.0, 0.0, 0.0)
    z.poll_with_job_activity(*(args + (0.0, 0.0, 0.0, False, {"J1": ACTIVITY})))
    assert job.state == "RUNNING" and job.node_id == "node1"
    assert engine.res_cpu_reserved == 8.0 and engine.res_mem_reserved_mb == 8000.0
    assert job.wait_reason == "quota" and job.wait_since == 123.0
    assert z.jobs.list_jobs_detailed()[0]["wait_seconds"] == 77.0
    assert len(z.jobs.list_jobs()[0]) == 14
    assert len(z.jobs.list_jobs(None, True)[0]) == 15
    extended = z.jobs.list_jobs(None, True, False, True)[0]
    assert len(extended) == 20 and extended[-3:] == ["quota", 123.0, 77.0]
    assert len(z.jobs.list_jobs(None, True, True)[0]) == 17
    combined = z.jobs.list_jobs(None, True, True, True)[0]
    assert len(combined) == 22 and combined[15:17] == [0, []]
    assert combined[17:] == extended[15:]
    assert z.jobs.queue_stats()["waiting"]["quota"] == dict(jobs=1, longest_wait_s=77.0)
    assert z.ping()["list_jobs_activity"] is True
    assert z.ping()["job_activity"] is True
    # Old poll variants remain callable and do not invent or erase metadata.
    z.poll(*args)
    assert job.wait_reason == "quota"
    z.poll_with_job_activity(*(args + (0.0, 0.0, 0.0, False, {})))
    assert job.wait_reason == "" and job.wait_since is None


@pytest.mark.parametrize("invalid", [None, 5, dict(wait_reason="unknown"),
    dict(phase_name="x" * 129), dict(phase_name="bad\nname"),
    dict(wait_reason="quota"), dict(wait_reason="quota", wait_since=float("nan")),
    dict(wait_reason="quota", wait_since=-1), dict(phase_since="bad")])
def test_manager_ignores_malformed_entries(manager, invalid):
    z, job, _ = manager
    z.jobs.update_job_activity("node1", {"J1": ACTIVITY}, {"J1"})
    z.jobs.update_job_activity("node1", {"J1": invalid}, {"J1"})
    assert job.wait_reason == "quota"


def test_only_own_running_known_jobs_can_be_annotated(manager):
    z, job, _ = manager
    z.jobs.update_job_activity("different-node", {"J1": ACTIVITY}, {"J1"})
    assert job.wait_reason == ""
    z.jobs.update_job_activity("node1", {"J1": ACTIVITY}, set())
    assert job.wait_reason == ""
    job.state = "PENDING"
    z.jobs.update_job_activity("node1", {"J1": ACTIVITY}, {"J1"})
    assert job.wait_reason == ""


def test_metadata_survives_handover_and_clears_on_finish_and_restart(manager):
    z, job, _ = manager
    z.jobs.update_job_activity("node1", {"J1": ACTIVITY}, {"J1"})
    imported = z._job_from_handover(z._snapshot_job(job))
    assert imported.wait_reason == "quota" and imported.wait_since == 123.0
    job.done(0, "done", "COMPLETED")
    assert job.wait_reason == "" and job.wait_since is None
    job.wait_reason, job.wait_since = "quota", 123.0
    job.started("node1")
    assert job.wait_reason == "" and job.phase_name == ""


def selected_chief_functions(*names):
    tree = ast.parse((ROOT / "zslurm_chief").read_text())
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "chief_helpers", "exec"), namespace)
    return namespace


@pytest.mark.parametrize("activity_supported,gpfs_supported,method", [
    (True, True, "poll_with_job_activity"), (True, False, "poll_with_job_activity"),
    (False, True, "poll_with_gpfs"), (False, False, "poll"),
])
def test_chief_uses_one_existing_heartbeat_and_old_manager_fallback(
    chief, activity_supported, gpfs_supported, method
):
    controller, _, _, _ = chief
    calls = []
    proxy = SimpleNamespace(**{name: (lambda *args, name=name: calls.append((name, args)))
                              for name in ("poll", "poll_with_gpfs", "poll_with_job_activity")})
    args = tuple(range(12))
    gpfs = dict(rx_mib_s=1.0, tx_mib_s=2.0, io_pct=3.0, available=True)
    selected_chief_functions("poll_manager")["poll_manager"](
        proxy, args, gpfs, controller, activity_supported, gpfs_supported)
    assert len(calls) == 1 and calls[0][0] == method
    expected = args if method == "poll" else args + (1.0, 2.0, 3.0, True)
    if activity_supported:
        expected += (controller.activity_snapshot(),)
    assert calls[0][1] == expected


def test_stuck_detection_resets_window_after_intentional_wait():
    detector = selected_chief_functions("stuck_window_is_idle")["stuck_window_is_idle"]
    cpu, io_samples = [{"cpu_perc": 0.0}] * 100, [0.0] * 100
    assert detector(cpu, io_samples, 3, 0.05, 4096)
    # The actual monitor sets its cutoff to len(samples) throughout a wait.
    assert not detector(cpu, io_samples, 3, 0.05, 4096, history_start=100)
    assert not detector(cpu + cpu[:3], io_samples + io_samples[:3], 3, 0.05, 4096, history_start=100)
    assert detector(cpu + cpu[:4], io_samples + io_samples[:4], 3, 0.05, 4096, history_start=100)
    text = (ROOT / "zslurm_chief").read_text()
    assert "lease_controller.is_intentionally_waiting(jobid)" in text
    assert "not intentionally_waiting" in text
    assert "history_start=stuck_history_start" in text


@pytest.mark.parametrize("capabilities,extra", [({}, []), ({"list_jobs_priority": True}, [100]),
    ({"list_jobs_activity": True}, [100, "quota_wait", 120.0, "quota", 123.0, 2520.0]),
    ({"gpu_submit_protocol": 1}, [100, 1, ["0"]]),
    ({"gpu_submit_protocol": 1, "list_jobs_activity": True},
     [100, 1, ["0"], "quota_wait", 120.0, "quota", 123.0, 2520.0])])
@pytest.mark.parametrize("output_mode", ["--json", "--parseable", "pretty"])
def test_queue_client_old_and_new_managers(manager, monkeypatch, capsys, capabilities, extra, output_mode):
    z, job, _ = manager
    row = z.jobs.list_jobs()[0] + extra
    calls = []
    proxy = SimpleNamespace(
        ping=lambda: capabilities,
        list_jobs=lambda *args: calls.append(args) or [row],
    )
    monkeypatch.setattr(shared, "get_instance_names", lambda: ["test"])
    monkeypatch.setattr(shared, "get_job_url", lambda **_k: "unused")
    monkeypatch.setattr(shared, "TimeoutServerProxy", lambda *_a, **_k: proxy)
    monkeypatch.setattr(sys, "argv", ["zsqueue"] + ([] if output_mode == "pretty" else [output_mode]))
    with pytest.raises(SystemExit) as stop:
        runpy.run_path(str(ROOT / "zsqueue"), run_name="__main__")
    assert stop.value.code == 0
    output = capsys.readouterr().out
    supported = bool(capabilities.get("list_jobs_activity"))
    gpu_supported = capabilities.get("gpu_submit_protocol") == 1
    expected_args = ((None, True, True, True) if supported and gpu_supported
                     else (None, True, True) if supported or gpu_supported
                     else (None, True) if extra else ())
    assert calls == [expected_args]
    if output_mode == "--json":
        data = json.loads(output)
        assert data["schema_version"] == 3
        item = data["instances"]["test"][0]
        assert item["state"] == "RUNNING"
        assert item["gpus"] == (1 if gpu_supported else 0)
        assert item["gpu_devices"] == (["0"] if gpu_supported else [])
        assert item["wait_reason"] == ("quota" if supported else "")
        assert item["wait_seconds"] == (2520.0 if supported else 0.0)
    elif output_mode == "--parseable":
        lines = output.splitlines()
        assert len(lines[0].split("\t")) == len(lines[1].split("\t")) == 23
        assert "RUNNING" in lines[1]
    else:
        assert "ACTIVITY" in output and "RUNNING" in output
        if supported:
            assert "WAIT_QUOTA 42m" in output
