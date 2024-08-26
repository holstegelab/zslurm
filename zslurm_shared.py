import sys

if sys.version_info.major == 2:
    import xmlrpclib
    import httplib
else:
    import xmlrpc.client as xmlrpclib
    import http.client as httplib

import socket
from dns import resolver, reversename
import time
import yaml
import socket
import os
import os.path
import random
import string


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


def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) == 0


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


def get_config():
    if os.path.exists('zslurm.config'):
        config_file = 'zslurm.config'
    else:
        config_file = os.path.expanduser("~/.zslurm")

    config = {"port": port, "reports_file_prefix": "reports"}

    if os.path.exists(config_file):
        config.update(read_yaml_config(config_file))

    if "rpcpath" not in config:
        config["rpcpath"] = "".join(random.sample(string.ascii_letters, 8))
        config = write_yaml_config(config_file, config)

    return config


def get_job_url(address="127.0.0.1"):
    config = get_config()
    return f"http://{address}:" + str(int(config["port"]) + 1) + "/" + config["rpcpath"]

def get_manager_url(address="127.0.0.1"):
    config = get_config()
    return f"http://{address}:" + str(int(config["port"])) + "/" + config["rpcpath"]

def format_time(rtime):
    days = 0
    hours = 0
    minutes = 0
    while rtime > (24 * 60 * 60):
        days += 1
        rtime -= 24 * 60 * 60
    while rtime > 60 * 60:
        hours += 1
        rtime -= 60 * 60
    while rtime > 60:
        minutes += 1
        rtime -= 60
    seconds = rtime
    rtime = "%d:%d:%d" % (hours, minutes, seconds)
    if days > 0:
        rtime = ("%d-" % days) + rtime

    return rtime
