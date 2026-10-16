"""Idle retirement is explicit, identity-bound, portable and source-preserving."""
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest


def load(filename):
    loader = importlib.machinery.SourceFileLoader('consolidate_test_'+filename,
        str(Path(__file__).parents[1]/filename))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def manager():
    z = load('zslurm')
    z.gb.lock = threading.RLock()
    z.gb.log_file = io.StringIO()
    z.status.enable_control_rpc = True
    z.status.control_token = None
    z.status.manager_uuid = 'exact-primary'
    z.status.handover_mode = 'TARGET_ACTIVE'
    z.status.autoconsolidate_enable = False
    z.status.handover_saved_autoconsolidate = False
    z.status.autogrow_enable = True
    z.status.config = {'autogrow_max_compute_nodes': 47}
    z.jobs = z.JobManager()
    z.engines = z.EngineManager()
    return z


@pytest.mark.parametrize('shape', [(8,58788), (30,220708), (192,344064)])
def test_restore_idle_retirement_keeps_spider_and_snellius_work_and_capacity(manager, shape):
    z = manager
    engine = z.Engine('occupied', *shape, 'compute', cluster_id='123', managed=True)
    z.engines.engine_by_id['occupied'] = engine
    engine.jobs = ['work']
    job = SimpleNamespace(state='RUNNING', node_id='occupied')
    z.jobs.jobs_by_id['work'] = job
    reply = z.set_autoconsolidate(None, True, False, 'exact-primary')
    assert reply == dict(ok=True, autoconsolidate_enable=True,
                        previous=False, manager_uuid='exact-primary')
    assert z.status.handover_saved_autoconsolidate is True
    assert z.jobs.jobs_by_id == {'work': job} and job.state == 'RUNNING'
    assert z.engines.engine_by_id == {'occupied': engine}
    assert engine.jobs == ['work'] and engine.status != z.PHASING_OUT
    assert z.status.autogrow_enable is True
    assert z.status.config['autogrow_max_compute_nodes'] == 47
    assert z.health()['conditional_autoconsolidate'] is True


@pytest.mark.parametrize('mode', ['SOURCE_PREPARING','SOURCE_FORWARDING','TARGET_IMPORTED','TARGET_MIGRATING'])
def test_rejects_migration_and_old_forwarding_endpoint(manager, mode):
    manager.status.handover_mode = mode
    assert manager.set_autoconsolidate(None,True,False,'exact-primary')['ok'] is False
    assert manager.status.autoconsolidate_enable is False


@pytest.mark.parametrize('enable,expected,uuid', [(1,False,'exact-primary'),
    ('on',False,'exact-primary'), (True,0,'exact-primary'), (True,None,'exact-primary'),
    (True,True,'exact-primary'), (True,False,'other'), (True,False,None)])
def test_rejects_ambiguous_or_stale_request(manager, enable, expected, uuid):
    assert manager.set_autoconsolidate(None,enable,expected,uuid)['ok'] is False
    assert manager.status.autoconsolidate_enable is False


def test_replay_fails_closed_and_disabling_is_explicit(manager):
    z = manager
    assert z.set_autoconsolidate(None,True,False,'exact-primary')['ok'] is True
    assert z.set_autoconsolidate(None,True,False,'exact-primary')['ok'] is False
    assert z.set_autoconsolidate(None,False,True,'exact-primary')['ok'] is True


def test_control_gate_and_token_are_required(manager):
    z = manager
    z.status.enable_control_rpc = False
    assert z.set_autoconsolidate(None,True,False,'exact-primary')['ok'] is False
    z.status.enable_control_rpc = True
    z.status.control_token = 'secret'
    assert z.set_autoconsolidate(None,True,False,'exact-primary')['ok'] is False
    assert z.set_autoconsolidate('secret',True,False,'exact-primary')['ok'] is True


def test_restored_setting_survives_actual_handover_snapshot(manager, monkeypatch):
    z = manager
    monkeypatch.setattr(z, '_manager_descriptor', lambda *a, **kw: {'instance':'source','manager_uuid':'exact-primary'})
    z.set_autoconsolidate(None,True,False,'exact-primary')
    snapshot = z._build_handover_snapshot('transfer', {'manager_uuid':'target'})
    assert snapshot['scheduler']['autoconsolidate_enable'] is True
    z._apply_handover_snapshot(snapshot)
    assert z.status.autoconsolidate_enable is False  # inert until activation
    assert z.status.handover_saved_autoconsolidate is True


def test_cli_passes_expected_identity_and_state(monkeypatch):
    cli = load('zscontrol')
    calls = []
    monkeypatch.setattr(sys, 'argv', ['zscontrol','--instance','primary','consolidate',
        'on','--expect','off','--manager-uuid','exact-primary'])
    monkeypatch.setattr(cli, '_proxy', lambda _: SimpleNamespace(
        set_autoconsolidate=lambda *args: calls.append(args) or {'ok':True}))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 0
    assert calls == [(None,True,False,'exact-primary')]
