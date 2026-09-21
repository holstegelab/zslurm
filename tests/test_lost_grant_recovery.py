import ast
import importlib.machinery
import importlib.util
import pathlib
from types import SimpleNamespace
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
ZSLURM_PATH = ROOT / "zslurm"
CHIEF_PATH = ROOT / "zslurm_chief"


def load_zslurm():
    loader = importlib.machinery.SourceFileLoader(
        "zslurm_lost_grant_under_test", str(ZSLURM_PATH)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_request_jobs_never_marks_a_grant_running_before_delivery():
    tree = ast.parse(ZSLURM_PATH.read_text())
    manager = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "JobManager"
    )
    request_jobs = next(
        node for node in manager.body
        if isinstance(node, ast.FunctionDef) and node.name == "request_jobs"
    )
    called_attributes = [
        node.func.attr for node in ast.walk(request_jobs)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]

    assert "assigned" in called_attributes
    assert "started" not in called_attributes
    assert "job_start" not in called_attributes


def test_start_acknowledgement_is_idempotent_for_same_engine():
    z = load_zslurm()
    manager = z.JobManager()
    job = SimpleNamespace(
        jobid="J1", state="ASSIGNED", node_id="node1",
        ssd_use="no", ssd_gb=0.0,
    )
    manager.jobs_by_id[job.jobid] = job
    engine = SimpleNamespace(
        stopping=False, has_ssd=False, ssd_total_gb=0.0,
        res_ssd_reserved_gb=0.0,
    )

    def commit(nodeid, jobid):
        assert nodeid == "node1" and jobid == "J1"
        job.state = "RUNNING"

    with mock.patch.object(z.engines, "engine_by_id", {"node1": engine}), \
            mock.patch.object(manager, "job_start", side_effect=commit) as start:
        assert manager.can_run_assigned_job("node1", "J1") is True
        # Simulate loss of the first XML-RPC response. The retry succeeds but
        # must not reserve/start the manager-side job a second time.
        assert manager.can_run_assigned_job("node1", "J1") is True

    start.assert_called_once_with("node1", "J1")


def test_missing_worker_heartbeat_requeues_only_after_grace():
    z = load_zslurm()
    manager = z.JobManager()
    job = SimpleNamespace(
        jobid="J1", job_name="rule", state="RUNNING", node_id="node1",
        starttime=100.0, heartbeat_missing_since=None,
        heartbeat_miss_count=0,
    )
    manager.jobs_by_id[job.jobid] = job
    old_config = z.status.config
    z.status.config = {
        "worker_grant_heartbeat_grace_sec": 180,
        "worker_grant_heartbeat_misses": 3,
    }
    try:
        with mock.patch.object(manager, "job_done") as done, \
                mock.patch.object(z, "add_log_line"):
            assert manager.reconcile_engine_heartbeat("node1", [], now=200) == []
            assert manager.reconcile_engine_heartbeat("node1", [], now=300) == []
            assert manager.reconcile_engine_heartbeat("node1", [], now=381) == ["J1"]
            done.assert_called_once_with(
                "J1", z.RC_ENGINE_TERMINATED,
                reason="Lost grant: absent from worker heartbeat",
            )
    finally:
        z.status.config = old_config


def test_known_pending_or_completion_grant_resets_miss_counter():
    z = load_zslurm()
    manager = z.JobManager()
    job = SimpleNamespace(
        jobid="J1", job_name="rule", state="RUNNING", node_id="node1",
        starttime=100.0, heartbeat_missing_since=150.0,
        heartbeat_miss_count=9,
    )
    manager.jobs_by_id[job.jobid] = job

    with mock.patch.object(manager, "job_done") as done:
        assert manager.reconcile_engine_heartbeat(
            "node1", ["J1"], now=1000
        ) == []
    done.assert_not_called()
    assert job.heartbeat_missing_since is None
    assert job.heartbeat_miss_count == 0


def test_chief_keeps_assignment_until_ack_and_heartbeats_all_owned_grants():
    source = CHIEF_PATH.read_text()
    start = source.index("if state == 'ASSIGNED':")
    end = source.index("job_starter.claim(job)", start)
    block = source[start:end]

    keep = block.index("status.assigned_jobs[jobid] = job")
    acknowledge = block.index("permission = s.can_run_assigned_job")
    release = block.index("status.assigned_jobs.pop(jobid, None)")
    assert keep < acknowledge < release
    assert "set(status.return_codes)" in source
    assert "heartbeat_cpu_usage.setdefault" in source
