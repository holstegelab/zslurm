"""A rolling chief update must not silently lose controller protection."""
import pytest
from test_large_placement_window import manager, node
from zslurm_startup import controller_monitor_supported, MONITOR_ROLE_ENV


@pytest.mark.parametrize('version,expected', [(None,False), ('',False), ('0.2.3',False),
    ('0.2.4',True), ('0.2.5',True), ('0.3.0',True), ('0.2.4-dev',False), (True,False)])
def test_minimum_version_is_explicit_and_fail_closed(version, expected):
    assert controller_monitor_supported(version) is expected


def job(z, key, role=None, dependency=None):
    env = {} if role is None else {MONITOR_ROLE_ENV:role}
    result = z.Job(key,key,'true','/test-only/'+key,env,1,8000,3600,0,dependency,
                   0,0,0,0,0,0,'compute',0,None,'',owner='test',priority=1000 if role else 100)
    z.jobs.jobs_by_id[key] = result
    return result


@pytest.mark.parametrize('shape', [(30,220708), (192,344064)])
def test_mixed_spider_snellius_fleet_keeps_legacy_compute_and_new_controllers(manager, shape):
    z = manager
    old = node(z,*shape,name='old'); old.chief_version='0.2.3'
    new = node(z,*shape,name='new'); new.chief_version='0.2.4'
    coordinator=job(z,'controller','controller'); worker=job(z,'compute')
    assert [r[0] for r in z.jobs.request_jobs('old',*shape,'compute')] == ['compute']
    assert z.jobs.can_run_assigned_job('old','compute')
    assert coordinator.state == 'PENDING'
    assert [r[0] for r in z.jobs.request_jobs('new',*shape,'compute')] == ['controller']
    assert z.jobs.can_run_assigned_job('new','controller')
    assert coordinator.state == 'RUNNING'


def test_legacy_grant_is_returned_to_queue_before_start_not_killed(manager):
    z=manager; old=node(z,30,220708,name='old'); old.chief_version='0.2.3'
    coordinator=job(z,'controller','controller'); coordinator.assigned('old')
    assert not z.jobs.can_run_assigned_job('old','controller')
    assert coordinator.state == 'PENDING' and coordinator.node_id is None


@pytest.mark.parametrize('dependency,mem', [(None,4000), ('afterok:999999',16000)])
def test_new_chief_cannot_bypass_dependency_or_resources(manager, dependency, mem):
    z=manager; new=node(z,1,mem,name='new'); new.chief_version='0.2.4'
    coordinator=job(z,'controller','controller',dependency=dependency)
    assert z.jobs.request_jobs('new',1,mem,'compute') == []
    assert coordinator.state == 'PENDING'
