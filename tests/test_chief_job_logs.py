import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHIEF_PATH = ROOT / "zslurm_chief"


def load_retain_completed_job_log():
    tree = ast.parse(CHIEF_PATH.read_text(encoding="utf-8"))
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "retain_completed_job_log"
    ]
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    namespace = {}
    exec(compile(module, str(CHIEF_PATH), "exec"), namespace)
    return namespace["retain_completed_job_log"]


def test_failed_job_logs_are_retained_independent_of_message_text():
    retain = load_retain_completed_job_log()

    assert retain(0) is False
    assert retain(1) is True
    assert retain(137) is True
    assert retain(-254) is True


def test_chief_no_longer_classifies_failure_by_log_text():
    source = CHIEF_PATH.read_text(encoding="utf-8")

    assert "'error in file' not in log_insides.lower()" not in source
    assert "if not retain_completed_job_log(retcode):" in source
