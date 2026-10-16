"""Explicit controller waiting must not look like a stuck scientific task."""
import ast
import os
from pathlib import Path
import shlex
import sys

import pytest

import zslurm_startup as startup
from test_chief_startup import fixture


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('role', [None, 'compute', 'controller'])
def test_only_explicit_controller_disables_activity_heuristic(enabled, role):
    assert startup.stuck_detection_enabled({'stuck_detection_enable':enabled}, role) is (
        enabled and role != 'controller')


@pytest.mark.parametrize('role', ['', 'false', 'CONTROLLER', 0, False, {}, 'unknown'])
def test_invalid_roles_do_not_silently_disable_monitoring(role):
    with pytest.raises(ValueError): startup.stuck_detection_enabled({}, role)


@pytest.mark.parametrize('role', ['compute', 'controller'])
def test_actual_child_receives_no_inheritable_opt_out_but_monitor_does(tmp_path, role):
    starter, state, leases, job, clock, finished, observed = fixture(tmp_path)
    original_monitor = starter.monitor
    roles = []
    def monitor(*args, **kwargs):
        roles.append(kwargs)
        original_monitor(*args)
    starter.monitor = monitor
    env = dict(job[4], **{startup.MONITOR_ROLE_ENV:role})
    command = shlex.join([sys.executable, '-c',
        'import os; assert "ZSLURM_JOB_MONITOR_ROLE" not in os.environ; print("normal child env")'])
    submitted = (job[0], job[1], command, job[3], env, *job[5:])
    assert starter.claim(submitted)
    starter.run_ready()
    assert finished.wait(10)
    assert state.return_codes == {'J1':0}
    assert roles == [dict(job_role=role)]
    assert env[startup.MONITOR_ROLE_ENV] == role  # submission was not mutated
    assert len(observed) == 1 and state.current_cpu == 8


def test_chief_environment_never_implicitly_exempts_jobs(tmp_path, monkeypatch):
    starter, state, leases, job, clock, finished, observed = fixture(tmp_path)
    monkeypatch.setenv(startup.MONITOR_ROLE_ENV, 'controller')
    command = shlex.join([sys.executable, '-c',
        'import os; assert "ZSLURM_JOB_MONITOR_ROLE" not in os.environ'])
    submitted = (job[0], job[1], command, job[3], None, *job[5:])
    assert starter.claim(submitted); starter.run_ready()
    assert finished.wait(10) and state.return_codes == {'J1':0}


def test_invalid_role_fails_before_spawn_and_releases_reservation(tmp_path):
    starter, state, leases, job, clock, finished, observed = fixture(tmp_path)
    env = dict(job[4], **{startup.MONITOR_ROLE_ENV:'typo'})
    assert starter.claim((*job[:4], env, *job[5:])); starter.run_ready()
    assert state.return_codes == {'J1':-20}
    assert state.running_processes == {} and starter.pending == {} and not observed
    assert state.current_cpu == 8 and state.current_mem == 16000


def test_actual_monitor_selects_role_policy_not_unconditional_global_switch():
    source = Path(startup.__file__).with_name('zslurm_chief')
    tree = ast.parse(source.read_text())
    monitor = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'job_monitor')
    assignments = [n for n in ast.walk(monitor) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == 'stuck_enabled' for t in n.targets)]
    assert len(assignments) == 1
    assert ast.unparse(assignments[0].value) == 'stuck_detection_enabled(config, job_role)'
