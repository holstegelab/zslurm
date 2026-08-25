import sys

if sys.version_info.major == 2:
    import xmlrpclib
    import httplib
else:
    import xmlrpc.client as xmlrpclib
    import http.client as httplib

import copy
import socket
from dns import resolver, reversename
import time
import yaml
import socket
import os
import os.path
import random
import string
import json
import re
import hashlib
import subprocess
import tempfile


DEFAULT_INSTANCE_NAME = "zslurm"
CONFIG_HOME = os.path.expanduser("~/.zslurm")
INSTANCE_DIR = os.path.join(CONFIG_HOME, "instances")
USER_CONFIG_FILENAME = os.path.join(CONFIG_HOME, "config.yaml")


# COMMANDS
NOOP = 0
STOP = 1
DIE = 2
CANCEL = 3
REREGISTER = 4
DEASSIGN = 5
MIGRATE_MANAGER = 6

# MODES
RUNNING = 1
STOPPING = 2


def read_yaml_config(filename):
    with open(filename, "r", encoding="utf-8") as file:
        yaml_config = yaml.load(file, Loader=yaml.FullLoader)
    return yaml_config


def write_yaml_config(filename, config):
    # Instance records are live service-discovery pointers.  A handover changes
    # their endpoint while clients and queued chiefs may read them, so never
    # expose a partially-written YAML file.
    directory = os.path.dirname(os.path.abspath(filename))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".instance-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(yaml.dump(config))
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, filename)
    finally:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except Exception:
            pass
    return read_yaml_config(filename)


class TimeoutHTTPConnection(httplib.HTTPConnection):
    def __init__(self, host, timeout=70):
        httplib.HTTPConnection.__init__(self, host, timeout=timeout)


class TimeoutTransport(xmlrpclib.Transport):
    def __init__(self, timeout=70, *l, **kw):
        xmlrpclib.Transport.__init__(self, *l, **kw)
        self.timeout = timeout

    def make_connection(self, host):
        conn = TimeoutHTTPConnection(host, self.timeout)
        return conn


class TimeoutServerProxy(xmlrpclib.ServerProxy):
    def __init__(self, uri, timeout=70, *l, **kw):
        kw["transport"] = TimeoutTransport(
            timeout=timeout, use_datetime=kw.get("use_datetime", 0)
        )
        xmlrpclib.ServerProxy.__init__(self, uri, *l, **kw)


# self register
port = 38864
address = "127.0.0.1"

cache_hostname = None


def get_full_hostname():
    return socket.getfqdn()


def get_hostname():
    # get IP address
    global cache_hostname
    if not cache_hostname is None:
        return cache_hostname
    # adresses = ['google.com', 'nu.nl', 'tweakers.net']
    # while adresses:
    #    try:
    #        socket.setdefaulttimeout(30)
    #        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM);
    #        s.connect((adresses[0], 0));
    #        myip = s.getsockname()[0]
    #        s.close()
    #        break
    #    except:
    #        adresses = adresses[1:]


    # try:
    #    addr=reversename.from_address(myip)
    #    myip = str(resolver.query(addr,"PTR")[0])[:-1]
    #    print(myip)
    # except Exception as e:
    #    myip = socket.gethostname().split('.')[0]
    #    pass
    #

    # if '-bb' in myip:
    #    myip = myip.split('-bb')[0]
    myip = get_full_hostname()
    if "." in myip:
        myip = myip.split(".")[0]
    cache_hostname = myip

    return myip


def short_name(name):
    return name.split(".")[0]


def format_time(seconds):
    try:
        t = max(0.0, float(seconds))
    except Exception:
        t = 0.0
    total = int(t)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, sec = divmod(rem, 60)
    if days > 0:
        return f"{days}-{hours:02d}:{minutes:02d}:{sec:02d}"
    return f"{hours:02d}:{minutes:02d}:{sec:02d}"


def _resolve_config_path(config_path=None):
    if config_path is not None:
        return os.path.expanduser(config_path)
    return os.path.expanduser(USER_CONFIG_FILENAME)


def _config_for_disk(config):
    cfg = copy.deepcopy(config or {})
    for compat_key in ("port", "rpcpath", "job_name", "address", "advertise_address", "_config_path"):
        cfg.pop(compat_key, None)

    return cfg

def _ensure_storage():
    try:
        os.makedirs(INSTANCE_DIR, exist_ok=True)
        try:
            os.makedirs(CONFIG_HOME, exist_ok=True)
        except Exception:
            pass
        try:
            os.chmod(CONFIG_HOME, 0o700)
        except Exception:
            pass
    except Exception:
        pass


def _safe_name(name):
    s = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or ""))
    return s or hashlib.sha1(str(name).encode("utf-8")).hexdigest()[:8]


def _instance_path(name):
    _ensure_storage()
    return os.path.join(INSTANCE_DIR, f"{_safe_name(name)}.yaml")


def get_instance_names():
    _ensure_storage()
    names = []
    try:
        for fn in os.listdir(INSTANCE_DIR):
            if fn.endswith(".yaml"):
                path = os.path.join(INSTANCE_DIR, fn)
                try:
                    data = read_yaml_config(path) or {}
                    nm = data.get("name") or os.path.splitext(fn)[0]
                    names.append(str(nm))
                except Exception:
                    names.append(os.path.splitext(fn)[0])
    except Exception:
        pass
    return sorted(names)


def _host_aliases():
    try:
        hn = get_hostname()
        fh = get_full_hostname()
        return [hn, short_name(hn), fh, short_name(fh), "localhost", "127.0.0.1"]
    except Exception:
        return ["localhost", "127.0.0.1"]


def resolve_instance_name(instance=None):
    names = get_instance_names()
    if instance:
        if instance in names:
            return instance        
    # No instance provided: prefer env var; else single discovered instance    
    if len(names) == 1:
        return names[0]
    return None


def get_instance_config(instance=None, copy_instance=True):
    if not instance:
        return None
    path = _instance_path(instance)
    try:
        data = read_yaml_config(path) or {}
    except Exception:
        data = {}
    if not isinstance(data, dict) or not data:
        return None
    return copy.deepcopy(data) if copy_instance else data


def set_instance_config(name, instance_config):
    inst = dict(instance_config or {})
    if "name" not in inst:
        inst["name"] = name
    if not inst.get("bind_host"):
        inst["bind_host"] = address
    if not inst.get("advertise_host"):
        inst["advertise_host"] = inst.get("bind_host", address)
    try:
        inst["base_port"] = int(inst.get("base_port", port))
    except Exception:
        inst["base_port"] = int(port)
    if not inst.get("rpcpath"):
        inst["rpcpath"] = "".join(random.sample(string.ascii_letters, 8))    
    path = _instance_path(name)
    write_yaml_config(path, inst)
    return inst

def remove_instance_from_disk(instance):
    path = _instance_path(instance)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
    return {}


def remove_instance_if_manager(instance, manager_uuid):
    """Remove an instance only while it still belongs to this manager.

    During handover the old instance name becomes an alias for the target.  The
    source manager's atexit handler must not delete that newly-owned pointer.
    """
    if not instance or not manager_uuid:
        return False
    current = get_instance_config(instance) or {}
    if str(current.get("manager_uuid") or "") != str(manager_uuid):
        return False
    remove_instance_from_disk(instance)
    return True

def update_instance_metadata(instance, updates):
    if not updates:
        return None
    inst = get_instance_config(instance) or {}
    inst.update(updates)
    return set_instance_config(instance, inst)


def remove_instance(instance):
    remove_instance_from_disk(instance)
    return {}


def get_config(config_path=None, instance=None):
    config_file = _resolve_config_path(config_path)
    config = {}
    if os.path.exists(config_file):
        loaded = read_yaml_config(config_file) or {}
        if isinstance(loaded, dict):
            config = loaded
    elif config_path is not None:
        raise RuntimeError(f"Config file {config_file} not found")

    # Overlay instance runtime if provided
    if instance:
        inst = get_instance_config(instance)
        if inst:
            bind_host = inst.get("bind_host") or address
            advertise_host = inst.get("advertise_host") or bind_host
            base_port = int(inst.get("base_port", port))
            config["address"] = bind_host
            config["advertise_address"] = advertise_host
            config["port"] = base_port
            config["rpcpath"] = inst.get("rpcpath")
            config["job_name"] = inst.get("job_name", instance)
            config["selected_instance"] = instance
    return config


def get_manager_url(instance=None, address=None):
    resolved = resolve_instance_name(instance)
    inst = get_instance_config(resolved) if resolved else None
    if not inst:
        raise KeyError(f"Instance '{instance}' not found")
    host = address or inst.get("advertise_host") or inst.get("bind_host") or "127.0.0.1"
    base_port = int(inst.get("base_port", port))
    rpcpath = inst.get("rpcpath")
    return f"http://{host}:{base_port}/{rpcpath}"


def get_job_url(instance=None, address=None):
    resolved = resolve_instance_name(instance)
    inst = get_instance_config(resolved) if resolved else None
    if not inst:
        raise KeyError(f"Instance '{instance}' not found")
    host = address or inst.get("advertise_host") or inst.get("bind_host") or "127.0.0.1"
    base_port = int(inst.get("base_port", port)) + 1
    rpcpath = inst.get("rpcpath")
    return f"http://{host}:{base_port}/{rpcpath}"


# Scratch-aware Slurm helpers
_scratch_parts_cache_ts = 0.0
_scratch_parts_cache = None


def _canon_state(s):
    # Slurm's JSON node "state" is a list (base state first, then flags), e.g.
    # ["IDLE"] or ["ALLOCATED", "RESERVED"] or ["DOWN", "DRAIN"]; the text form is
    # "IDLE+DRAIN". Both must reduce to the base state token -- without this the
    # JSON path produced the literal string "['IDLE']", so IDLE lookups always
    # returned 0 and the autogrow planner was blind to real node availability.
    if isinstance(s, (list, tuple)):
        s = s[0] if s else ""
    if not s:
        return "UNKNOWN"
    v = str(s).strip()
    if not v:
        return "UNKNOWN"
    v = v.split()[0]
    v = v.split("(")[0]
    v = v.split("+")[0]
    v = v.rstrip("*!")
    v = v.upper()
    return v or "UNKNOWN"


def _parse_features_str(v):
    if not v:
        return set()
    s = str(v).strip()
    if s in ("(null)", "N/A", "none", "None"):
        return set()
    out = set()
    for p in re.split(r"[,\s]+", s):
        p = p.strip()
        if p:
            out.add(p)
    return out


def _parse_partitions_any(v):
    if not v:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    s = str(v).strip()
    if s in ("(null)", "N/A", "none", "None"):
        return []
    return [p.strip() for p in s.split(",") if p.strip()]


def _kv_tokens_to_pairs(tokens):
    out = []
    k = None
    buf = []
    for t in tokens:
        if "=" in t:
            if k is not None:
                out.append((k, " ".join(buf)))
            a, b = t.split("=", 1)
            k, buf = a, [b]
        else:
            if k is not None:
                buf.append(t)
    if k is not None:
        out.append((k, " ".join(buf)))
    return out


def _parse_scontrol_text_nodes(s):
    s = s.replace("\u00A0", " ")
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    nodes = []
    cur = []
    for ln in lines:
        parts = ln.split()
        if any(p.startswith("NodeName=") for p in parts) and cur:
            nodes.append(dict(_kv_tokens_to_pairs(cur)))
            cur = []
        cur.extend(parts)
    if cur:
        nodes.append(dict(_kv_tokens_to_pairs(cur)))
    out = []
    for nd in nodes:
        parts = _parse_partitions_any(nd.get("Partitions"))
        state = _canon_state(nd.get("State"))
        feats = _parse_features_str(nd.get("AvailableFeatures"))
        out.append({"partitions": parts, "state": state, "features": feats})
    return out


def _parse_scontrol_json_nodes(s):
    try:
        data = json.loads(s)
    except Exception:
        return None
    nodes = data.get("nodes") or data.get("Nodes") or []
    out = []
    for nd in nodes:
        parts = _parse_partitions_any(nd.get("partitions") or nd.get("Partitions"))
        st_obj = nd.get("state") or nd.get("State")
        if isinstance(st_obj, dict):
            st = st_obj.get("current_state") or st_obj.get("state")
        else:
            st = st_obj
        feats = set()
        f = nd.get("features") or nd.get("available_features") or nd.get("AvailableFeatures")
        if isinstance(f, dict):
            av = f.get("available") or f.get("avail") or f.get("Available")
            if isinstance(av, list):
                feats = set(str(x) for x in av)
            elif isinstance(av, str):
                feats = _parse_features_str(av)
        elif isinstance(f, list):
            feats = set(str(x) for x in f)
        elif isinstance(f, str):
            feats = _parse_features_str(f)
        out.append({"partitions": parts, "state": _canon_state(st), "features": feats})
    return out


def _collect_states_by_scratch(nodes):
    parts = {}
    for nd in nodes:
        pset = nd.get("partitions") or []
        if not pset:
            pset = ["(none)"]
        has_scratch = ("scratch-node" in (nd.get("features") or set()))
        key = "scratch" if has_scratch else "no_scratch"
        st = nd.get("state") or "UNKNOWN"
        for p in pset:
            if p not in parts:
                parts[p] = {
                    "scratch": {"nodes": 0, "states": {}},
                    "no_scratch": {"nodes": 0, "states": {}},
                }
            grp = parts[p][key]
            grp["nodes"] = int(grp.get("nodes", 0)) + 1
            stc = grp.get("states")
            stc[st] = int(stc.get(st, 0)) + 1
    return parts


def slurm_partition_state_counts_by_scratch(cache_ttl_sec=60):
    global _scratch_parts_cache_ts, _scratch_parts_cache
    now = time.time()
    if _scratch_parts_cache is not None and (now - _scratch_parts_cache_ts) < float(cache_ttl_sec):
        return _scratch_parts_cache
    raw = None
    try:
        p = subprocess.run(["scontrol", "--json", "show", "nodes", "-o"], check=False, capture_output=True, text=True)
        if p.returncode == 0 and p.stdout.strip().startswith("{"):
            raw = p.stdout
            nodes = _parse_scontrol_json_nodes(raw)
        else:
            nodes = None
    except Exception:
        nodes = None
    if nodes is None:
        try:
            p = subprocess.run(["scontrol", "show", "nodes", "-o"], check=False, capture_output=True, text=True)
            if p.returncode == 0:
                raw = p.stdout
                nodes = _parse_scontrol_text_nodes(raw)
            else:
                nodes = []
        except Exception:
            nodes = []
    parts = _collect_states_by_scratch(nodes or [])
    _scratch_parts_cache = parts
    _scratch_parts_cache_ts = now
    return parts


# Maintenance-aware Slurm submission helpers
def parse_slurm_duration(value):
    """Return seconds for Slurm [days-]hours:minutes:seconds durations."""
    try:
        text = str(value).strip()
        if not text or text.upper() in ("INVALID", "N/A", "UNLIMITED", "INFINITE"):
            return None
        days = 0
        has_days = False
        if "-" in text:
            day_text, text = text.split("-", 1)
            days = int(day_text)
            has_days = True
        fields = [int(x) for x in text.split(":")]
        if len(fields) == 3:
            hours, minutes, seconds = fields
        elif len(fields) == 2:
            if has_days:
                hours, minutes, seconds = fields[0], fields[1], 0
            else:
                hours, minutes, seconds = 0, fields[0], fields[1]
        elif len(fields) == 1:
            if has_days:
                hours, minutes, seconds = fields[0], 0, 0
            else:
                hours, minutes, seconds = 0, fields[0], 0
        else:
            return None
        return days * 86400 + hours * 3600 + minutes * 60 + seconds
    except Exception:
        return None


def format_slurm_duration(seconds):
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days:
        return "%d-%02d:%02d:%02d" % (days, hours, minutes, seconds)
    return "%02d:%02d:%02d" % (hours, minutes, seconds)


def _slurm_time_epoch(value):
    try:
        return int(time.mktime(time.strptime(str(value), "%Y-%m-%dT%H:%M:%S")))
    except Exception:
        return None


# --- maintenance windows ---------------------------------------------------
# We deliberately do NOT try to work out when the next maintenance window starts.
# Snellius runs with PrivateData=reservations, so `scontrol show reservation`
# returns nothing to a normal user, and `sbatch --test-only` is not a usable
# substitute: it reports the PRIORITY-ordered start time, not the backfill one.
# Measured 2026-07-21 on genoa with 451 idle nodes, --test-only answered
# 2026-07-25T15:10 for 5 minutes, 12 hours, 1 day and 5 days alike -- no
# duration-dependent discontinuity left to binary-search -- while a real 1-hour
# job started instantly. Any inference resting on that oracle concludes "no
# maintenance" exactly when a window is imminent, which is the worst possible
# direction to fail in: it submits the full walltime, the job cannot start, and a
# still-usable allocation gets traded for one that will not run until the window
# has passed.
#
# Instead we hand Slurm both a maximum and a minimum walltime and let the
# backfill scheduler size the allocation for us:
#
#     sbatch -t <requested>  --time-min <floor>
#
# Slurm starts the job in the first slot that fits and truncates TimeLimit to
# exactly the time available before the next reservation. Nothing is inferred,
# and the boundary -- for anyone who wants the number -- is simply
# StartTime + TimeLimit of the resulting job.

DEFAULT_TIME_MIN_SECONDS = 12 * 3600


def submission_time_min(requested, floor_seconds=DEFAULT_TIME_MIN_SECONDS):
    """Return the ``--time-min`` value to submit alongside ``-t requested``.

    ``floor_seconds`` is the shortest allocation still worth having. Below it a
    compute node costs more to start up and drain than it gives back, so we would
    rather stay queued until after the maintenance window than take the scraps.

    Returns None when no meaningful floor applies (floor disabled, or not shorter
    than the request itself, which Slurm would reject); the caller then omits
    ``--time-min`` and the request behaves exactly as before.
    """
    try:
        floor_seconds = int(floor_seconds)
    except (TypeError, ValueError):
        return None
    if floor_seconds <= 0:
        return None
    requested_seconds = parse_slurm_duration(requested)
    if requested_seconds is not None and floor_seconds >= requested_seconds:
        return None
    return format_slurm_duration(floor_seconds)
