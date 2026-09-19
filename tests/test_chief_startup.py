import ast
import errno
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

import zslurm_lease
import zslurm_startup as startup


def fixture(tmp_path):
    state = SimpleNamespace(
        lock=threading.RLock(), current_cpu=8.0, current_mem=16000.0,
        running_processes={}, current_cpu_usage={}, current_mem_usage={},
        return_codes={}, reports={},
    )
    wake = threading.Event()
    finished = threading.Event()
    observed = []
    clock = [0.0]
    leases = zslurm_lease.ChiefLeaseController(
        status=state, total_cpu=8, total_mem_mb=16000,
        manager_resize=lambda *args: {'ok': True}, wake_event=wake,
    )

    def monitor(jobid, process, cores, mem, logfile, logfile_path):
        rc = process.wait(timeout=10)
        with state.lock:
            assert zslurm_lease.claim_finished_attempt(state.running_processes, jobid, process)
            leases.finish_job(jobid, cores, mem)
            state.current_cpu_usage.pop(jobid)
            state.current_mem_usage.pop(jobid)
            state.return_codes[jobid] = rc
        logfile.close()
        observed.append((jobid, logfile_path))
        finished.set()

    starter = startup.DeferredJobStarts(
        state, leases, '/tmp/test-only.sock', monitor, wake,
        retry_seconds=20, retry_max_seconds=300, clock=lambda: clock[0],
    )
    command = shlex.join([sys.executable, '-c', "print('child completed')"])
    job = ('J1', 'test_rule', command, str(tmp_path), dict(os.environ), 8.0, 16000.0, 'RUNNING')
    return starter, state, leases, job, clock, finished, observed


@pytest.mark.parametrize('err', [errno.EDQUOT, errno.ENOSPC, errno.EIO, errno.ESTALE])
@pytest.mark.parametrize('operation', ['open', 'makedirs'])
def test_storage_error_waits_then_starts_exactly_once(tmp_path, err, operation):
    starter, state, leases, job, clock, finished, observed = fixture(tmp_path)
    original_cwd = os.getcwd()
    assert starter.claim(job)
    assert not starter.claim(job)
    assert state.current_cpu == 0 and state.current_mem == 0
    target = startup if operation == 'open' else startup.os
    with mock.patch.object(target, operation, create=True,
                           side_effect=OSError(err, 'injected storage failure')) as failing:
        starter.run_ready()
        starter.run_ready()
        assert failing.call_count == 1  # Backoff, not a busy retry loop.
        assert starter.pending['J1']['retry_at'] == 20
        assert state.return_codes == {}
        assert state.running_processes == {} and leases.leases == {}
        assert state.current_cpu_usage == {'J1': 0}
        assert os.getcwd() == original_cwd
        # A completion/lease thread can still acquire the shared lock.
        acquired = []
        def probe():
            ok = state.lock.acquire(timeout=1)
            acquired.append(ok)
            if ok:
                state.lock.release()
        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(2)
        assert acquired == [True]
    clock[0] = 20
    starter.run_ready()  # Must retry despite having zero free reserved cores.
    assert finished.wait(10)
    starter.run_ready()
    assert len(observed) == 1 and state.return_codes == {'J1': 0}
    assert state.current_cpu == 8 and state.current_mem == 16000
    assert not starter.pending and not leases.leases
    assert Path(observed[0][1]).read_text() == 'child completed\n'


def test_cancel_waiting_start_returns_reservation_once_and_never_spawns(tmp_path):
    starter, state, leases, job, clock, finished, observed = fixture(tmp_path)
    starter.claim(job)
    with mock.patch.object(startup, 'open', create=True, side_effect=OSError(errno.EDQUOT, 'quota')):
        starter.run_ready()
    assert starter.cancel('J1')
    assert not starter.cancel('J1')
    clock[0] = 1000
    with mock.patch.object(startup.subprocess, 'Popen') as spawn:
        starter.run_ready()
        spawn.assert_not_called()
    assert state.return_codes == {'J1': 143}
    assert state.current_cpu == 8 and state.current_mem == 16000
    assert not state.current_cpu_usage and not state.current_mem_usage
    assert not starter.pending and not leases.leases


def test_spawn_failure_discards_only_prepared_lease_then_recovers(tmp_path):
    starter, state, leases, job, clock, finished, observed = fixture(tmp_path)
    starter.claim(job)
    with mock.patch.object(startup.subprocess, 'Popen', side_effect=OSError(errno.EAGAIN, 'process limit')):
        starter.run_ready()
    assert not leases.leases and not state.running_processes
    assert state.current_cpu == 0 and state.current_mem == 0
    assert state.return_codes == {}
    clock[0] = 20
    starter.run_ready()
    assert finished.wait(10)
    assert len(observed) == 1
    assert state.current_cpu == 8 and state.current_mem == 16000


def test_thread_failure_happens_before_spawn(tmp_path):
    starter, state, leases, job, clock, finished, observed = fixture(tmp_path)
    starter.claim(job)
    with mock.patch.object(startup.threading.Thread, 'start', side_effect=RuntimeError("can't start new thread")), \
            mock.patch.object(startup.subprocess, 'Popen') as spawn:
        starter.run_ready()
        spawn.assert_not_called()
    assert not leases.leases and state.return_codes == {}
    clock[0] = 20
    starter.run_ready()
    assert finished.wait(10)
    assert state.current_cpu == 8 and state.current_mem == 16000


@pytest.mark.parametrize('opening_fails', [False, True])
def test_cancel_during_log_open_does_not_resurrect_job(tmp_path, opening_fails):
    starter, state, leases, job, clock, finished, observed = fixture(tmp_path)
    starter.claim(job)
    opened = threading.Event()
    release = threading.Event()
    real_open = open
    handles, errors = [], []
    def blocking_open(*args):
        opened.set()
        assert release.wait(3)
        if opening_fails:
            raise OSError(errno.EDQUOT, 'quota')
        handle = real_open(*args)
        handles.append(handle)
        return handle
    def attempt():
        try:
            starter.run_ready()
        except Exception as error:
            errors.append(error)
    with mock.patch.object(startup, 'open', create=True, side_effect=blocking_open), \
            mock.patch.object(startup.subprocess, 'Popen') as spawn:
        thread = threading.Thread(target=attempt)
        thread.start()
        assert opened.wait(3)
        assert starter.cancel('J1')  # Not blocked behind GPFS I/O.
        release.set()
        thread.join(3)
        assert not thread.is_alive() and not errors
        spawn.assert_not_called()
    assert all(handle.closed for handle in handles)
    assert state.current_cpu == 8 and state.current_mem == 16000


def test_backoff_caps_and_stop_cancels_all_pending_starts(tmp_path):
    starter, state, leases, job, clock, finished, observed = fixture(tmp_path)
    starter.claim(job)
    with mock.patch.object(startup, 'open', create=True, side_effect=OSError(errno.EDQUOT, 'quota')):
        delays = []
        for _ in range(8):
            starter.run_ready()
            entry = starter.pending['J1']
            delays.append(entry['retry_at'] - clock[0])
            clock[0] = entry['retry_at']
    assert delays == [20, 40, 80, 160, 300, 300, 300, 300]
    starter.cancel_all()
    assert not starter.pending and state.return_codes == {'J1': 143}
    assert state.current_cpu == 8 and state.current_mem == 16000


def test_permanent_invalid_command_fails_only_that_job(tmp_path):
    starter, state, leases, job, clock, finished, observed = fixture(tmp_path)
    state.running_processes['unrelated'] = object()
    job = job[:2] + ('unterminated "',) + job[3:]
    starter.claim(job)
    starter.run_ready()
    assert state.return_codes == {'J1': -20}
    assert set(state.running_processes) == {'unrelated'}
    assert state.current_cpu == 8 and state.current_mem == 16000


def test_memory_headroom_rechecked_after_quota_wait(tmp_path):
    starter, state, leases, job, clock, finished, observed = fixture(tmp_path)
    starter.memory_available = lambda: 100
    starter.claim(job)
    with mock.patch.object(startup, 'open', create=True) as opening:
        starter.run_ready()
        opening.assert_not_called()
    assert state.return_codes == {} and 'J1' in starter.pending
    starter.memory_available = lambda: 16000
    starter.run_ready()
    assert finished.wait(10)


def test_engine_slurm_identity_and_original_environment_are_preserved(tmp_path, monkeypatch):
    starter, state, leases, job, clock, finished, observed = fixture(tmp_path)
    monkeypatch.setenv('SLURM_JOB_ID', 'engine-allocation')
    env = {'SLURM_JOB_ID': 'controller-allocation', 'CUSTOM': 'kept'}
    code = "import os; print(os.environ['SLURM_JOB_ID'], os.environ['CUSTOM'])"
    job = job[:2] + (shlex.join([sys.executable, '-c', code]), job[3], env) + job[5:]
    starter.claim(job)
    starter.run_ready()
    assert finished.wait(10)
    assert Path(observed[0][1]).read_text() == 'engine-allocation kept\n'
    assert env == {'SLURM_JOB_ID': 'controller-allocation', 'CUSTOM': 'kept'}


def test_child_scratch_context_survives_retry_and_is_cleaned(tmp_path):
    starter, state, leases, job, clock, finished, observed = fixture(tmp_path)
    prepared_paths = []
    cleaned_paths = []
    environment_before_prepare = []
    cleanup_done = threading.Event()

    def prepare_environment(env, jobid):
        environment_before_prepare.append({
            name: env.get(name)
            for name in (
                'ZSLURM_SCRATCH_ROOT', 'ZSLURM_SCRATCH_DIR',
                'TMPDIR', 'TMP', 'TEMP', 'TEMPDIR',
            )
        })
        path = tmp_path / f'scratch-{len(prepared_paths)}'
        path.mkdir()
        prepared_paths.append(path)
        env['ZSLURM_SCRATCH_DIR'] = str(path)
        env['TMPDIR'] = str(path)
        return str(path)

    def cleanup_context(path):
        cleaned_paths.append(Path(path))
        shutil.rmtree(path)

    original_monitor = starter.monitor

    def monitor_with_context(
        jobid, process, cores, mem, logfile, logfile_path, context
    ):
        try:
            original_monitor(
                jobid, process, cores, mem, logfile, logfile_path
            )
        finally:
            cleanup_context(context)
            cleanup_done.set()

    starter.prepare_child_environment = prepare_environment
    starter.cleanup_child_context = cleanup_context
    starter.monitor = monitor_with_context
    controller_env = dict(os.environ)
    controller_env.update({
        'TMPDIR': '/controller/tmp',
        'ZSLURM_SCRATCH_DIR': '/controller/scratch',
    })
    code = (
        "import os; print(os.environ['ZSLURM_SCRATCH_DIR'], "
        "os.environ['TMPDIR'])"
    )
    job = job[:2] + (
        shlex.join([sys.executable, '-c', code]), job[3], controller_env
    ) + job[5:]

    starter.claim(job)
    with mock.patch.object(
        startup.subprocess, 'Popen',
        side_effect=OSError(errno.EAGAIN, 'process limit'),
    ):
        starter.run_ready()

    assert cleaned_paths == [prepared_paths[0]]
    assert not prepared_paths[0].exists()
    assert all(value is None for value in environment_before_prepare[0].values())

    clock[0] = 20
    starter.run_ready()
    assert finished.wait(10)
    assert cleanup_done.wait(10)
    assert cleaned_paths == prepared_paths
    assert not prepared_paths[1].exists()
    assert Path(observed[0][1]).read_text() == (
        f'{prepared_paths[1]} {prepared_paths[1]}\n'
    )
    assert controller_env['TMPDIR'] == '/controller/tmp'
    assert controller_env['ZSLURM_SCRATCH_DIR'] == '/controller/scratch'


def test_chief_diagnostic_write_and_flush_failures_are_nonfatal():
    stream = mock.Mock()
    stream.write.side_effect = OSError(errno.EDQUOT, 'quota')
    stream.flush.side_effect = OSError(errno.ENOSPC, 'full')
    safe = startup.BestEffortDiagnosticStream(stream)
    assert safe.write('heartbeat') == 9
    safe.flush()
    stream.write.side_effect = None
    safe.write('recovered')
    stream.write.assert_called_with('recovered')


def test_chief_integration_uses_exception_safe_locks_and_pending_ownership():
    path = Path(__file__).resolve().parents[1] / 'zslurm_chief'
    source = path.read_text()
    tree = ast.parse(source)
    assert 'status.lock.acquire()' not in source
    assert 'os.chdir(cwd)' not in source
    assert 'job_starter.cancel(jobid)' in source
    assert 'idle = not status.running_processes and not job_starter.pending' in source
    # Startup retry is outside the resource lock, before heartbeats, and not
    # conditional on free cores (its resources are already reserved).
    loop = next(node for outer in tree.body if isinstance(outer, ast.Try)
                for node in outer.body if isinstance(node, ast.While))
    retry = next(node for node in loop.body if isinstance(node, ast.If)
                 and any(isinstance(call, ast.Call) and ast.unparse(call.func) == 'job_starter.run_ready'
                         for call in ast.walk(node)))
    assert ast.unparse(retry.test) == 'mode == zslurm_shared.RUNNING'
