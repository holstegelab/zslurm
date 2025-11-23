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

# MODES
RUNNING = 1
STOPPING = 2


def read_yaml_config(filename):
    with open(filename, "r", encoding="utf-8") as file:
        yaml_config = yaml.load(file, Loader=yaml.FullLoader)
    return yaml_config


def write_yaml_config(filename, config):
    with open(filename, "w", encoding="utf-8") as file:
        file.write(yaml.dump(config))
    os.chmod(filename, 0o600)
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
