"""Retry child startup without losing the engine or double-reserving resources.

After the chief acknowledges an ASSIGNED grant, pending starts retain the
resulting RUNNING reservation (including storage/transfer reservations), report
zero usage, and do not consume a workflow retry.
"""
import errno
import os
import re
import shlex
import subprocess
import sys
import threading
import time

import zslurm_lease
import zslurm_gpu

MONITOR_ROLE_ENV = 'ZSLURM_JOB_MONITOR_ROLE'


def controller_monitor_supported(version):
    """Fail closed when a rolling deployment still has legacy chiefs."""
    if not isinstance(version, str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        return False
    return tuple(int(v) for v in version.split('.')) >= (0, 2, 4)


def stuck_detection_enabled(config, job_role=None):
    """CPU/I/O silence is not proof that a queue-waiting controller is stuck.

    This changes only that heuristic; normal accounting, cancellation and
    allocation limits remain active. Unknown roles must not silently opt out.
    """
    if job_role not in (None, 'compute', 'controller'):
        raise ValueError('job monitor role must be compute or controller')
    return bool(config.get('stuck_detection_enable', True)) and job_role != 'controller'


TRANSIENT_START_ERRNOS = frozenset(
    getattr(errno, name) for name in (
        'EDQUOT', 'ENOSPC', 'EIO', 'ESTALE', 'EROFS', 'ETIMEDOUT',
        'EAGAIN', 'ENOMEM', 'EMFILE', 'ENFILE',
    ) if hasattr(errno, name)
)


class BestEffortDiagnosticStream:
    """Chief diagnostics must not abort scheduling when its Slurm log is full.

    This is NOT applied to child-job output files. Those must open successfully
    before launch, and a failed open follows the explicit startup retry path.
    """
    def __init__(self, stream):
        self.stream = stream

    def write(self, value):
        try:
            return self.stream.write(value)
        except OSError:
            return len(value)

    def flush(self):
        try:
            return self.stream.flush()
        except OSError:
            return None

    def __getattr__(self, name):
        return getattr(self.stream, name)


class DeferredJobStarts:
    def __init__(self, status, leases, socket_path, monitor, wake_event,
                 retry_seconds=20.0, retry_max_seconds=300.0, clock=None,
                 memory_available=None, prepare_child_environment=None,
                 cleanup_child_context=None):
        self.status = status
        self.leases = leases
        self.socket_path = socket_path
        self.monitor = monitor
        self.wake_event = wake_event
        self.retry_seconds = max(1.0, float(retry_seconds))
        self.retry_max_seconds = max(self.retry_seconds, float(retry_max_seconds))
        self.clock = clock or time.monotonic
        self.memory_available = memory_available
        self.prepare_child_environment = prepare_child_environment
        self.cleanup_child_context = cleanup_child_context
        self.pending = {}

    @staticmethod
    def log(message):
        # A full filesystem may also prevent writing the chief's own Slurm log.
        # Diagnostic failure must not turn an already handled startup error into
        # an engine-wide exception.
        try:
            stamp = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())
            sys.stderr.write(f'{stamp} - {message}\n')
            sys.stderr.flush()
        except OSError:
            pass

    def claim(self, job):
        """Mirror a manager grant locally, exactly once, before attempting I/O."""
        jobid, name, command, cwd, env, cores, mem, state = job
        if state != 'RUNNING':
            raise ValueError('Only manager-granted RUNNING jobs may be claimed')
        with self.status.lock:
            if jobid in self.pending or jobid in self.status.running_processes:
                return False
            self.pending[jobid] = {'job': job, 'retry_at': 0.0, 'failures': 0}
            self.status.current_cpu -= cores
            self.status.current_mem -= mem
            # Preserve ownership during manager handover/reconciliation too.
            self.status.current_cpu_usage[jobid] = 0.0
            self.status.current_mem_usage[jobid] = 0.0
            return True

    def cancel(self, jobid, return_code=143):
        """Cancel a not-yet-spawned grant and release its local holding once."""
        with self.status.lock:
            entry = self.pending.pop(jobid, None)
            if entry is None:
                return False
            job = entry['job']
            self.status.current_cpu += job[5]
            self.status.current_mem += job[6]
            self.status.current_cpu_usage.pop(jobid, None)
            self.status.current_mem_usage.pop(jobid, None)
            self.status.return_codes[jobid] = return_code
            self.wake_event.set()
            return True

    def cancel_all(self):
        with self.status.lock:
            for jobid in list(self.pending):
                self.cancel(jobid)

    def run_ready(self):
        with self.status.lock:
            ready = [(key, entry) for key, entry in self.pending.items()
                     if self.clock() >= entry['retry_at']]
        for jobid, entry in ready:
            if self.memory_available is not None and entry['job'][6] > self.memory_available():
                continue
            self._start(jobid, entry)

    def _start(self, jobid, entry):
        jobid, name, command, cwd, original_env, cores, mem, _ = entry['job']
        logfile = None
        prepared = False
        child_context = None
        started = threading.Event()
        child = {}

        def monitor_when_spawned():
            started.wait()
            process = child.get('process')
            if process is not None:
                monitor_args = [
                    jobid, process, cores, mem,
                    child['logfile'], child['logfile_path'],
                ]
                if child.get('context') is not None:
                    monitor_args.append(child['context'])
                monitor_options = {}
                if child.get('monitor_role') is not None:
                    monitor_options['job_role'] = child['monitor_role']
                self.monitor(*monitor_args, **monitor_options)

        try:
            # Never chdir the multi-threaded chief. Slow/failing shared-storage
            # operations also happen without its accounting/monitor lock held.
            logfile_path = os.path.abspath(os.path.join(
                cwd, 'zslurm_logs', f'zslurm-{name}-{jobid}.out'))
            os.makedirs(os.path.dirname(logfile_path), exist_ok=True)
            logfile = open(logfile_path, 'w')
            args = shlex.split(command)
            env = original_env
            if isinstance(env, dict):
                env = zslurm_gpu.child_environment(env, os.environ)
            else:
                env = zslurm_gpu.child_environment({}, os.environ)
            # This is a one-job launch option, not an inheritable environment
            # default. A controller's submitted compute children must retain
            # ordinary stuck detection, as must jobs inheriting chief env.
            monitor_role = original_env.get(MONITOR_ROLE_ENV) if isinstance(original_env, dict) else None
            stuck_detection_enabled({}, monitor_role)  # validate before spawn
            env.pop(MONITOR_ROLE_ENV, None)
            # The submitted environment was captured on the controller. Never
            # pass its temporary or scratch directory to a worker allocation.
            for name in (
                'ZSLURM_SCRATCH_ROOT', 'ZSLURM_SCRATCH_DIR',
                'TMPDIR', 'TMP', 'TEMP', 'TEMPDIR',
            ):
                env.pop(name, None)
            if self.prepare_child_environment is not None:
                child_context = self.prepare_child_environment(env, jobid)

            with self.status.lock:
                if self.pending.get(jobid) is not entry:
                    return  # Cancelled while opening the log; never resurrect it.
                lease_env = self.leases.prepare_job(jobid, cores, mem)
                prepared = True
                lease_env[zslurm_lease.ENV_SOCKET] = self.socket_path
                env.update(lease_env)
                # Start the monitor thread BEFORE spawning. A thread creation
                # failure must never leave an unmonitored child or spawn twice.
                monitor = threading.Thread(target=monitor_when_spawned)
                monitor.start()
                process = subprocess.Popen(
                    args, stdout=logfile, stderr=logfile, cwd=cwd, env=env,
                    preexec_fn=os.setpgrp,
                )
                child.update(
                    process=process, logfile=logfile,
                    logfile_path=logfile_path, context=child_context,
                    monitor_role=monitor_role,
                )
                logfile = None  # The monitor now owns/cleans the open handle.
                child_context = None  # The monitor now owns/cleans the context.
                prepared = False  # The live child now owns its lease.
                self.status.running_processes[jobid] = process
                self.pending.pop(jobid)
            self.log(f'{jobid} - START; INFO: {cores} {mem} {command} {logfile_path}')
        except Exception as error:
            if prepared:
                self.leases.discard_prepared_job(jobid)
            transient = (
                isinstance(error, OSError) and error.errno in TRANSIENT_START_ERRNOS
            ) or (isinstance(error, RuntimeError) and "can't start new thread" in str(error))
            with self.status.lock:
                if self.pending.get(jobid) is not entry:
                    if child.get('process') is not None:
                        raise  # Never retry AFTER successful spawn.
                    return  # Cancellation won the race with failed log creation.
                if transient:
                    entry['failures'] += 1
                    delay = min(self.retry_max_seconds,
                                self.retry_seconds * 2 ** min(entry['failures'] - 1, 10))
                    entry['retry_at'] = self.clock() + delay
                else:
                    self.cancel(jobid, return_code=-20)
            if transient:
                self.log(f'{jobid} - START DEFERRED; retry in {delay:g}s: {error}; '
                         'reservation retained, workflow retry not consumed')
            else:
                self.log(f'{jobid} - START FAILED: {error}')
        finally:
            started.set()  # Failed spawn: the waiting monitor exits without reporting.
            if child_context is not None and self.cleanup_child_context is not None:
                try:
                    self.cleanup_child_context(child_context)
                except Exception as error:
                    self.log(
                        f'{jobid} - child-context cleanup failed after '
                        f'unsuccessful start: {error}'
                    )
            if logfile is not None:
                try:
                    logfile.close()
                except OSError:
                    pass
