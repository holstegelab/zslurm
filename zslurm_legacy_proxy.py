"""Temporary job-RPC forwarding for pre-handover clients with cached URLs.

No queue, scheduling, discovery registration, or Slurm mutations live here.
Keep only until the legacy controllers have exited or learned the new endpoint.
"""
import argparse
import json
from pathlib import Path
import signal
import socket
from socketserver import ThreadingMixIn
import threading
from urllib.parse import urlsplit
from xmlrpc.server import SimpleXMLRPCRequestHandler, SimpleXMLRPCServer

import zslurm_shared


class Forwarder:
    def __init__(self, target_url):
        self.target_url = target_url

    def _dispatch(self, method, params):
        # One connection per request: ServerProxy is not thread-safe.
        with zslurm_shared.TimeoutServerProxy(self.target_url, allow_none=True) as proxy:
            return getattr(proxy, method)(*params)


class Server(ThreadingMixIn, SimpleXMLRPCServer):
    daemon_threads = True


def create_server(source_url, target_url, expected_uuid):
    source, target = urlsplit(source_url), urlsplit(target_url)
    local_names = {socket.gethostname(), socket.getfqdn(), socket.gethostname().split('.')[0]}
    if (source.scheme != 'http' or source.hostname not in local_names
            or not source.port or not source.path or source_url == target_url
            or (source.hostname == target.hostname and source.port == target.port)):
        raise ValueError('Expected a local legacy endpoint distinct from the target')
    with zslurm_shared.TimeoutServerProxy(target_url, allow_none=True) as proxy:
        health = proxy.ping()
    if not health.get('ok') or health.get('manager_uuid') != expected_uuid:
        raise ValueError('Target manager identity does not match the approved handover')

    class Handler(SimpleXMLRPCRequestHandler):
        rpc_paths = (source.path,)

    server = Server((source.hostname, source.port), requestHandler=Handler,
                    allow_none=True, logRequests=False)
    server.register_instance(Forwarder(target_url))
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-banner-log', required=True)
    parser.add_argument('--target-instance', required=True)
    parser.add_argument('--expected-target-uuid', required=True)
    args = parser.parse_args()
    with Path(args.source_banner_log).open() as stream:
        banner = next(json.loads(line) for line in stream if line.startswith('{'))
    target = zslurm_shared.get_job_url(instance=args.target_instance)
    with create_server(banner['job_url'], target, args.expected_target_uuid) as server:
        def stop(signum, frame):
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        print('Legacy job-RPC forwarding ready; no scheduler started', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()
