"""Local dynamic resource leases for jobs running under a zslurm chief.

The Slurm allocation owned by the chief never changes.  A lease changes only
how much of that allocation zslurm reserves for one child job.  Jobs address
the local chief through a Unix-domain socket and authenticate with a
per-job capability token inherited in their environment.

Lease ``set`` targets are absolute rather than deltas.  Independent parallel
consumers can instead use an atomic relative ``release`` identified by a
stable release id.  Repeating the same release id is idempotent, including
when a manager RPC succeeded but its reply was lost.
"""

import errno
import hmac
import json
import math
import os
import random
import secrets
import socket
import socketserver
import threading
import time
import uuid


PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024
MAX_PHASE_NAME_BYTES = 128
PHASE_PERCENTILES = (0, 5, 25, 50, 75, 95, 100)
PHASE_SAMPLE_RESERVOIR_SIZE = 512
LEASE_SERVER_REQUEST_QUEUE_SIZE = 128
LEASE_CONNECT_ATTEMPTS = 6
LEASE_CONNECT_TIMEOUT_SLICE_S = 0.5
LEASE_CONNECT_INITIAL_BACKOFF_S = 0.05

ENV_SOCKET = "ZSLURM_LEASE_SOCKET"
ENV_TOKEN = "ZSLURM_LEASE_TOKEN"
ENV_JOB_ID = "ZSLURM_JOB_ID"
ENV_MAX_CORES = "ZSLURM_LEASE_MAX_CORES"
ENV_MAX_MEM_MB = "ZSLURM_LEASE_MAX_MEM_MB"


def claim_finished_attempt(running_processes, job_id, process):
    """Claim cleanup for the currently registered process attempt.

    ZSlurm retries reuse the same job id. Process identity, rather than job id,
    therefore distinguishes a legitimate later completion from a duplicate or
    delayed monitor for an older attempt. The caller must hold the chief status
    lock while calling this helper.
    """

    if running_processes.get(job_id) is not process:
        return False
    running_processes.pop(job_id, None)
    return True


def _finite_float(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number")
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _phase_name(value, *, required=False):
    if value is None:
        if required:
            raise ValueError("phase name must not be empty")
        return None
    result = str(value).strip()
    if not result:
        raise ValueError("phase name must not be empty")
    if len(result.encode("utf-8")) > MAX_PHASE_NAME_BYTES:
        raise ValueError(
            f"phase name must not exceed {MAX_PHASE_NAME_BYTES} UTF-8 bytes"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in result):
        raise ValueError("phase name must not contain control characters")
    return result


def _percentiles(values):
    """Return linearly interpolated percentiles without a NumPy dependency."""

    ordered = sorted(float(value) for value in values)
    if not ordered:
        return []
    if len(ordered) == 1:
        return [ordered[0] for _ in PHASE_PERCENTILES]
    result = []
    last = len(ordered) - 1
    for percentile in PHASE_PERCENTILES:
        position = last * (float(percentile) / 100.0)
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        if lower == upper:
            result.append(ordered[lower])
        else:
            fraction = position - lower
            result.append(
                ordered[lower]
                + (ordered[upper] - ordered[lower]) * fraction
            )
    return result


class ChiefLeaseController:
    """Own mutable lease state and update a chief's free-resource counters.

    ``status`` must expose an ``RLock`` named ``lock`` and the mutable fields
    ``current_cpu``, ``current_mem``, and ``current_mem_usage``.

    ``manager_resize(job_id, cores, mem_mb)`` must atomically set the
    corresponding manager-side running-job reservation and return a dict with
    an ``ok`` boolean.  It is intentionally called while ``status.lock`` is
    held so the chief cannot start another job between its capacity check and
    the manager update.
    """

    def __init__(
        self,
        status,
        total_cpu,
        total_mem_mb,
        manager_resize,
        memory_available=None,
        wake_event=None,
        min_cpu=0.1,
        memory_headroom_fraction=0.2,
        memory_headroom_mb=512.0,
    ):
        self.status = status
        self.total_cpu = _finite_float(total_cpu, "total_cpu")
        self.total_mem_mb = _finite_float(total_mem_mb, "total_mem_mb")
        self.manager_resize = manager_resize
        self.memory_available = memory_available
        self.wake_event = wake_event
        self.min_cpu = max(0.0, _finite_float(min_cpu, "min_cpu"))
        self.memory_headroom_fraction = max(
            0.0,
            _finite_float(memory_headroom_fraction, "memory_headroom_fraction"),
        )
        self.memory_headroom_mb = max(
            0.0, _finite_float(memory_headroom_mb, "memory_headroom_mb")
        )
        self.condition = threading.Condition(status.lock)
        self.leases = {}
        self.waiters = []

    @staticmethod
    def _key(job_id):
        return str(job_id)

    @staticmethod
    def _new_phase_locked(
        record,
        *,
        now,
        name,
        event,
        transition_id=None,
        requested_cpu=None,
        requested_mem_mb=None,
    ):
        phase = {
            "phase_index": len(record["phases"]),
            "phase_name": name,
            "event": event,
            "transition_id": "" if transition_id is None else str(transition_id),
            "lease_epoch": int(record["epoch"]),
            "started_at_epoch": float(now),
            "held_cores": float(record["held_cpu"]),
            "held_mem_mb": float(record["held_mem_mb"]),
            "requested_cores": (
                float(record["held_cpu"])
                if requested_cpu is None
                else float(requested_cpu)
            ),
            "requested_mem_mb": (
                float(record["held_mem_mb"])
                if requested_mem_mb is None
                else float(requested_mem_mb)
            ),
            "sampled_duration_s": 0.0,
            "used_core_seconds": 0.0,
            "pss_mb_seconds": 0.0,
            "cpu_samples": [],
            "pss_mb_samples": [],
            "samples_seen": 0,
        }
        record["phases"].append(phase)
        record["phase_name"] = name
        # A sample from the previous phase must not be extrapolated across a
        # semantic or resource boundary. The first fresh sample backfills only
        # the new phase's own interval.
        record["last_usage_sample"] = None
        return phase

    @staticmethod
    def _account_usage_until_locked(record, now):
        if not record.get("phases"):
            return
        sample = record.get("last_usage_sample")
        if sample is None:
            return
        phase = record["phases"][-1]
        start = max(
            float(sample["sampled_at_epoch"]),
            float(phase["started_at_epoch"]),
        )
        duration = max(0.0, float(now) - start)
        if duration > 0.0:
            phase["sampled_duration_s"] += duration
            phase["used_core_seconds"] += float(sample["cpu_cores"]) * duration
            phase["pss_mb_seconds"] += float(sample["pss_mb"]) * duration
        sample["sampled_at_epoch"] = float(now)

    @staticmethod
    def _add_sample_locked(phase, cpu_cores, pss_mb):
        """Keep bounded paired reservoirs for phase quantiles."""

        phase["samples_seen"] += 1
        seen = int(phase["samples_seen"])
        if len(phase["cpu_samples"]) < PHASE_SAMPLE_RESERVOIR_SIZE:
            phase["cpu_samples"].append(cpu_cores)
            phase["pss_mb_samples"].append(pss_mb)
            return
        replace = random.randrange(seen)
        if replace < PHASE_SAMPLE_RESERVOIR_SIZE:
            phase["cpu_samples"][replace] = cpu_cores
            phase["pss_mb_samples"][replace] = pss_mb

    @classmethod
    def _close_phase_locked(cls, record, now):
        if not record.get("phases"):
            return
        cls._account_usage_until_locked(record, now)
        phase = record["phases"][-1]
        phase["ended_at_epoch"] = max(
            float(now), float(phase["started_at_epoch"])
        )

    @classmethod
    def _transition_phase_locked(
        cls,
        record,
        *,
        now,
        name,
        event,
        transition_id=None,
        requested_cpu=None,
        requested_mem_mb=None,
    ):
        cls._close_phase_locked(record, now)
        return cls._new_phase_locked(
            record,
            now=now,
            name=name,
            event=event,
            transition_id=transition_id,
            requested_cpu=requested_cpu,
            requested_mem_mb=requested_mem_mb,
        )

    @staticmethod
    def _serialise_phase(phase):
        started = float(phase["started_at_epoch"])
        ended = float(phase.get("ended_at_epoch", started))
        duration = max(0.0, ended - started)
        sampled = max(0.0, float(phase["sampled_duration_s"]))
        held_cpu = float(phase["held_cores"])
        held_mem_mb = float(phase["held_mem_mb"])
        cpu_samples = phase["cpu_samples"]
        pss_samples = phase["pss_mb_samples"]
        avg_cpu = (
            float(phase["used_core_seconds"]) / sampled if sampled > 0.0 else None
        )
        avg_pss_mb = (
            float(phase["pss_mb_seconds"]) / sampled if sampled > 0.0 else None
        )
        used_core_seconds = (
            float(phase["used_core_seconds"]) if sampled > 0.0 else None
        )
        pss_mb_seconds = (
            float(phase["pss_mb_seconds"]) if sampled > 0.0 else None
        )
        sampled_reserved_core_seconds = held_cpu * sampled
        sampled_reserved_mem_mb_seconds = held_mem_mb * sampled
        return {
            "phase_index": int(phase["phase_index"]),
            "phase_name": phase["phase_name"],
            "event": phase["event"],
            "transition_id": phase["transition_id"],
            "lease_epoch": int(phase["lease_epoch"]),
            "started_at_epoch": started,
            "ended_at_epoch": ended,
            "duration_s": duration,
            "held_cores": held_cpu,
            "held_mem_mb": held_mem_mb,
            "requested_cores": float(phase["requested_cores"]),
            "requested_mem_mb": float(phase["requested_mem_mb"]),
            "sample_count": int(phase["samples_seen"]),
            "sampled_duration_s": sampled,
            "avg_cpu_cores": avg_cpu,
            "avg_pss_mb": avg_pss_mb,
            "cpu_cores_percentiles": _percentiles(cpu_samples),
            "pss_mb_percentiles": _percentiles(pss_samples),
            "reserved_core_seconds": held_cpu * duration,
            "sampled_reserved_core_seconds": sampled_reserved_core_seconds,
            "used_core_seconds": used_core_seconds,
            "reserved_mem_mb_seconds": held_mem_mb * duration,
            "sampled_reserved_mem_mb_seconds": sampled_reserved_mem_mb_seconds,
            "pss_mb_seconds": pss_mb_seconds,
            "cpu_efficiency": (
                used_core_seconds / sampled_reserved_core_seconds
                if sampled_reserved_core_seconds > 0.0
                else None
            ),
            "memory_efficiency": (
                pss_mb_seconds / sampled_reserved_mem_mb_seconds
                if sampled_reserved_mem_mb_seconds > 0.0
                else None
            ),
            "sample_coverage": sampled / duration if duration > 0.0 else 0.0,
        }

    @staticmethod
    def _phase_summary(phases):
        reserved_fields = (
            "reserved_core_seconds",
            "sampled_reserved_core_seconds",
            "reserved_mem_mb_seconds",
            "sampled_reserved_mem_mb_seconds",
        )
        result = {
            field: sum(float(p[field]) for p in phases)
            for field in reserved_fields
        }
        observed = [p for p in phases if float(p["sampled_duration_s"]) > 0.0]
        result["used_core_seconds"] = (
            sum(float(p["used_core_seconds"]) for p in observed)
            if observed else None
        )
        result["pss_mb_seconds"] = (
            sum(float(p["pss_mb_seconds"]) for p in observed)
            if observed else None
        )
        result["phase_count"] = len(phases)
        result["duration_s"] = sum(float(p["duration_s"]) for p in phases)
        result["sampled_duration_s"] = sum(
            float(p["sampled_duration_s"]) for p in phases
        )
        result["sample_count"] = sum(int(p["sample_count"]) for p in phases)
        result["sample_coverage"] = (
            result["sampled_duration_s"] / result["duration_s"]
            if result["duration_s"] > 0.0 else 0.0
        )
        reserved_cpu = result["sampled_reserved_core_seconds"]
        reserved_mem = result["sampled_reserved_mem_mb_seconds"]
        result["cpu_efficiency"] = (
            result["used_core_seconds"] / reserved_cpu
            if reserved_cpu > 0.0 and result["used_core_seconds"] is not None
            else None
        )
        result["memory_efficiency"] = (
            result["pss_mb_seconds"] / reserved_mem
            if reserved_mem > 0.0 and result["pss_mb_seconds"] is not None
            else None
        )
        return result

    def prepare_job(self, job_id, max_cpu, max_mem_mb):
        """Create a full-size lease and return environment variables for the job.

        The caller must subtract the initial maximum from the chief's free
        counters after successfully spawning the child.  Preparing first lets
        the token be included in the child's environment; the shared RLock
        prevents the child from changing the lease before spawn accounting is
        complete.
        """

        key = self._key(job_id)
        max_cpu = max(0.0, _finite_float(max_cpu, "max_cpu"))
        max_mem_mb = max(0.0, _finite_float(max_mem_mb, "max_mem_mb"))
        with self.condition:
            if key in self.leases:
                raise RuntimeError(f"lease already exists for job {key}")
            token = secrets.token_urlsafe(32)
            now = time.time()
            record = {
                "job_id": key,
                "token": token,
                "max_cpu": max_cpu,
                "max_mem_mb": max_mem_mb,
                "held_cpu": max_cpu,
                "held_mem_mb": max_mem_mb,
                "epoch": 0,
                "created_at": now,
                "completed_releases": {},
                "phases": [],
                "phase_name": "initial",
                "last_usage_sample": None,
            }
            self.leases[key] = record
            self._new_phase_locked(
                record,
                now=now,
                name="initial",
                event="initial",
            )
            return {
                ENV_TOKEN: token,
                ENV_JOB_ID: key,
                ENV_MAX_CORES: str(max_cpu),
                ENV_MAX_MEM_MB: str(max_mem_mb),
            }

    def discard_prepared_job(self, job_id):
        """Remove a lease for a child that failed to spawn.

        No resources are returned here because the caller has not yet
        subtracted the initial maximum from the chief.
        """

        key = self._key(job_id)
        with self.condition:
            self.leases.pop(key, None)
            self.waiters = [item for item in self.waiters if item["job_id"] != key]
            self.condition.notify_all()

    def finish_job(self, job_id, fallback_cpu, fallback_mem_mb):
        """Return the job's *current* holding to the chief and remove its lease."""

        key = self._key(job_id)
        with self.condition:
            record = self.leases.pop(key, None)
            self.waiters = [item for item in self.waiters if item["job_id"] != key]
            phases = []
            summary = {}
            if record is None:
                held_cpu = max(0.0, _finite_float(fallback_cpu, "fallback_cpu"))
                held_mem_mb = max(
                    0.0, _finite_float(fallback_mem_mb, "fallback_mem_mb")
                )
            else:
                held_cpu = record["held_cpu"]
                held_mem_mb = record["held_mem_mb"]
                self._close_phase_locked(record, time.time())
                phases = [self._serialise_phase(p) for p in record["phases"]]
                summary = self._phase_summary(phases)
            self.status.current_cpu = min(
                self.total_cpu, self.status.current_cpu + held_cpu
            )
            self.status.current_mem = min(
                self.total_mem_mb, self.status.current_mem + held_mem_mb
            )
            self.condition.notify_all()
            self._wake()
            result = {
                "held_cpu": held_cpu,
                "held_mem_mb": held_mem_mb,
                "dynamic": record is not None,
                "phases": phases,
            }
            result.update(summary)
            return result

    def record_usage(self, job_id, cpu_cores, pss_mb, sampled_at=None):
        """Attach one chief process-tree sample to the current lease phase."""

        key = self._key(job_id)
        try:
            cpu_cores = max(0.0, _finite_float(cpu_cores, "cpu_cores"))
            pss_mb = max(0.0, _finite_float(pss_mb, "pss_mb"))
            now = time.time() if sampled_at is None else _finite_float(
                sampled_at, "sampled_at"
            )
        except ValueError:
            return False
        with self.condition:
            record = self.leases.get(key)
            if record is None or not record.get("phases"):
                return False
            phase = record["phases"][-1]
            previous = record.get("last_usage_sample")
            if previous is None:
                duration = max(0.0, now - float(phase["started_at_epoch"]))
                phase["sampled_duration_s"] += duration
                phase["used_core_seconds"] += cpu_cores * duration
                phase["pss_mb_seconds"] += pss_mb * duration
            else:
                self._account_usage_until_locked(record, now)
            self._add_sample_locked(phase, cpu_cores, pss_mb)
            record["last_usage_sample"] = {
                "sampled_at_epoch": now,
                "cpu_cores": cpu_cores,
                "pss_mb": pss_mb,
            }
            return True

    def mark_phase(self, job_id, token, phase):
        """Start a named semantic phase without changing held resources."""

        key = self._key(job_id)
        try:
            phase = _phase_name(phase, required=True)
        except ValueError as exc:
            return {
                "ok": False,
                "code": 2,
                "status": "invalid",
                "message": str(exc),
            }
        with self.condition:
            record = self._authenticate_locked(key, token)
            if record is None:
                return {
                    "ok": False,
                    "code": 3,
                    "status": "denied",
                    "message": "unknown job or invalid lease token",
                }
            if phase == record.get("phase_name"):
                return self._response_locked(record, status="unchanged")
            self._transition_phase_locked(
                record,
                now=time.time(),
                name=phase,
                event="phase",
            )
            self.condition.notify_all()
            return self._response_locked(record, status="phase-changed")

    def notify_capacity_change(self):
        with self.condition:
            self.condition.notify_all()
        self._wake()

    def has_pending_acquire(self):
        with self.condition:
            return bool(self.waiters)

    def pending_summary(self):
        with self.condition:
            if not self.waiters:
                return {"count": 0, "job_id": None}
            first = self.waiters[0]
            return {
                "count": len(self.waiters),
                "job_id": first["job_id"],
                "requested_cpu": first["target_cpu"],
                "requested_mem_mb": first["target_mem_mb"],
            }

    def _wake(self):
        if self.wake_event is not None:
            try:
                self.wake_event.set()
            except Exception:
                pass

    def _authenticate_locked(self, job_id, token):
        record = self.leases.get(self._key(job_id))
        if record is None:
            return None
        supplied = "" if token is None else str(token)
        if not hmac.compare_digest(record["token"], supplied):
            return None
        return record

    def _safe_target_locked(self, record, requested_cpu, requested_mem_mb):
        old_cpu = record["held_cpu"]
        old_mem_mb = record["held_mem_mb"]
        target_cpu = (
            old_cpu
            if requested_cpu is None
            else _finite_float(requested_cpu, "cores")
        )
        target_mem_mb = (
            old_mem_mb
            if requested_mem_mb is None
            else _finite_float(requested_mem_mb, "mem_mb")
        )
        if target_cpu < 0.0 or target_mem_mb < 0.0:
            raise ValueError("lease targets cannot be negative")
        if target_cpu > record["max_cpu"] + 1e-9:
            raise ValueError(
                f"cores target {target_cpu} exceeds job maximum {record['max_cpu']}"
            )
        if target_mem_mb > record["max_mem_mb"] + 1e-9:
            raise ValueError(
                "mem_mb target "
                f"{target_mem_mb} exceeds job maximum {record['max_mem_mb']}"
            )

        cpu_floor = min(record["max_cpu"], self.min_cpu)
        observed_mem_mb = max(
            0.0,
            float(
                getattr(self.status, "current_mem_usage", {}).get(
                    record["job_id"], 0.0
                )
                or 0.0
            ),
        )
        memory_floor = min(
            record["max_mem_mb"],
            observed_mem_mb * (1.0 + self.memory_headroom_fraction)
            + self.memory_headroom_mb,
        )
        effective_cpu = max(target_cpu, cpu_floor)
        effective_mem_mb = max(target_mem_mb, memory_floor)
        return effective_cpu, effective_mem_mb, {
            "cpu_floor": cpu_floor,
            "memory_floor_mb": memory_floor,
            "observed_memory_mb": observed_mem_mb,
            "adjusted": (
                abs(effective_cpu - target_cpu) > 1e-9
                or abs(effective_mem_mb - target_mem_mb) > 1e-9
            ),
        }

    def _actual_memory_available_locked(self):
        if self.memory_available is None:
            return float("inf")
        try:
            return max(0.0, float(self.memory_available()))
        except Exception:
            return 0.0

    def status_for(self, job_id, token):
        with self.condition:
            record = self._authenticate_locked(job_id, token)
            if record is None:
                return {
                    "ok": False,
                    "code": 3,
                    "status": "denied",
                    "message": "unknown job or invalid lease token",
                }
            return self._response_locked(record, status="current")

    def set_target(
        self,
        job_id,
        token,
        cores=None,
        mem_mb=None,
        timeout_s=3600.0,
        request_id=None,
        phase=None,
    ):
        """Set an absolute lease target, waiting for growth when necessary."""

        key = self._key(job_id)
        request_id = str(request_id or uuid.uuid4())
        try:
            timeout_s = max(0.0, _finite_float(timeout_s, "timeout_s"))
            phase = _phase_name(phase)
        except ValueError as exc:
            return {
                "ok": False,
                "code": 2,
                "status": "invalid",
                "message": str(exc),
            }
        deadline = time.monotonic() + timeout_s
        waiter = None

        with self.condition:
            record = self._authenticate_locked(key, token)
            if record is None:
                return {
                    "ok": False,
                    "code": 3,
                    "status": "denied",
                    "message": "unknown job or invalid lease token",
                }
            try:
                target_cpu, target_mem_mb, safety = self._safe_target_locked(
                    record, cores, mem_mb
                )
            except ValueError as exc:
                return {
                    "ok": False,
                    "code": 2,
                    "status": "invalid",
                    "message": str(exc),
                }

            needs_growth = (
                target_cpu > record["held_cpu"] + 1e-9
                or target_mem_mb > record["held_mem_mb"] + 1e-9
            )
            if needs_growth:
                waiter = {
                    "request_id": request_id,
                    "job_id": key,
                    "target_cpu": target_cpu,
                    "target_mem_mb": target_mem_mb,
                    "created_at": time.time(),
                }
                self.waiters.append(waiter)
                self._wake()

            try:
                while True:
                    record = self._authenticate_locked(key, token)
                    if record is None:
                        return {
                            "ok": False,
                            "code": 4,
                            "status": "gone",
                            "message": "job ended while waiting for its lease",
                        }

                    # Re-evaluate the observed-memory floor while waiting.
                    try:
                        target_cpu, target_mem_mb, safety = self._safe_target_locked(
                            record, cores, mem_mb
                        )
                    except ValueError as exc:
                        return {
                            "ok": False,
                            "code": 2,
                            "status": "invalid",
                            "message": str(exc),
                        }
                    if waiter is not None:
                        waiter["target_cpu"] = target_cpu
                        waiter["target_mem_mb"] = target_mem_mb

                    if (
                        abs(target_cpu - record["held_cpu"]) <= 1e-9
                        and abs(target_mem_mb - record["held_mem_mb"]) <= 1e-9
                    ):
                        if phase is not None and phase != record.get("phase_name"):
                            self._transition_phase_locked(
                                record,
                                now=time.time(),
                                name=phase,
                                event="phase",
                                transition_id=request_id,
                                requested_cpu=cores,
                                requested_mem_mb=mem_mb,
                            )
                            self.condition.notify_all()
                            self._wake()
                            return self._response_locked(
                                record,
                                status="phase-changed",
                                safety=safety,
                                request_id=request_id,
                            )
                        return self._response_locked(
                            record,
                            status="unchanged",
                            safety=safety,
                            request_id=request_id,
                        )

                    cpu_delta = max(0.0, target_cpu - record["held_cpu"])
                    mem_delta = max(0.0, target_mem_mb - record["held_mem_mb"])
                    at_front = waiter is None or (
                        self.waiters and self.waiters[0] is waiter
                    )
                    capacity_ready = (
                        self.status.current_cpu + 1e-9 >= cpu_delta
                        and self.status.current_mem + 1e-9 >= mem_delta
                        and self._actual_memory_available_locked() + 1e-9
                        >= mem_delta
                    )

                    if at_front and capacity_ready:
                        try:
                            manager_result = self.manager_resize(
                                key, target_cpu, target_mem_mb
                            )
                        except Exception as exc:
                            return {
                                "ok": False,
                                "code": 5,
                                "status": "manager-error",
                                "message": f"manager resize failed: {exc}",
                            }
                        if not isinstance(manager_result, dict) or not manager_result.get(
                            "ok", False
                        ):
                            message = (
                                manager_result.get("message", "manager rejected resize")
                                if isinstance(manager_result, dict)
                                else "manager returned an invalid resize response"
                            )
                            return {
                                "ok": False,
                                "code": 5,
                                "status": "manager-rejected",
                                "message": message,
                            }

                        old_cpu = record["held_cpu"]
                        old_mem_mb = record["held_mem_mb"]
                        self.status.current_cpu = min(
                            self.total_cpu,
                            max(
                                0.0,
                                self.status.current_cpu + old_cpu - target_cpu,
                            ),
                        )
                        self.status.current_mem = min(
                            self.total_mem_mb,
                            max(
                                0.0,
                                self.status.current_mem + old_mem_mb - target_mem_mb,
                            ),
                        )
                        record["held_cpu"] = target_cpu
                        record["held_mem_mb"] = target_mem_mb
                        record["epoch"] = int(
                            manager_result.get("epoch", record["epoch"] + 1)
                        )
                        changed_at = time.time()
                        record["updated_at"] = changed_at
                        self._transition_phase_locked(
                            record,
                            now=changed_at,
                            name=phase or f"lease-{record['epoch']}",
                            event="set",
                            transition_id=request_id,
                            requested_cpu=cores,
                            requested_mem_mb=mem_mb,
                        )
                        self.condition.notify_all()
                        self._wake()
                        return self._response_locked(
                            record,
                            status="granted",
                            safety=safety,
                            request_id=request_id,
                        )

                    remaining = deadline - time.monotonic()
                    if remaining <= 0.0:
                        return {
                            "ok": False,
                            "code": 4,
                            "status": "timeout",
                            "message": (
                                "timed out waiting for "
                                f"{target_cpu} cores and {target_mem_mb} MB"
                            ),
                        }
                    self.condition.wait(timeout=min(1.0, remaining))
            finally:
                if waiter is not None and waiter in self.waiters:
                    self.waiters.remove(waiter)
                    self.condition.notify_all()
                    self._wake()

    def release_resources(
        self,
        job_id,
        token,
        cores=0.0,
        mem_mb=0.0,
        release_id=None,
        request_id=None,
    ):
        """Atomically release CPU/memory relative to the current holding.

        ``release_id`` identifies one logical consumer completion. Reusing it
        with the same amounts returns the original response without applying
        the decrement twice. Reusing it with different amounts is rejected.
        """

        key = self._key(job_id)
        request_id = str(request_id or uuid.uuid4())
        release_id = str(release_id or request_id)
        if not release_id:
            return {
                "ok": False,
                "code": 2,
                "status": "invalid",
                "message": "release_id must not be empty",
            }
        try:
            release_cpu = _finite_float(cores, "cores")
            release_mem_mb = _finite_float(mem_mb, "mem_mb")
        except ValueError as exc:
            return {
                "ok": False,
                "code": 2,
                "status": "invalid",
                "message": str(exc),
            }
        if release_cpu < 0.0 or release_mem_mb < 0.0:
            return {
                "ok": False,
                "code": 2,
                "status": "invalid",
                "message": "relative release amounts cannot be negative",
            }
        if release_cpu <= 0.0 and release_mem_mb <= 0.0:
            return {
                "ok": False,
                "code": 2,
                "status": "invalid",
                "message": "release requires a positive core or memory amount",
            }

        with self.condition:
            record = self._authenticate_locked(key, token)
            if record is None:
                return {
                    "ok": False,
                    "code": 3,
                    "status": "denied",
                    "message": "unknown job or invalid lease token",
                }

            completed = record.setdefault("completed_releases", {})
            previous = completed.get(release_id)
            if previous is not None:
                if (
                    abs(previous["cores"] - release_cpu) > 1e-9
                    or abs(previous["mem_mb"] - release_mem_mb) > 1e-9
                ):
                    return {
                        "ok": False,
                        "code": 2,
                        "status": "invalid",
                        "message": (
                            f"release_id {release_id!r} was already used with "
                            "different resource amounts"
                        ),
                    }
                response = dict(previous["response"])
                response.update(
                    {
                        "status": "already-released",
                        "duplicate": True,
                        "request_id": request_id,
                    }
                )
                return response

            old_cpu = record["held_cpu"]
            old_mem_mb = record["held_mem_mb"]
            requested_target_cpu = max(0.0, old_cpu - release_cpu)
            requested_target_mem_mb = max(0.0, old_mem_mb - release_mem_mb)
            try:
                target_cpu, target_mem_mb, safety = self._safe_target_locked(
                    record, requested_target_cpu, requested_target_mem_mb
                )
            except ValueError as exc:
                return {
                    "ok": False,
                    "code": 2,
                    "status": "invalid",
                    "message": str(exc),
                }

            if (
                abs(target_cpu - old_cpu) <= 1e-9
                and abs(target_mem_mb - old_mem_mb) <= 1e-9
            ):
                response = self._response_locked(
                    record,
                    status="unchanged",
                    safety=safety,
                    request_id=request_id,
                )
                response.update(
                    {
                        "release_id": release_id,
                        "requested_release_cores": release_cpu,
                        "requested_release_mem_mb": release_mem_mb,
                        "released_cores": 0.0,
                        "released_mem_mb": 0.0,
                        "duplicate": False,
                    }
                )
                completed[release_id] = {
                    "cores": release_cpu,
                    "mem_mb": release_mem_mb,
                    "response": dict(response),
                }
                return response

            try:
                manager_result = self.manager_resize(key, target_cpu, target_mem_mb)
            except Exception as exc:
                return {
                    "ok": False,
                    "code": 5,
                    "status": "manager-error",
                    "message": f"manager resize failed: {exc}",
                }
            if not isinstance(manager_result, dict) or not manager_result.get(
                "ok", False
            ):
                message = (
                    manager_result.get("message", "manager rejected resize")
                    if isinstance(manager_result, dict)
                    else "manager returned an invalid resize response"
                )
                return {
                    "ok": False,
                    "code": 5,
                    "status": "manager-rejected",
                    "message": message,
                }

            self.status.current_cpu = min(
                self.total_cpu,
                max(0.0, self.status.current_cpu + old_cpu - target_cpu),
            )
            self.status.current_mem = min(
                self.total_mem_mb,
                max(0.0, self.status.current_mem + old_mem_mb - target_mem_mb),
            )
            record["held_cpu"] = target_cpu
            record["held_mem_mb"] = target_mem_mb
            record["epoch"] = int(
                manager_result.get("epoch", record["epoch"] + 1)
            )
            changed_at = time.time()
            record["updated_at"] = changed_at
            self._transition_phase_locked(
                record,
                now=changed_at,
                name=record.get("phase_name") or f"lease-{record['epoch']}",
                event="release",
                transition_id=release_id,
                requested_cpu=requested_target_cpu,
                requested_mem_mb=requested_target_mem_mb,
            )
            response = self._response_locked(
                record,
                status="released",
                safety=safety,
                request_id=request_id,
            )
            response.update(
                {
                    "release_id": release_id,
                    "requested_release_cores": release_cpu,
                    "requested_release_mem_mb": release_mem_mb,
                    "released_cores": old_cpu - target_cpu,
                    "released_mem_mb": old_mem_mb - target_mem_mb,
                    "duplicate": False,
                }
            )
            completed[release_id] = {
                "cores": release_cpu,
                "mem_mb": release_mem_mb,
                "response": dict(response),
            }
            self.condition.notify_all()
            self._wake()
            return response

    def _response_locked(
        self, record, status, safety=None, request_id=None
    ):
        result = {
            "ok": True,
            "code": 0,
            "status": status,
            "job_id": record["job_id"],
            "held_cores": record["held_cpu"],
            "held_mem_mb": record["held_mem_mb"],
            "max_cores": record["max_cpu"],
            "max_mem_mb": record["max_mem_mb"],
            "epoch": record["epoch"],
            "phase_index": len(record.get("phases", [])) - 1,
            "phase_name": record.get("phase_name", ""),
        }
        if safety is not None:
            result["safety"] = safety
        if request_id is not None:
            result["request_id"] = request_id
        return result

    def handle_request(self, request):
        if not isinstance(request, dict):
            return {
                "ok": False,
                "code": 2,
                "status": "invalid",
                "message": "request must be a JSON object",
            }
        if request.get("version", PROTOCOL_VERSION) != PROTOCOL_VERSION:
            return {
                "ok": False,
                "code": 2,
                "status": "invalid",
                "message": "unsupported lease protocol version",
            }
        action = str(request.get("action", "")).lower()
        job_id = request.get("job_id")
        token = request.get("token")
        if action == "status":
            return self.status_for(job_id, token)
        if action == "set":
            return self.set_target(
                job_id,
                token,
                cores=request.get("cores"),
                mem_mb=request.get("mem_mb"),
                timeout_s=request.get("timeout_s", 3600.0),
                request_id=request.get("request_id"),
                phase=request.get("phase"),
            )
        if action == "phase":
            return self.mark_phase(job_id, token, request.get("phase"))
        if action == "release":
            return self.release_resources(
                job_id,
                token,
                cores=request.get("cores", 0.0),
                mem_mb=request.get("mem_mb", 0.0),
                release_id=request.get("release_id"),
                request_id=request.get("request_id"),
            )
        return {
            "ok": False,
            "code": 2,
            "status": "invalid",
            "message": f"unknown action {action!r}",
        }


class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    # A new large engine can start many fused jobs at once.  The socketserver
    # default backlog is only five, which is too small for their simultaneous
    # lease preflight requests.
    request_queue_size = LEASE_SERVER_REQUEST_QUEUE_SIZE


class _LeaseRequestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            response = {
                "ok": False,
                "code": 2,
                "status": "invalid",
                "message": "request is too large",
            }
        else:
            try:
                request = json.loads(raw.decode("utf-8"))
                response = self.server.controller.handle_request(request)
            except Exception as exc:
                response = {
                    "ok": False,
                    "code": 2,
                    "status": "invalid",
                    "message": f"invalid request: {exc}",
                }
        payload = json.dumps(response, sort_keys=True).encode("utf-8") + b"\n"
        self.wfile.write(payload)


class LeaseServer:
    def __init__(self, socket_path, controller):
        self.socket_path = os.path.abspath(socket_path)
        self.controller = controller
        self.server = None
        self.thread = None
        self.created_parent = False

    def start(self):
        parent = os.path.dirname(self.socket_path)
        parent_existed = os.path.isdir(parent)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        self.created_parent = not parent_existed
        if not parent_existed:
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self.server = _ThreadingUnixServer(self.socket_path, _LeaseRequestHandler)
        self.server.controller = self.controller
        os.chmod(self.socket_path, 0o600)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="zslurm-lease-server",
            daemon=True,
        )
        self.thread.start()
        return self

    def close(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass


def _connect_client(socket_path, deadline):
    """Connect with bounded retries, but never replay a request after sending."""

    delay_s = LEASE_CONNECT_INITIAL_BACKOFF_S
    retryable_errnos = {
        errno.EAGAIN,
        errno.ECONNREFUSED,
        errno.ETIMEDOUT,
    }
    for attempt in range(LEASE_CONNECT_ATTEMPTS):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0.0:
            client.close()
            raise TimeoutError(f"timed out connecting to local lease socket {socket_path}")
        client.settimeout(min(remaining_s, LEASE_CONNECT_TIMEOUT_SLICE_S))
        try:
            client.connect(socket_path)
            client.settimeout(max(0.1, deadline - time.monotonic()))
            return client
        except OSError as exc:
            client.close()
            retryable = (
                isinstance(exc, (BlockingIOError, TimeoutError))
                or exc.errno in retryable_errnos
            )
            remaining_s = deadline - time.monotonic()
            if (
                not retryable
                or attempt + 1 >= LEASE_CONNECT_ATTEMPTS
                or remaining_s <= 0.0
            ):
                raise
            sleep_s = min(delay_s, remaining_s)
            if sleep_s > 0.0:
                time.sleep(sleep_s)
            delay_s *= 2.0

    raise RuntimeError("unreachable lease connection retry state")


def send_request(socket_path, request, timeout_s=30.0):
    timeout_s = max(0.1, _finite_float(timeout_s, "timeout_s"))
    payload = json.dumps(request, sort_keys=True).encode("utf-8") + b"\n"
    if len(payload) > MAX_REQUEST_BYTES:
        raise ValueError("request is too large")
    deadline = time.monotonic() + timeout_s
    client = _connect_client(socket_path, deadline)
    try:
        client.sendall(payload)
        chunks = []
        total = 0
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_REQUEST_BYTES:
                raise RuntimeError("lease response is too large")
            if b"\n" in chunk:
                break
        if not chunks:
            raise RuntimeError("local zslurm chief returned an empty response")
        return json.loads(b"".join(chunks).split(b"\n", 1)[0].decode("utf-8"))
    finally:
        client.close()


def request_from_environment(action, socket_timeout_s=30.0, **fields):
    socket_path = os.environ.get(ENV_SOCKET)
    token = os.environ.get(ENV_TOKEN)
    job_id = os.environ.get(ENV_JOB_ID)
    if not socket_path or not token or not job_id:
        raise RuntimeError(
            "this process has no zslurm lease environment; "
            f"expected {ENV_SOCKET}, {ENV_TOKEN}, and {ENV_JOB_ID}"
        )
    request = {
        "version": PROTOCOL_VERSION,
        "request_id": str(uuid.uuid4()),
        "action": action,
        "job_id": job_id,
        "token": token,
    }
    request.update(fields)
    return send_request(socket_path, request, timeout_s=socket_timeout_s)
