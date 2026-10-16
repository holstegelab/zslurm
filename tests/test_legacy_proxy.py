import socket
import threading
from unittest import mock
from xmlrpc.client import ServerProxy
from xmlrpc.server import SimpleXMLRPCServer

import pytest
from zslurm_legacy_proxy import create_server


def test_cached_endpoint_forwards_to_exact_target_without_own_queue():
    with SimpleXMLRPCServer(('127.0.0.1', 0), allow_none=True, logRequests=False) as target:
        target.register_function(lambda: {'ok': True, 'manager_uuid': 'approved'}, 'ping')
        target.register_function(lambda ids, owner: {'ids': ids, 'owner': owner}, 'get_job_states')
        thread = threading.Thread(target=target.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{target.server_address[1]}/RPC2'
        with socket.socket() as spare:
            spare.bind(('127.0.0.1', 0))
            port = spare.getsockname()[1]
        source = f'http://127.0.0.1:{port}/legacy'
        try:
            with mock.patch('zslurm_legacy_proxy.socket.gethostname', return_value='127.0.0.1'):
                with pytest.raises(ValueError, match='identity'):
                    create_server(source, url, 'wrong')
                with create_server(source, url, 'approved') as forwarding:
                    worker = threading.Thread(target=forwarding.serve_forever, daemon=True)
                    worker.start()
                    try:
                        with ServerProxy(source, allow_none=True) as client:
                            assert client.get_job_states(['42'], 'owner') == {'ids': ['42'], 'owner': 'owner'}
                            assert client.ping()['manager_uuid'] == 'approved'
                    finally:
                        forwarding.shutdown()
                        worker.join()
        finally:
            target.shutdown()
            thread.join()


def test_proxy_rejects_nonlocal_source_and_forwarding_loops():
    with pytest.raises(ValueError, match='local legacy'):
        create_server('http://elsewhere.invalid:123/a', 'http://localhost:456/b', 'x')
    host = socket.gethostname()
    with pytest.raises(ValueError, match='local legacy'):
        create_server(f'http://{host}:123/a', f'http://{host}:123/b', 'x')
