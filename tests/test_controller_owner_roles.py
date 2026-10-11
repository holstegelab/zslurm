"""Explicit legacy-controller adoption must not exempt scientific children."""
import copy
import xmlrpc.client

import pytest

from test_large_placement_window import manager, node
from test_controller_monitor_placement import job
from zslurm_startup import MONITOR_ROLE_ENV

PREFIX = 'app-controller:'


def owned(z, key, owner=None, role=None, dependency=None):
    j = job(z, key, role, dependency=dependency)
    j.owner = PREFIX+key if owner is None else owner
    return j


def submit(z, owner, env=None):
    key = z.jobs.submit_job('future', 'true', '/test/future', env or {}, 1, 8000,
        3600, 0, None, 0, 0, 0, 0, 0, 0, 'compute', 0, None, owner=owner)
    return z.jobs.jobs_by_id[key]


def test_default_does_not_infer_role_from_name_owner_or_inherited_env(manager):
    z = manager
    j = owned(z, 'controller')
    j.env = {'LSV_CONTROLLER_CORES': '1', 'LSV_CONTROLLER_MEM_MB': '8000'}
    old = node(z, 30, 220708); old.chief_version = '0.2.3'
    grant, = z.jobs.request_jobs(old.engine_id, 30, 220708, 'compute')
    assert MONITOR_ROLE_ENV not in grant[4]


@pytest.mark.parametrize('shape', [(30, 220708), (192, 344064)])
def test_adoption_keeps_ids_resources_dependencies_and_legacy_compute(manager, shape):
    z = manager
    coordinator = owned(z, 'controller')
    worker = owned(z, 'science', owner='scientific-workflow')
    worker.env = {'LSV_CONTROLLER_CORES': '1'}
    original = copy.deepcopy(coordinator.__dict__)
    result = z.jobs.register_controller_owner_prefix(PREFIX)
    assert result == dict(prefix=PREFIX, prefixes=[PREFIX], matched=1,
                          queued_tagged=1, queued_jobids=['controller'], running_unmodified=0)
    changed = {k for k in original if original[k] != coordinator.__dict__[k]}
    assert changed == {'env'}
    details = {row['jobid']: row for row in z.jobs.list_jobs_detailed()}
    assert details['controller']['owner'] == PREFIX+'controller'
    assert details['controller']['requested_monitor_role'] == 'controller'
    assert details['science']['requested_monitor_role'] == 'compute'
    old = node(z, *shape, name='old'); old.chief_version = '0.2.3'
    new = node(z, *shape, name='new'); new.chief_version = '0.2.4'
    assert [r[0] for r in z.jobs.request_jobs('old', *shape, 'compute')] == ['science']
    assert z.jobs.can_run_assigned_job('old', 'science')
    grants = z.jobs.request_jobs('new', *shape, 'compute')
    assert [r[0] for r in grants] == ['controller']
    assert grants[0][4][MONITOR_ROLE_ENV] == 'controller'
    assert z.jobs.can_run_assigned_job('new', 'controller')


def test_existing_running_and_shared_environment_are_not_hotpatched(manager):
    z = manager
    running = owned(z, 'running'); running.state = 'RUNNING'
    waiting = owned(z, 'waiting')
    common = {'KEEP': 'yes'}; waiting.env = running.env = common
    result = z.jobs.register_controller_owner_prefix(PREFIX)
    assert result['running_unmodified'] == 1 and result['queued_tagged'] == 1
    assert running.env == common == {'KEEP': 'yes'}
    assert waiting.env == {'KEEP': 'yes', MONITOR_ROLE_ENV: 'controller'}


def test_registration_is_idempotent_and_covers_future_submissions(manager):
    z = manager
    owned(z, 'queued')
    assert z.jobs.register_controller_owner_prefix(PREFIX)['queued_tagged'] == 1
    assert z.jobs.register_controller_owner_prefix(PREFIX)['queued_tagged'] == 0
    shared = {'KEEP': 'value'}
    future = submit(z, PREFIX+'later', shared)
    assert future.env[MONITOR_ROLE_ENV] == 'controller'
    assert shared == {'KEEP': 'value'}
    assert MONITOR_ROLE_ENV not in submit(z, 'scientific-workflow', shared).env
    assert MONITOR_ROLE_ENV not in submit(z, 'not-'+PREFIX+'later').env


@pytest.mark.parametrize('prefix', [None, '', ' ', '*', 'controller:*', '../*',
                                    'line\nbreak', 3, ['controller'], 'a'*257])
def test_invalid_prefixes_do_not_change_policy_or_jobs(manager, prefix):
    z = manager; j = owned(z, 'queued')
    with pytest.raises(ValueError): z.jobs.register_controller_owner_prefix(prefix)
    assert z.jobs.controller_owner_prefixes == [] and j.env == {}


@pytest.mark.parametrize('conflict', ['compute', 'unrecognized'])
def test_explicit_role_conflict_rejects_whole_registration(manager, conflict):
    z = manager; first = owned(z, 'first'); owned(z, 'second', role=conflict)
    with pytest.raises(ValueError, match='conflict'):
        z.jobs.register_controller_owner_prefix(PREFIX)
    assert z.jobs.controller_owner_prefixes == [] and first.env == {}


def test_inflight_untagged_grant_is_not_changed_after_delivery(manager):
    z = manager; first = owned(z, 'first'); grant = owned(z, 'grant')
    grant.assigned('old')
    with pytest.raises(ValueError, match='in flight'):
        z.jobs.register_controller_owner_prefix(PREFIX)
    assert z.jobs.controller_owner_prefixes == [] and first.env == grant.env == {}
    assert grant.state == 'ASSIGNED' and grant.node_id == 'old'


def test_already_tagged_grant_can_register_without_changing_it(manager):
    z = manager; grant = owned(z, 'grant', role='controller'); grant.assigned('new')
    original = copy.deepcopy(grant.__dict__)
    result = z.jobs.register_controller_owner_prefix(PREFIX)
    assert result['queued_tagged'] == 0 and grant.__dict__ == original


def test_policy_does_not_bypass_dependency_or_available_memory(manager):
    z = manager
    waiting = owned(z, 'waiting', dependency='afterok:999999')
    ready = owned(z, 'ready')
    z.jobs.register_controller_owner_prefix(PREFIX)
    new = node(z, 1, 4000); new.chief_version = '0.2.4'
    assert z.jobs.request_jobs(new.engine_id, 1, 4000, 'compute') == []
    new.totmem = 16000
    assert [r[0] for r in z.jobs.request_jobs(new.engine_id, 1, 16000, 'compute')] == ['ready']
    assert waiting.state == 'PENDING'


def test_conflicting_future_submission_is_rejected_before_queuing(manager):
    z = manager; z.jobs.register_controller_owner_prefix(PREFIX)
    with pytest.raises(ValueError, match='conflict'):
        submit(z, PREFIX+'wrong', {MONITOR_ROLE_ENV: 'compute'})
    assert not z.jobs.jobs_by_id and not z.jobs.total_jobs


def test_requeued_old_running_controller_gets_tag_only_when_waiting(manager):
    z = manager; j = owned(z, 'returned'); j.state = 'RUNNING'
    z.jobs.register_controller_owner_prefix(PREFIX)
    assert j.env == {}
    j.state = 'PENDING'; j.node_id = None
    old = node(z, 30, 220708); old.chief_version = '0.2.3'
    assert z.jobs.request_jobs(old.engine_id, 30, 220708, 'compute') == []
    assert j.env[MONITOR_ROLE_ENV] == 'controller' and j.state == 'PENDING'


def test_registered_policy_survives_actual_handover_serialization(manager, monkeypatch):
    z = manager; owned(z, 'queued'); z.jobs.register_controller_owner_prefix(PREFIX)
    z.status.instance_name = 'source'; z.status.manager_uuid = 'source-uuid'
    z.status.handover_owned_aliases = set()
    z.status.autogrow_enable = False; z.status.autoconsolidate_enable = False
    monkeypatch.setattr(z, '_manager_descriptor', lambda *a, **k:
                        dict(instance='source', manager_uuid='source-uuid'))
    monkeypatch.setattr(z, '_instance_names_owned_by', lambda _: ['source'])
    snapshot = z._build_handover_snapshot('test-role-transfer', dict(manager_uuid='target-uuid'))
    snapshot = xmlrpc.client.loads(xmlrpc.client.dumps((snapshot,), allow_none=True))[0][0]
    z.jobs = z.JobManager(); z.engines = z.EngineManager()
    z.status.instance_name = 'target'; z.status.manager_uuid = 'target-uuid'
    z._apply_handover_snapshot(snapshot)
    assert z.jobs.controller_owner_prefixes == [PREFIX]
    assert z.jobs.jobs_by_id['queued'].env[MONITOR_ROLE_ENV] == 'controller'
    assert submit(z, PREFIX+'future').env[MONITOR_ROLE_ENV] == 'controller'


def test_rpc_control_gate_and_forwarding(manager, monkeypatch):
    z = manager
    monkeypatch.setattr(z, '_control_denied', lambda _: dict(ok=False, code=1))
    assert z.rpc_register_controller_owner_prefix(None, PREFIX) == dict(ok=False, code=1)
    assert z.jobs.controller_owner_prefixes == []
    monkeypatch.setattr(z, '_control_denied', lambda _: None)
    monkeypatch.setattr(z, '_handover_committed_source', lambda: False)
    assert z.rpc_register_controller_owner_prefix(None, '*')['ok'] is False
    assert z.rpc_register_controller_owner_prefix(None, PREFIX)['ok'] is True
    calls = []
    class Target:
        def register_controller_owner_prefix(self, *args):
            calls.append(args); return dict(ok=True, forwarded=True)
    monkeypatch.setattr(z, '_handover_committed_source', lambda: True)
    monkeypatch.setattr(z, '_handover_proxy', lambda _: Target())
    assert z.rpc_register_controller_owner_prefix('token', PREFIX)['forwarded']
    assert calls == [('token', PREFIX)]
