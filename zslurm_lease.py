"""Local dynamic resource leases for jobs running under a zslurm chief.

The Slurm allocation owned by the chief never changes.  A lease changes only
how much of that allocation zslurm reserves for one child job.  Jobs address
the local chief through a Unix-domain socket and authenticate with a
per-job capability token inherited in their environment.

Lease targets are absolute rather than deltas.  This is important: if a
manager RPC succeeds but its reply is lost, retrying the same target is
idempotent and cannot release or acquire the resources twice.
"""

import hmac
import json
import math
import os
import secrets
import socket
import socketserver
import threading
import time
import uuid


PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024

ENV_SOCKET = "ZSLURM_LEASE_SOCKET"
ENV_TOKEN = "ZSLURM_LEASE_TOKEN"
ENV_JOB_ID = "ZSLURM_JOB_ID"
ENV_MAX_CORES = "ZSLURM_LEASE_MAX_CORES"
ENV_MAX_MEM_MB = "ZSLURM_LEASE_MAX_MEM_MB"


def _finite_float(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number")
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
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
            self.leases[key] = {
                "job_id": key,
                "token": token,
                "max_cpu": max_cpu,
                "max_mem_mb": max_mem_mb,
                "held_cpu": max_cpu,
                "held_mem_mb": max_mem_mb,
                "epoch": 0,
                "created_at": time.time(),
            }
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
            if record is None:
                held_cpu = max(0.0, _finite_float(fallback_cpu, "fallback_cpu"))
                held_mem_mb = max(
                    0.0, _finite_float(fallback_mem_mb, "fallback_mem_mb")
                )
            else:
                held_cpu = record["held_cpu"]
                held_mem_mb = record["held_mem_mb"]
            self.status.current_cpu = min(
                self.total_cpu, self.status.current_cpu + held_cpu
            )
            self.status.current_mem = min(
                self.total_mem_mb, self.status.current_mem + held_mem_mb
            )
            self.condition.notify_all()
            self._wake()
            return {
                "held_cpu": held_cpu,
                "held_mem_mb": held_mem_mb,
                "dynamic": record is not None,
            }

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
    ):
        """Set an absolute lease target, waiting for growth when necessary."""

        key = self._key(job_id)
        request_id = str(request_id or uuid.uuid4())
        try:
            timeout_s = max(0.0, _finite_float(timeout_s, "timeout_s"))
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
                        record["updated_at"] = time.time()
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
            )
        return {
            "ok": False,
            "code": 2,
            "status": "invalid",
            "message": f"unknown action {action!r}",
        }


class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


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


def send_request(socket_path, request, timeout_s=30.0):
    timeout_s = max(0.1, _finite_float(timeout_s, "timeout_s"))
    payload = json.dumps(request, sort_keys=True).encode("utf-8") + b"\n"
    if len(payload) > MAX_REQUEST_BYTES:
        raise ValueError("request is too large")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(timeout_s)
        client.connect(socket_path)
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
