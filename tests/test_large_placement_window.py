"""Qualify the existing packing-window control against a long mixed backlog.

This is not a fair-share guarantee: visibility permits resource-based packing,
but neither FIFO nor a larger window guarantees service on every node shape.
"""
from collections import Counter
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import threading

import pytest


@pytest.fixture
def manager():
    loader = importlib.machinery.SourceFileLoader('zslurm_large_window_test',
                                                str(Path(__file__).parents[1]/'zslurm'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    z = importlib.util.module_from_spec(spec)
    loader.exec_module(z)
    z.gb.lock = threading.RLock()
    z.gb.log_file = io.StringIO()
    z.status.config = {'job_walltime_buffer_sec': 900}
    z.status.lastin_first = True
    z.status.prio_fillmem_context = 500
    z.status.enable_control_rpc = True
    z.status.control_token = None
    z.engines = z.EngineManager()
    z.engines.cluster_queued_by_partition = {}
    z.jobs = z.JobManager()
    z.jobs.active_total = z.jobs.dcache_total = z.jobs.archive_total = 100000
    return z


def backlog(z, split_count=3, reference_priority=100, reference_dependency=None):
    for group, count, cpu, mem in [('reference', 673, 4, 24000),
                                   ('otter', 8192, 2, 16000), ('split', split_count, 2, 8000)]:
        for i in range(count):
            key = group+'-'+str(i)
            job = z.Job(group, key, 'true', '/work/'+group, {}, cpu, mem, 21600, 0,
                        reference_dependency if group == 'reference' else None,
                        0,0,0,0,0,0,'compute',0,None,'', owner=group,
                        priority=reference_priority if group == 'reference' else 100)
            z.jobs.jobs_by_id[key] = job


def node(z, cores, mem, name='probe'):
    engine = z.Engine(name, cores, mem, 'compute', cluster_id=False, managed=True)
    z.engines.engine_by_id[name] = engine
    return engine


def mixed_spider_profiles(z):
    for i, shape in enumerate([(5,36708), (8,58788), (30,220708)]):
        node(z, *shape, name='profile-'+str(i))


@pytest.mark.parametrize('split_count', [0, 3])
@pytest.mark.parametrize('window', [500, 10000])
def test_window_can_expose_older_ready_work_without_fifo_or_priority_changes(manager, window, split_count):
    z = manager
    mixed_spider_profiles(z)
    engine = node(z, 30, 220708)
    backlog(z, split_count)
    reply = z.set_scheduler_mode(None, None, window)
    assert reply == dict(ok=True, lastin_first=True, prio_fillmem_context=window)
    selected = z.jobs.request_jobs(engine.engine_id,30,220708,'compute')
    counts = Counter(row[1] for row in selected)
    assert counts['otter'] > 0
    assert (counts['reference'] > 0) == (window == 10000)
    if split_count:
        assert counts['split'] > 0
    assert sum(row[5] for row in selected) <= 30
    assert sum(row[6] for row in selected) <= 220708
    assert all(job.priority == 100 for job in z.jobs.jobs_by_id.values())
    assert all(job.state in ('PENDING','ASSIGNED') for job in z.jobs.jobs_by_id.values())


@pytest.mark.parametrize('shape', [(8,58788), (30,220708), (192,344064)])
def test_larger_window_keeps_spider_and_snellius_capacity_and_grant_replay(manager, shape):
    z = manager
    engine = node(z,*shape)
    backlog(z)
    z.set_scheduler_mode(None,None,10000)
    selected = z.jobs.request_jobs(engine.engine_id,*shape,'compute')
    assert selected
    assert sum(row[5] for row in selected) <= shape[0]
    assert sum(row[6] for row in selected) <= shape[1]
    # A chief's repeated/stale free-capacity report must only replay holdings.
    replay = z.jobs.request_jobs(engine.engine_id,*shape,'compute')
    assert sorted(replay, key=lambda row: row[0]) == sorted(selected, key=lambda row: row[0])


def test_expanded_window_cannot_bypass_dependencies_or_strict_priority(manager):
    z = manager
    engine = node(z,30,220708)
    backlog(z,reference_priority=101,reference_dependency='afterok:999999999')
    z.set_scheduler_mode(None,None,10000)
    selected = z.jobs.request_jobs(engine.engine_id,30,220708,'compute')
    assert selected and all(row[1] != 'reference' for row in selected)


def test_expanded_window_cannot_start_a_task_beyond_allocation_time(manager):
    z = manager
    engine = node(z,30,220708)
    engine.timeleft = 21600  # No 15-minute placement headroom.
    backlog(z)
    z.set_scheduler_mode(None,None,10000)
    assert z.jobs.request_jobs(engine.engine_id,30,220708,'compute') == []


def test_expanded_window_preserves_higher_priority_even_with_worse_memory_fit(manager):
    z = manager
    engine = node(z,30,220708)
    backlog(z,reference_priority=101)
    z.set_scheduler_mode(None,None,10000)
    selected = z.jobs.request_jobs(engine.engine_id,30,220708,'compute')
    assert selected[0][1] == 'reference'
    assert sum(row[1] == 'reference' for row in selected) == 7
