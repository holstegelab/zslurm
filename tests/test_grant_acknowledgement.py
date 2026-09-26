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
        "zslurm_grant_ack_under_test", str(ZSLURM_PATH)
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
        assert manager.can_run_assigned_job("node1", "J1") is True

    start.assert_called_once_with("node1", "J1")


def test_chief_retains_assignment_until_acknowledgement_arrives():
    source = CHIEF_PATH.read_text()
    start = source.index("if state == 'ASSIGNED':")
    end = source.index("job_starter.claim(job)", start)
    block = source[start:end]

    keep = block.index("status.assigned_jobs[jobid] = job")
    acknowledge = block.index("permission = s.can_run_assigned_job")
    release = block.index("status.assigned_jobs.pop(jobid, None)")
    assert keep < acknowledge < release
