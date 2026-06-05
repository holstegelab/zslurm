"""zslurm_coord -- multi-agent coordination layer for offloading work to a *shared*
zslurm manager on Snellius.

Problem this solves
-------------------
Several Claude Code agents (and the human) may want to offload compute off the
login node at the same time. They must:

  * share ONE manager instead of each starting their own (two concurrent
    `zslurm --headless` starts collide -> `zslurm_int5` + `zslurm_int5_1` ->
    every client then errors "multiple instances");
  * never tear down a manager (which runs `engines.stop_all()` and kills every
    agent's engines + requeues their jobs) while another agent has work in
    flight or is mid-submit;
  * never stop a manager they did not start -- in particular the human's own
    manager ("you can just offload into it, and not stop it!").

Design (pure stdlib; no third-party deps so it imports anywhere)
----------------------------------------------------------------
State lives under ~/.zslurm/ (shared home, visible from every login/compute node):

  manager.lock        flock mutex, held only for the short start/stop/scale
                      critical sections (cluster-coherent on GPFS).
  leases/<id>.json    one per agent. A *lease* = "I have (or am about to submit)
                      work; keep the manager+engines alive until <expires>."
                      TTL-based so a crashed agent's lease self-expires.
  offloader.json      ownership marker: which instance THIS tooling provisioned,
                      how (login pid / slurm jobid), on which host. The reaper may
                      only ever stop a manager recorded here with autostarted=true.
  submits/<key>.json  idempotency: a retried submit with the same key returns the
                      original jobid instead of double-submitting.

Discovery is via the instance YAML files that zslurm itself writes; a manager is
"reachable" iff `zsstatus --instance <name>` returns health.ok. This works
regardless of whether the manager runs on the login node or a compute node.

The library is transport-agnostic: it drives the existing, hardened zs* clients
(zsstatus / zsbatch / zscontrol / zscancel / zslurm) as subprocesses and branches
on their documented exit codes (0 ok / 2 transport-retry / 3 logical-reject).
"""

import binascii
import contextlib
import errno
import fcntl
import json
import os
import shlex
import socket
import subprocess
import sys
import time

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
# Fixed at ~/.zslurm to MATCH zslurm_shared (which hardcodes it and ignores any
# env override). Honoring a $ZSLURM_HOME here but not there would make a node
# manager register its instance YAML where the coordinator never looks.
STATE_DIR   = os.path.expanduser("~/.zslurm")
INSTANCES   = os.path.join(STATE_DIR, "instances")
LEASE_DIR   = os.path.join(STATE_DIR, "leases")
SUBMIT_DIR  = os.path.join(STATE_DIR, "submits")
JOBSCRIPT_DIR = os.path.join(STATE_DIR, "jobscripts")
LOCK_PATH   = os.path.join(STATE_DIR, "manager.lock")
OWNER_PATH  = os.path.join(STATE_DIR, "offloader.json")
TOKEN_PATH  = os.path.join(STATE_DIR, "control.token")
LOG_PATH    = os.path.join(STATE_DIR, "offloader.log")

# Directory of the python running us -> the conda env's bin/ that holds the zs*
# clients and the zslurm manager. Lets the CLIs work without `conda activate`.
BIN_DIR     = os.path.dirname(os.path.abspath(sys.executable))


def _ensure_dirs():
    for d in (STATE_DIR, LEASE_DIR, SUBMIT_DIR):
        os.makedirs(d, mode=0o700, exist_ok=True)


def now():
    return time.time()


def hostname():
    return socket.gethostname().split(".")[0]


def log(msg):
    """Append a timestamped line to the offloader log (best effort)."""
    try:
        _ensure_dirs()
        with open(LOG_PATH, "a") as fh:
            fh.write("%s %s[%d] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                         hostname(), os.getpid(), msg))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Atomic JSON helpers
# --------------------------------------------------------------------------- #
def read_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json_atomic(path, obj):
    _ensure_dirs()
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# The lock (flock). Held ONLY for the short critical sections:
#   - ensuring/provisioning a manager (start)
#   - stopping a self-provisioned manager
#   - growing/shrinking/(re)configuring engines
# A holder that dies releases the lock automatically (kernel closes the fd),
# so a crashed agent can never wedge the cluster.
# --------------------------------------------------------------------------- #
class LockTimeout(Exception):
    pass


@contextlib.contextmanager
def manager_lock(timeout=60.0, poll=0.25):
    """Cluster-coherent advisory lock around manager lifecycle changes."""
    _ensure_dirs()
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = now() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as ex:
                if ex.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if now() >= deadline:
                    raise LockTimeout(
                        "could not acquire %s within %.0fs (another agent is "
                        "starting/stopping the manager)" % (LOCK_PATH, timeout))
                time.sleep(poll)
        # Record holder for diagnostics (informational only).
        try:
            os.write(fd, b"")  # no-op; keep fd valid
        except OSError:
            pass
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


# --------------------------------------------------------------------------- #
# Leases -- "near-term intent": keep the shared manager alive while I have work
# --------------------------------------------------------------------------- #
def _lease_path(agent_id):
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in agent_id)
    return os.path.join(LEASE_DIR, "%s.json" % safe)


def write_lease(agent_id, instance, ttl, note="", jobids=None):
    """Create/refresh this agent's lease. `expires` extends to cover the longest
    pending job (the caller passes ttl = job_walltime + margin); existing jobids
    are merged so one agent can hold several jobs under one lease."""
    path = _lease_path(agent_id)
    cur = read_json(path, {}) or {}
    jobids = [str(j) for j in (jobids or [])]
    merged = sorted(set([str(j) for j in cur.get("jobids", [])] + jobids))
    expires = max(float(cur.get("expires", 0.0)), now() + float(ttl))
    lease = {
        "agent_id": agent_id,
        "host": hostname(),
        "pid": os.getpid(),
        "instance": instance,
        "created": float(cur.get("created", now())),
        "renewed": now(),
        "expires": expires,
        "note": note or cur.get("note", ""),
        "jobids": merged,
    }
    write_json_atomic(path, lease)
    return lease


def release_lease(agent_id):
    path = _lease_path(agent_id)
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def all_leases():
    out = []
    try:
        for fn in os.listdir(LEASE_DIR):
            if fn.endswith(".json"):
                d = read_json(os.path.join(LEASE_DIR, fn))
                if d:
                    out.append(d)
    except OSError:
        pass
    return out


def live_leases():
    t = now()
    return [l for l in all_leases() if float(l.get("expires", 0)) > t]


def prune_expired_leases():
    """Remove lease files whose TTL has passed. Returns the list pruned."""
    t = now()
    pruned = []
    try:
        for fn in os.listdir(LEASE_DIR):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(LEASE_DIR, fn)
            d = read_json(p)
            if not d or float(d.get("expires", 0)) <= t:
                try:
                    os.remove(p)
                    pruned.append(d or {"file": fn})
                except OSError:
                    pass
    except OSError:
        pass
    return pruned


# --------------------------------------------------------------------------- #
# Ownership marker -- the ONLY thing that authorizes a teardown
# --------------------------------------------------------------------------- #
def read_owner():
    return read_json(OWNER_PATH, None)


def write_owner(**kw):
    owner = read_owner() or {}
    owner.update(kw)
    write_json_atomic(OWNER_PATH, owner)
    return owner


def clear_owner():
    try:
        os.remove(OWNER_PATH)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Driving the zs* clients
# --------------------------------------------------------------------------- #
def _client(name):
    """Absolute path to a zs* client / the manager, preferring our env's bin/."""
    cand = os.path.join(BIN_DIR, name)
    return cand if os.path.exists(cand) else name


def run_client(name, args, timeout=90, capture=True):
    """Run a zs* client; return (rc, stdout, stderr)."""
    cmd = [_client(name)] + list(args)
    try:
        p = subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout)
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 2, "", "timeout running %s" % name
    except FileNotFoundError:
        return 127, "", "client not found: %s" % name


def zsstatus(instance=None, timeout=30):
    """Return (rc, status_dict|None). rc: 0 ok, 2 transport (down/retry),
    3 ambiguous-instance, other = error."""
    args = ["--instance", instance] if instance else []
    rc, out, err = run_client("zsstatus", args, timeout=timeout)
    if rc == 0:
        try:
            return 0, json.loads(out)
        except ValueError:
            return 2, None
    return rc, None


def control_token():
    tok = os.environ.get("ZSLURM_CONTROL_TOKEN")
    if tok:
        return tok
    try:
        with open(TOKEN_PATH) as fh:
            return fh.read().strip()
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def instance_names():
    out = []
    try:
        for fn in os.listdir(INSTANCES):
            if fn.endswith(".yaml"):
                out.append(os.path.splitext(fn)[0])
    except OSError:
        pass
    return sorted(out)


def reachable_instances():
    """List of (name, status_dict) for every instance whose manager answers."""
    live = []
    for name in instance_names():
        rc, st = zsstatus(instance=name)
        if rc == 0 and st and st.get("health", {}).get("ok"):
            live.append((name, st))
    return live


def pick_instance(prefer_foreign=True):
    """Choose the single canonical instance to use when several are reachable.

    Preference: a manager we did NOT auto-start (the human's / another agent's)
    over our own, then the one on the local host, then name order. Returns
    (name, status) or (None, None). Honour an explicit override via
    $ZSLURM_INSTANCE / $OFFLOAD_INSTANCE."""
    override = os.environ.get("OFFLOAD_INSTANCE") or os.environ.get("ZSLURM_INSTANCE")
    live = reachable_instances()
    if not live:
        return None, None
    if override:
        for name, st in live:
            if name == override:
                return name, st
    owner = read_owner() or {}
    ours = owner.get("instance") if owner.get("autostarted") else None

    def rank(item):
        name, st = item
        is_ours = (name == ours)
        local = (st.get("health", {}).get("instance", "") or name).endswith(hostname())
        # lower sorts first: foreign-preferred, then local-host, then name
        return (0 if (prefer_foreign and not is_ours) else 1,
                0 if local else 1, name)

    live.sort(key=rank)
    return live[0]


# --------------------------------------------------------------------------- #
# Snellius node facts (we have access to thin + fat + staging only; NOT
# himem / gpu). Partial-node allocations are allowed down to 1/8 of a node.
# --------------------------------------------------------------------------- #
NODE_FACTS = {
    "genoa":     {"cores": 192, "gb_per_core": 1.75, "sbu": 1.0, "min_frac_cores": 24},
    "rome":      {"cores": 128, "gb_per_core": 1.75, "sbu": 1.0, "min_frac_cores": 16},
    "fat_genoa": {"cores": 192, "gb_per_core": 7.5,  "sbu": 1.5, "min_frac_cores": 24},
    "fat_rome":  {"cores": 128, "gb_per_core": 7.5,  "sbu": 1.5, "min_frac_cores": 16},
    "staging":   {"cores": 16,  "gb_per_core": 14.0, "sbu": 2.0, "min_frac_cores": 1},
}
CHEAP_COMPUTE = ["genoa", "rome", "fat_genoa", "fat_rome"]   # never himem/gpu


def mem_mb_for(partition, cores):
    f = NODE_FACTS.get(partition, NODE_FACTS["genoa"])
    return int(cores * f["gb_per_core"] * 1024)


def partition_room():
    """Free capacity per base partition from `zsoccupancy --json`:
    {base: {'free_cores': int, 'open_nodes': int}}. open_nodes = idle + mixed
    (a partial allocation fits on an idle OR a partially-used 'mixed' node)."""
    try:
        rc, out, _ = run_client("zsoccupancy", ["--json"], timeout=30)
        rows = json.loads(out) if rc == 0 else []
    except Exception:
        rows = []
    agg = {}
    for r in rows:
        base = str(r.get("PARTITION", "")).split()[0]    # "genoa (scratch)" -> "genoa"
        if base not in NODE_FACTS:
            continue
        a = agg.setdefault(base, {"free_cores": 0, "open_nodes": 0})
        a["free_cores"] += max(0, int(r.get("CPU_TOT", 0)) - int(r.get("CPU_ALLOC", 0)))
        a["open_nodes"] += int(r.get("STATE_IDLE", 0)) + int(r.get("STATE_MIXED", 0))
    return agg


def pick_provision_partition(cores, prefer=None):
    """Cheapest CHEAP_COMPUTE partition with room for a `cores`-core PARTIAL
    allocation, by SBU tier (genoa/rome=1.0 before fat_*=1.5) then most-free-first.
    Falls back to 'genoa' if zsoccupancy is unavailable."""
    prefer = prefer or CHEAP_COMPUTE
    room = partition_room()
    tiers = {}
    for p in prefer:
        tiers.setdefault(NODE_FACTS[p]["sbu"], []).append(p)
    for sbu in sorted(tiers):
        cands = [p for p in tiers[sbu]
                 if room.get(p, {}).get("free_cores", 0) >= cores
                 and room.get(p, {}).get("open_nodes", 0) > 0]
        if cands:
            return max(cands, key=lambda p: room[p]["free_cores"])
    return "genoa"


# --------------------------------------------------------------------------- #
# Status helpers
# --------------------------------------------------------------------------- #
def queue_and_engines(status):
    """(pending_jobs, running_engines) from a zsstatus dict."""
    return (int(status.get("pending_jobs", 0) or 0),
            int(status.get("running_engines", 0) or 0))


def is_idle(status):
    """No pending jobs, no running engines -> nothing depends on the manager."""
    pend, eng = queue_and_engines(status)
    # also count RUNNING jobs via queue_stats if present
    running = 0
    try:
        running = int((status.get("engines", {}) or {}).get("totals", {}).get("running_jobs", 0) or 0)
    except Exception:
        running = 0
    return pend == 0 and eng == 0 and running == 0


# --------------------------------------------------------------------------- #
# Adopt: find an existing manager to offload into (NEVER provisions, NEVER spends)
# --------------------------------------------------------------------------- #
def adopt():
    """Return (instance, status, is_ours) for the manager to use, or
    (None, None, False) if none is reachable.

    PURE READ: never writes the owner marker. The marker is written ONLY by the
    provisioning path (up -> provision_*), so it always means "a manager WE
    started". Adopting a foreign/human manager therefore records nothing, and the
    reaper/down (which key entirely off autostarted==true + job/pid identity) can
    never mistake an adopted manager for one of ours."""
    name, st = pick_instance(prefer_foreign=True)
    if not name:
        return None, None, False
    owner = read_owner() or {}
    is_ours = (bool(owner.get("autostarted")) and owner.get("instance") == name
               and _self_manager_alive(owner))
    return name, st, is_ours


# --------------------------------------------------------------------------- #
# Self-manager liveness (only a manager WE provisioned can be torn down).
# Hardened against PID/jobid reuse: a recycled pid or reused jobid running some
# OTHER process must NOT read as "our manager".
# --------------------------------------------------------------------------- #
def _proc_starttime(pid):
    """Kernel start-time (clock ticks since boot) of a pid, or None."""
    try:
        with open("/proc/%d/stat" % int(pid)) as fh:
            # field 22 (1-based) is starttime; comm may contain spaces -> split on ')'
            return int(fh.read().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError, TypeError):
        return None


def _proc_cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % int(pid)) as fh:
            return fh.read().replace("\0", " ")
    except (OSError, ValueError, TypeError):
        return ""


def _pid_is_our_manager(owner):
    """True iff owner.manager_pid is alive, was the process WE started (start-time
    matches what we recorded), and still looks like a zslurm headless manager."""
    pid = owner.get("manager_pid")
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    rec_start = owner.get("manager_pid_start")
    if rec_start is not None and _proc_starttime(pid) != rec_start:
        return False  # pid was recycled to a different process
    cl = _proc_cmdline(pid)
    return ("zslurm" in cl and "--headless" in cl)


def _slurm_job_active(jobid):
    """Return True if the job is queued/running, False if definitively gone, None if
    UNKNOWN (transient squeue/controller failure -> callers fail-closed). 'Invalid
    job id' means the controller affirmatively does not know the job (never existed
    or completed and aged out) -> gone, NOT unknown."""
    if not jobid:
        return False
    try:
        p = subprocess.run(["squeue", "-h", "-j", str(jobid), "-o", "%T"],
                           capture_output=True, text=True, timeout=30)
    except Exception:
        return None  # transient -> unknown
    if p.returncode != 0:
        if "invalid job id" in (p.stderr or "").lower():
            return False  # controller says no such job -> gone
        return None       # contact/timeout error -> unknown
    state = (p.stdout or "").strip()
    return state in ("RUNNING", "PENDING", "CONFIGURING", "COMPLETING") if state else False


def _job_is_our_manager(jobid):
    """Verify a slurm job is OUR manager job (name zslurm_mgr, our user) before
    ever scancel-ing it -- guards against jobid reuse on a stale marker."""
    if not jobid:
        return False
    try:
        p = subprocess.run(["squeue", "-h", "-j", str(jobid), "-o", "%j|%u"],
                           capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    if p.returncode != 0:
        return None
    line = (p.stdout or "").strip()
    if not line:
        return False
    name, _, user = line.partition("|")
    import getpass
    return name.strip() == "zslurm_mgr" and user.strip() == getpass.getuser()


def _self_manager_alive(owner):
    """True iff the owner marker describes a manager WE started that is still up,
    verified by process/job identity (not just pid/jobid existence). Used for
    is_ours / teardown gating -> demands a DEFINITE yes (fail-safe: unknown=no)."""
    if not owner or not owner.get("autostarted"):
        return False
    backend = owner.get("backend")
    if backend == "login":
        return _pid_is_our_manager(owner)
    if backend == "node":
        # active (queued/running) AND identity-confirmed as our zslurm_mgr job
        return bool(_slurm_job_active(owner.get("slurm_jobid"))) and \
            (_job_is_our_manager(owner.get("slurm_jobid")) is not False)
    return False


# Reservation window: a node provision writes the marker BEFORE sbatch, with no
# jobid yet. If the broker dies between reservation and sbatch, the no-jobid marker
# must not block provisioning forever -> treat it as abandoned after this long.
RESERVATION_TTL_S = 300


def _self_provision_in_flight(owner):
    """Conservative ('don't double-spend') liveness for the up() in-flight guard.
    Returns True if the marker indicates a self-provision that MAY still be coming
    up -- and FAILS CLOSED (treats unknown as in-flight) so a transient squeue
    outage can never let a second `up` sbatch a duplicate manager."""
    if not owner or not owner.get("autostarted"):
        return False
    backend = owner.get("backend")
    if backend == "login":
        return _pid_is_our_manager(owner)
    if backend == "node":
        jobid = owner.get("slurm_jobid")
        if not jobid:
            # reservation written but sbatch not yet recorded a jobid
            if owner.get("provisioning"):
                return (now() - float(owner.get("started_at", 0))) < RESERVATION_TTL_S
            return False
        act = _slurm_job_active(jobid)          # True / False / None(unknown)
        if act is None:
            return True                          # FAIL CLOSED: assume still in flight
        if not act:
            return False
        # job exists; if squeue can affirmatively say it is NOT our zslurm_mgr job,
        # it is a reused id -> not in flight; unknown identity -> assume ours.
        return _job_is_our_manager(jobid) is not False
    return False


# --------------------------------------------------------------------------- #
# Configure a manager WE own (capped autogrow + sane budgets). Never touches an
# adopted/foreign manager.
# --------------------------------------------------------------------------- #
def configure_self_manager(instance, max_nodes=2, active_gb=1024, dcache_gb=512,
                           archive_gb=2048):
    tok = control_token()
    if not tok:
        log("configure_self_manager: no control token; skipping autogrow/budget config")
        return False
    env = dict(os.environ, ZSLURM_CONTROL_TOKEN=tok)

    def ctl(args):
        cmd = [_client("zscontrol"), "--instance", instance] + args
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
            return p.returncode, p.stdout, p.stderr
        except Exception as ex:
            return 2, "", str(ex)

    ctl(["budget", "--active", str(active_gb), "--dcache", str(dcache_gb),
         "--archive", str(archive_gb)])
    ctl(["autogrow", "on", "--max-nodes", str(int(max_nodes))])
    log("configured self manager %s: autogrow max_nodes=%d active=%dGB"
        % (instance, max_nodes, active_gb))
    return True


# --------------------------------------------------------------------------- #
# Provisioning backends (only reached via up(); both SPEND or RISK resources)
# --------------------------------------------------------------------------- #
def provision_login(no_autogrow=False):
    """Start a headless manager on the LOCAL (login) node. FREE but FRAGILE: the
    login node may kill long/CPU-heavy processes. Intended for short bursts /
    testing. Returns (instance, status) or (None, None)."""
    tok = control_token()
    if not tok:
        # mint one
        import secrets
        tok = secrets.token_hex(16)
        write_json_atomic(TOKEN_PATH, tok) if False else None
        with open(TOKEN_PATH, "w") as fh:
            fh.write(tok)
        os.chmod(TOKEN_PATH, 0o600)
    logdir = os.path.join(STATE_DIR, "manager_login")
    os.makedirs(logdir, exist_ok=True)
    args = [_client("zslurm"), "--headless", "--enable-control", "--control-token", tok]
    if no_autogrow:
        args.append("--no-autogrow")
    banner = os.path.join(logdir, "banner.json")
    errf = os.path.join(logdir, "manager.stderr.log")
    with open(banner, "w") as bo, open(errf, "a") as eo:
        p = subprocess.Popen(args, stdout=bo, stderr=eo, start_new_session=True)
    # wait for the manager to become reachable
    deadline = now() + 30
    inst = None
    while now() < deadline:
        b = read_json(banner)
        if b and b.get("instance"):
            inst = b["instance"]
            rc, st = zsstatus(instance=inst)
            if rc == 0 and st and st.get("health", {}).get("ok"):
                write_owner(instance=inst, autostarted=True, backend="login",
                            host=hostname(), manager_pid=p.pid,
                            manager_pid_start=_proc_starttime(p.pid),
                            slurm_jobid=None, started_at=now())
                log("provisioned LOGIN manager %s pid=%d" % (inst, p.pid))
                return inst, st
        time.sleep(0.5)
    log("provision_login: manager did not become reachable in time")
    return None, None


def provision_node(partition="genoa", cores=24, walltime="1-00:00:00",
                   max_nodes=2, mem_mb=None):
    """The human's pattern, automated: sbatch a (partial) compute node that runs
    the manager + a local engine ON that node. Returns (slurm_jobid, script_path).
    The manager becomes reachable only once the allocation starts; the caller
    polls reachable_instances() and records instance/node in the owner marker.

    This SPENDS SBU: a `cores`-core slice of a `partition` node for `walltime`
    (or until torn down). Keep `cores` at the 1/8-node minimum for cheap base
    capacity; capped autogrow adds whole-node overflow up to `max_nodes`.

    Returns (None, error_string) if the request fails a sanity ceiling."""
    # --- sanity rails on an explicit spend command (defensive, not a policy) ---
    if partition not in CHEAP_COMPUTE:
        return None, ("refusing partition %r for a manager-node base; allowed: %s "
                      "(never himem/gpu)" % (partition, ", ".join(CHEAP_COMPUTE)))
    f = NODE_FACTS[partition]
    cores = max(int(cores), int(f["min_frac_cores"]))     # floor at 1/8 node
    cores = min(cores, int(f["cores"]))                   # ceil at one whole node
    if _walltime_seconds(walltime) > 5 * 86400:           # Snellius hard max 5 days
        return None, "walltime %s exceeds the 5-day maximum" % walltime
    if mem_mb is None:
        mem_mb = mem_mb_for(partition, cores)
    mem_mb = min(int(mem_mb), int(f["cores"] * f["gb_per_core"] * 1024))
    tok = control_token()
    if not tok:
        import secrets
        tok = secrets.token_hex(16)
        with open(TOKEN_PATH, "w") as fh:
            fh.write(tok)
        os.chmod(TOKEN_PATH, 0o600)

    logdir = os.path.join(STATE_DIR, "manager_node")
    os.makedirs(logdir, exist_ok=True)
    script = os.path.join(logdir, "manager_node.sh")
    # conda base = .../miniconda3 (three levels up from .../envs/<env>/bin)
    conda_base = os.path.dirname(os.path.dirname(os.path.dirname(BIN_DIR)))
    conda_sh = os.path.join(conda_base, "etc/profile.d/conda.sh")
    env_name = os.path.basename(os.path.dirname(BIN_DIR))  # e.g. clustersnake

    body = """#!/bin/bash
#SBATCH -J zslurm_mgr
#SBATCH -p {partition}
#SBATCH --cpus-per-task={cores}
#SBATCH --mem={mem_mb}
#SBATCH -t {walltime}
#SBATCH -o {logdir}/manager_node-%j.out
set -uo pipefail
# Normalize env so the manager writes its instance YAML to ~/.zslurm/instances
# (zslurm_shared hardcodes that and the coordinator reads it there).
unset ZSLURM_HOME 2>/dev/null || true
# Make the clustersnake env binaries available without relying on `conda activate`
# (absolute PATH is authoritative; conda.sh is best-effort for runtime libs).
source {conda_sh} 2>/dev/null && conda activate {env_name} 2>/dev/null || true
export PATH={bindir}:$PATH
cd {logdir}
echo "[node-manager] host=$(hostname) job=$SLURM_JOB_ID cores={cores} mem={mem_mb}"

# 1) manager (headless; start with autogrow OFF -> broker enables it CAPPED,
#    so there is never a window at the built-in default cap of 40 nodes)
{bindir}/zslurm --headless --no-autogrow --enable-control --control-token "{tok}" \\
    > {logdir}/banner.$SLURM_JOB_ID.json 2> {logdir}/manager.$SLURM_JOB_ID.err &
MGR=$!

# 2) wait for the banner, then read the manager address/port from it
for i in $(seq 1 90); do
  [ -s {logdir}/banner.$SLURM_JOB_ID.json ] && break
  # bail early if the manager process died before writing a banner
  kill -0 $MGR 2>/dev/null || {{ echo "[node-manager] manager exited before banner"; exit 1; }}
  sleep 1
done
ADDR=$({bindir}/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["bind_host"])' {logdir}/banner.$SLURM_JOB_ID.json 2>/dev/null)
PORT=$({bindir}/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["base_port"])' {logdir}/banner.$SLURM_JOB_ID.json 2>/dev/null)
# never launch the engine against an empty address/port -> release the node instead
if [ -z "$ADDR" ] || [ -z "$PORT" ]; then
  echo "[node-manager] no valid banner (addr=$ADDR port=$PORT); aborting allocation"
  kill $MGR 2>/dev/null; exit 1
fi
echo "[node-manager] manager up addr=$ADDR port=$PORT"

# 3) local engine on THIS node (unmanaged: it lives as long as this allocation)
{bindir}/zslurm_chief -a "$ADDR" -p "$PORT" -c {cores} -m {mem_mb} -u \\
    > {logdir}/engine.$SLURM_JOB_ID.log 2>&1 &
ENG=$!

# 4) clean shutdown on scancel / walltime
term() {{ echo "[node-manager] SIGTERM"; kill $ENG $MGR 2>/dev/null; }}
trap term TERM INT
wait $MGR
kill $ENG 2>/dev/null
""".format(partition=partition, cores=cores, mem_mb=mem_mb, walltime=walltime,
           logdir=logdir, conda_sh=conda_sh, env_name=env_name, tok=tok, bindir=BIN_DIR)

    with open(script, "w") as fh:
        fh.write(body)
    os.chmod(script, 0o700)

    try:
        p = subprocess.run(["sbatch", "--parsable", script],
                           capture_output=True, text=True, timeout=60)
    except Exception as ex:
        log("provision_node sbatch failed: %s" % ex)
        return None, "sbatch failed: %s" % ex
    if p.returncode != 0:
        log("provision_node sbatch rc=%d err=%s" % (p.returncode, p.stderr.strip()))
        return None, "sbatch rejected: %s" % (p.stderr.strip() or "rc=%d" % p.returncode)
    jobid = (p.stdout or "").strip().split(";")[0]
    # MERGE the jobid into the reservation marker up() wrote before sbatch (keep
    # started_at); clear the provisioning flag now we have a job. Full identity is
    # set too so the marker is valid even if provision_node is ever called directly.
    write_owner(autostarted=True, backend="node", host=hostname(),
                slurm_jobid=jobid, provisioning=False,
                partition=partition, base_cores=cores)
    log("provision_node: sbatched manager job %s on %s (%dc)" % (jobid, partition, cores))
    return jobid, script


def _node_banner_instance(jobid):
    """The instance name OUR node-manager job wrote to its banner file -- used to
    bind the freshly provisioned instance to OUR jobid instead of trusting
    'whichever reachable instance appears' (which could be a foreign manager)."""
    b = read_json(os.path.join(STATE_DIR, "manager_node", "banner.%s.json" % jobid))
    return (b or {}).get("instance")


# --------------------------------------------------------------------------- #
# up(): the ONE entry point that may create capacity. Lock-guarded, adopt-first.
# --------------------------------------------------------------------------- #
def up(provision="adopt", partition="auto", cores=24, walltime="5-00:00:00",
       max_nodes=2, wait_s=180):
    """Ensure a shared manager exists. Adopt one if reachable (no spend). Else,
    per `provision`: 'adopt' -> refuse (no spend); 'login' -> free/fragile local
    headless; 'node' -> claim a partial node running manager+engine (SPENDS).
    Returns dict {instance, status, provisioned, backend, is_ours}."""
    # fast path: already reachable
    name, st, is_ours = adopt()
    if name:
        return {"instance": name, "status": st, "provisioned": False,
                "backend": "adopt", "is_ours": is_ours}

    # The lock wraps ONLY the decision + the sbatch/Popen (fast). The slow node
    # spin-up poll happens AFTER releasing the lock, so a slow allocation never
    # makes concurrent submit/down/up time out on the lock.
    node_jobid = None
    with manager_lock(timeout=90):
        name, st, is_ours = adopt()
        if name:
            return {"instance": name, "status": st, "provisioned": False,
                    "backend": "adopt", "is_ours": is_ours}

        # in-flight self-provision guard (FAIL CLOSED): a previous `up` may have a
        # manager still coming up (PENDING node job, or a reservation mid-sbatch).
        # Re-provisioning would double-spend + collide (zslurm_int5 + _1). The
        # reservation marker (written BEFORE sbatch below) makes this visible to a
        # racing agent during the spend itself, not only after.
        owner = read_owner() or {}
        if _self_provision_in_flight(owner):
            jobid = owner.get("slurm_jobid")
            inst = owner.get("instance") or (_node_banner_instance(jobid) if jobid else None)
            return {"instance": inst, "status": None, "provisioned": False,
                    "backend": owner.get("backend"), "is_ours": True,
                    "slurm_jobid": jobid,
                    "pending": "a self-provisioned manager is still starting; "
                               "not re-provisioning"}

        if provision == "adopt":
            return {"instance": None, "status": None, "provisioned": False,
                    "backend": "adopt", "is_ours": False,
                    "error": "no shared manager reachable; rerun with "
                             "--provision node (claim a node) or --provision login"}

        if provision == "login":
            # start with autogrow OFF, then enable it CAPPED (no default-cap-40 window)
            inst, st = provision_login(no_autogrow=True)
            if inst:
                configure_self_manager(inst, max_nodes=max_nodes)
            return {"instance": inst, "status": st, "provisioned": bool(inst),
                    "backend": "login", "is_ours": True}

        if provision == "node":
            # resolve the partition. 'auto' = cheapest thin-first with room;
            # 'auto_fat' = fat partitions only (~7.5 GB/core) for memory-heavy work.
            if partition in (None, "auto"):
                part = pick_provision_partition(cores)
            elif partition == "auto_fat":
                part = pick_provision_partition(cores, prefer=["fat_genoa", "fat_rome"])
            else:
                part = partition
            # RESERVE before sbatch: a racing agent reads this marker (atomic on
            # shared home) during our sbatch and refuses to provision a duplicate.
            write_owner(instance=None, autostarted=True, backend="node",
                        host=hostname(), manager_pid=None, slurm_jobid=None,
                        provisioning=True, started_at=now())
            jobid, detail = provision_node(partition=part, cores=cores,
                                           walltime=walltime, max_nodes=max_nodes)
            if not jobid:
                clear_owner()          # release the reservation on sbatch failure
                return {"instance": None, "status": None, "provisioned": False,
                        "backend": "node", "is_ours": False, "error": detail}
            node_jobid = jobid          # provision_node updated the marker with jobid
        # (lock released here)

    if node_jobid is None:
        return {"instance": None, "status": None, "provisioned": False, "error": "unreachable"}

    # OUTSIDE the lock: poll for OUR manager to come up. Bind to the instance OUR
    # job wrote in its banner (never 'first reachable', which could be foreign).
    deadline = now() + wait_s
    while now() < deadline:
        inst = _node_banner_instance(node_jobid)
        if inst:
            rc, st = zsstatus(instance=inst)
            if rc == 0 and st and st.get("health", {}).get("ok"):
                write_owner(instance=inst, provisioning=False)
                configure_self_manager(inst, max_nodes=max_nodes)
                return {"instance": inst, "status": st, "provisioned": True,
                        "backend": "node", "is_ours": True, "slurm_jobid": node_jobid}
        if _slurm_job_active(node_jobid) is False:
            break                       # job died before producing a usable manager
        time.sleep(3)
    return {"instance": None, "status": None, "provisioned": True,
            "backend": "node", "is_ours": True, "slurm_jobid": node_jobid,
            "pending": "manager job %s not up within %ds (still queued or failed); "
                       "check `squeue -j %s`" % (node_jobid, wait_s, node_jobid)}


# --------------------------------------------------------------------------- #
# down(): tear down ONLY a manager we provisioned, only when safe.
# --------------------------------------------------------------------------- #
def down(force=False):
    """Stop a self-provisioned manager iff: it is ours (autostarted), no live
    leases, and the queue/engines are idle (unless force). NEVER stops an
    adopted/foreign manager. Returns a result dict."""
    with manager_lock(timeout=90):
        owner = read_owner() or {}
        if not owner.get("autostarted"):
            return {"stopped": False, "reason": "no self-provisioned manager to stop"}
        if not _self_manager_alive(owner):
            # our manager is gone (or its pid/jobid was recycled to another process);
            # identity check failed -> safe to forget, NEVER signal it.
            clear_owner()
            return {"stopped": False, "reason": "self manager already gone (or identity "
                    "mismatch); cleared marker, signalled nothing"}

        # A LIVE LEASE ALWAYS BLOCKS TEARDOWN -- even with force. (force only waives
        # the idle check, so an operator can reclaim an idle manager that still shows
        # transient queue state, but never one another agent is actively using.)
        leases = live_leases()
        if leases:
            return {"stopped": False, "reason": "%d live lease(s) hold the manager"
                    % len(leases), "leases": [l.get("agent_id") for l in leases]}
        inst = owner.get("instance")
        rc, st = zsstatus(instance=inst) if inst else (2, None)
        idle = bool(st and is_idle(st))
        if not force and not idle:
            return {"stopped": False, "reason": "manager not idle (pending/running work)"}

        backend = owner.get("backend")
        if backend == "login":
            pid = owner.get("manager_pid")
            # identity re-verified by _self_manager_alive above (pid+start-time+cmdline)
            if pid and hostname() == owner.get("host") and _pid_is_our_manager(owner):
                try:
                    os.kill(int(pid), 15)
                except OSError:
                    pass
                # confirm it actually died before forgetting it
                gone = False
                for _ in range(20):
                    if not _pid_is_our_manager(owner):
                        gone = True
                        break
                    time.sleep(0.25)
                if gone:
                    clear_owner()
                    return {"stopped": True, "backend": "login", "manager_pid": pid}
                return {"stopped": False, "reason": "SIGTERM sent but manager still "
                        "alive; keeping marker for retry", "manager_pid": pid}
            return {"stopped": False, "reason": "login manager not locally identifiable "
                    "(pid on another host or not our process); refusing to signal"}
        if backend == "node":
            jobid = owner.get("slurm_jobid")
            # NEVER scancel a job that is not our zslurm_mgr job (jobid reuse guard)
            if _job_is_our_manager(jobid) is False:
                clear_owner()
                return {"stopped": False, "reason": "job %s is not our zslurm_mgr job "
                        "(reused id); cleared marker, scancelled nothing" % jobid}
            try:
                p = subprocess.run(["scancel", str(jobid)], capture_output=True,
                                   text=True, timeout=30)
            except Exception as ex:
                return {"stopped": False, "reason": "scancel error: %s; keeping marker" % ex}
            if p.returncode != 0:
                return {"stopped": False, "reason": "scancel rc=%d (%s); keeping marker "
                        "for retry" % (p.returncode, (p.stderr or "").strip())}
            clear_owner()
            return {"stopped": True, "backend": "node", "slurm_jobid": jobid}
        return {"stopped": False, "reason": "unknown backend %r" % backend}


# --------------------------------------------------------------------------- #
# Idempotent submit + lease write
# --------------------------------------------------------------------------- #
def _submit_record(key):
    return os.path.join(SUBMIT_DIR, "%s.json" % "".join(
        c if (c.isalnum() or c in "-_.") else "_" for c in key))


def _write_jobscript(agent_id, body, interp="bash", tag=None):
    os.makedirs(JOBSCRIPT_DIR, exist_ok=True)
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in (agent_id or "job"))
    uid = (("%s-" % tag) if tag else "") + binascii.hexlify(os.urandom(4)).decode()
    safe_uid = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in uid)
    path = os.path.join(JOBSCRIPT_DIR, "%s-%s.sh" % (safe, safe_uid))
    shebang = "#!/bin/bash" if interp in ("bash", "sh") else "#!/usr/bin/env %s" % interp
    with open(path, "w") as fh:
        fh.write(shebang + "\nset -e\n" + body + "\n")
    os.chmod(path, 0o700)
    return path


def _materialize_command(agent_id, cmd, shell=False, tag=None):
    """Return the token list to hand zsbatch. zsbatch space-joins job tokens, so
    inline shell quoting (`bash -c '...'`, pipes, redirects) would be lost. We
    materialize those into a script FILE (which round-trips as one token and whose
    shebang zsbatch honours). Simple argv commands pass through unchanged."""
    toks = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
    if not toks:
        return toks
    # explicit `bash -c '<script>'` / `sh -c '<script>'`
    if len(toks) >= 3 and toks[0] in ("bash", "sh") and toks[1] == "-c":
        return [_write_jobscript(agent_id, toks[2], toks[0], tag)]
    if shell:
        return [_write_jobscript(agent_id, " ".join(toks), "bash", tag)]
    # plain argv: only safe if it contains no shell metacharacters (the engine
    # reconstructs argv by splitting the space-join). If it does, wrap to be safe.
    META = set(">|<&;$`*?(){}[]\"'\n")
    if any(any(ch in META for ch in t) for t in toks):
        return [_write_jobscript(agent_id, " ".join(toks), "bash", tag)]
    return toks


def submit(agent_id, cmd, cores=1.0, mem_mb=1024, walltime="1:00:00",
           partition="compute", name=None, instance=None, idempotency_key=None,
           lease_margin_s=1800, note="", extra_args=None, shell=False):
    """Offload one command to the shared manager. Adopt-only (never provisions).

    The idempotency check and the intent-lease write happen UNDER manager_lock --
    the same lock down()/zsreap take to read leases -- so the lease is ordered
    strictly before any teardown's lease read (closing the submit<->teardown gap),
    and two concurrent retries of the same key cannot both submit. The (slower)
    zsbatch call itself runs outside the lock; the durable lease already protects
    it. Returns a result dict."""
    ttl = _walltime_seconds(walltime) + lease_margin_s

    # --- critical section: resolve manager, dedup, and stake the intent lease ---
    with manager_lock(timeout=90):
        if instance is None:
            instance, st, _ = adopt()
        if not instance:
            return {"ok": False, "code": 3,
                    "error": "no shared manager reachable; run `zsoffload up` first"}
        # idempotency: replay a prior submit, or claim an in-flight one for this key
        if idempotency_key:
            rec = read_json(_submit_record(idempotency_key))
            if rec and rec.get("jobid"):
                return {"ok": True, "jobid": rec["jobid"], "instance": instance,
                        "replayed": True}
            if rec and rec.get("inflight"):
                return {"ok": True, "jobid": None, "instance": instance,
                        "replayed": True, "inflight": True}
            write_json_atomic(_submit_record(idempotency_key),
                              {"inflight": True, "instance": instance, "ts": now(),
                               "agent_id": agent_id})
        # WRITE INTENT (lease) before releasing the lock -> teardown can never read
        # "no leases" between here and our submit.
        write_lease(agent_id, instance, ttl=ttl, note=note or (name or "offload"))

    args = ["-c", str(cores), "-n", "1", "--mem", str(int(mem_mb)),
            "-t", walltime, "-p", partition, "--instance", instance, "--parsable"]
    if name:
        args += ["-J", name]
    if extra_args:
        args += list(extra_args)
    job_tokens = _materialize_command(agent_id, cmd, shell=shell,
                                      tag=(idempotency_key or name))
    args += ["--"] + job_tokens

    rc, out, err = run_client("zsbatch", args, timeout=90)
    if rc != 0:
        # release the in-flight idempotency claim so a later retry can proceed
        if idempotency_key:
            try:
                os.remove(_submit_record(idempotency_key))
            except OSError:
                pass
        return {"ok": False, "code": rc, "error": (err or out).strip(),
                "instance": instance}
    jobid = (out or "").strip().split(";")[0].split()[-1]

    # finalize idempotency record (replaces the in-flight marker) + extend lease
    if idempotency_key:
        write_json_atomic(_submit_record(idempotency_key),
                          {"jobid": jobid, "instance": instance, "ts": now(),
                           "agent_id": agent_id})
    write_lease(agent_id, instance, ttl=ttl, note=note or (name or "offload"),
                jobids=[jobid])
    log("agent=%s submitted job %s (%s) to %s" % (agent_id, jobid, name or "", instance))
    return {"ok": True, "jobid": jobid, "instance": instance, "replayed": False}


def _walltime_seconds(s):
    """Parse a SLURM-style walltime into seconds.
    Formats: m | m:s | h:m:s | d-h | d-h:m | d-h:m:s ."""
    s = str(s).strip()
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = [int(x) for x in s.split(":")] if s else [0]
    if len(parts) == 1:
        h, m, sec = 0, parts[0], 0          # minutes
    elif len(parts) == 2:
        if days:
            h, m, sec = parts[0], parts[1], 0
        else:
            h, m, sec = 0, parts[0], parts[1]   # m:s
    else:
        h, m, sec = parts[0], parts[1], parts[2]
    return days * 86400 + h * 3600 + m * 60 + sec
