"""Snellius autogrow: genoa/rome routing, fat-node policy and the fat swap."""
import importlib.machinery
import importlib.util
import io
import math
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOUR = 3600.0


@pytest.fixture(scope='module')
def module():
    loader = importlib.machinery.SourceFileLoader('zslurm_fat_policy_under_test', str(ROOT / 'zslurm'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    result = importlib.util.module_from_spec(spec)
    loader.exec_module(result)
    return result


@pytest.fixture
def z(module, monkeypatch):
    monkeypatch.setattr(module, 'status', module.Status())
    monkeypatch.setattr(module, 'engines', module.EngineManager())
    monkeypatch.setattr(module, 'jobs', module.JobManager())
    monkeypatch.setattr(module, '_discover_slurm_partitions', lambda: None)
    module.gb.lock = threading.RLock()
    module.gb.log_file = io.StringIO()
    config = {'cluster_site': 'snellius'}
    module._apply_cluster_config(config)
    module.status.config = config
    for name in ('active', 'dcache', 'archive'):
        setattr(module.jobs, name + '_total', 100000)
    module.engines.cluster_queued_by_partition = {}
    module.engines.queued_engines_detail = []
    return module


def add_jobs(z, n, cpu=1, mem_mb=1536, prefix='j'):
    for i in range(n):
        job = z.Job('job', '%s%d' % (prefix, i), 'true', '/tmp', {}, cpu, mem_mb, 3600,
                    0, None, 0, 0, 0, 0, 0, 0, 'compute', 0, None, '')
        z.jobs.jobs_by_id[job.jobid] = job


def states(**idle):
    """{'genoa': 3} -> plain idle nodes; 'genoa_ssd' -> scratch idle nodes."""
    result = {}
    for part in ('genoa', 'rome', 'fat_genoa', 'fat_rome'):
        result[part] = {
            'scratch': {'states': {'IDLE': idle.get(part + '_ssd', 0)}},
            'no_scratch': {'states': {'IDLE': idle.get(part, 0)}},
        }
    return result


def plan(z, **idle):
    with mock.patch.object(z.zslurm_shared, 'slurm_partition_state_counts_by_scratch',
                           return_value=states(**idle)):
        return z.compute_autogrow_plan([], 'compute')


def queue_pilot(z, cid, part, age=600.0, scratch=False, wait=None, autogrow=True):
    engine = z.Engine(cluster_id=cid, cores=128, totmem=224 * 1024, partition='compute',
                      managed=True, slurm_partition=part)
    engine.slurm_state = 'PENDING'
    engine.autogrow = autogrow
    z.engines.engine_by_clusterid[cid] = engine
    z.engines.queued_engines_detail.append((cid, part, age, scratch))
    z.engines.queued_engine_capacity_by_cid[cid] = (128.0, 224.0 * 1024)
    z.engines.cluster_queued_by_partition[part] = z.engines.cluster_queued_by_partition.get(part, 0) + 1
    z.engines.queued_start_estimate_by_cid[cid] = None if wait is None else time.time() + wait
    return engine


def test_snellius_grows_without_idle_nodes_on_genoa_or_rome(z):
    add_jobs(z, 1000)
    result = plan(z)
    assert result['best_part'] == 'genoa,rome'
    assert result['best_nodes'] >= 2          # sized by demand, not one at a time
    assert not result['need_fat']


def test_partial_core_pilots_keep_one_partition(z):
    # Snellius rejects multi-partition requests for shared (non-exclusive) jobs.
    z.status.autogrow_engine_cores = 64
    add_jobs(z, 300)
    assert ',' not in plan(z)['best_part']


def test_idle_genoa_is_preferred_alone(z):
    add_jobs(z, 300)
    assert plan(z, genoa=5, rome=5)['best_part'] == 'genoa'
    # No idle genoa: let Slurm start it on rome now, or genoa if one frees.
    assert plan(z, rome=5)['best_part'] == 'genoa,rome'


def test_genoa_rome_pilot_is_credited_at_rome_size(z):
    assert z.compute_partition_profile('genoa,rome') == {'cores': 128.0, 'mem_gb': 224.0}
    add_jobs(z, 100)                           # 100 cores: covered by one rome-sized pilot
    queue_pilot(z, 'q1', 'genoa,rome')
    assert plan(z)['best_nodes'] == 0


def test_fat_needs_memory_pressure_even_when_idle(z):
    add_jobs(z, 300)
    assert 'fat' not in str(plan(z, fat_genoa_ssd=5)['best_part'])
    z.jobs.jobs_by_id.clear()
    add_jobs(z, 40, cpu=4, mem_mb=64 * 1024)   # 16 GB/core: memory pressure
    result = plan(z, fat_genoa_ssd=5)
    assert result['need_fat'] and result['best_part'] == 'fat_genoa'


def swap(z, plan_dict=None, **idle):
    return z.plan_fat_availability_swap(plan_dict or {'need_fat': False}, time.time(),
                                        partition_states=states(**idle))


def test_swap_moves_a_late_thin_pilot_to_idle_fat(z):
    queue_pilot(z, 'late', 'genoa,rome', wait=8 * HOUR)
    queue_pilot(z, 'soon', 'genoa,rome', wait=1 * HOUR)
    result = swap(z, fat_genoa_ssd=2)
    assert result['fat_part'] == 'fat_genoa' and result['nodes'] == 1
    assert result['cancel_cids'] == ['late']


def test_swap_requires_six_hours_gain(z):
    queue_pilot(z, 'q', 'genoa,rome', wait=5 * HOUR)
    assert swap(z, fat_genoa_ssd=2) is None
    z.status.config['autogrow_fat_min_earlier_sec'] = 4 * HOUR
    assert swap(z, fat_genoa_ssd=2) is not None


def test_no_swap_when_thin_is_idle_or_fat_busy_or_pressure(z):
    queue_pilot(z, 'late', 'genoa,rome', wait=10 * HOUR)
    assert swap(z, fat_genoa_ssd=2, rome=1) is None          # thin node available now
    assert swap(z) is None                                    # no idle fat node
    assert swap(z, {'need_fat': True}, fat_genoa_ssd=2) is None


def test_fat_cap_of_ten_without_pressure(z):
    queue_pilot(z, 'late', 'genoa,rome', wait=10 * HOUR)
    for i in range(10):
        e = z.Engine(cluster_id='f%d' % i, partition='compute', managed=True, slurm_partition='fat_genoa')
        z.engines.engine_by_clusterid['f%d' % i] = e
    assert z.count_fat_compute_engines() == 10
    assert swap(z, fat_genoa_ssd=2) is None
    z.status.config['autogrow_max_fat_nodes'] = 11
    assert swap(z, fat_genoa_ssd=2)['nodes'] == 1


def test_unknown_estimate_counts_as_late_only_after_grace(z):
    queue_pilot(z, 'fresh', 'genoa,rome', age=60, wait=None)
    assert swap(z, fat_genoa_ssd=2) is None                   # Slurm has not estimated yet
    z.engines.queued_engines_detail = [('fresh', 'genoa,rome', 900.0, False)]
    result = swap(z, fat_genoa_ssd=2)                         # still N/A after backfill passes
    assert result['cancel_cids'] == ['fresh'] and math.isinf(result['thin_wait_s'])


def test_manual_pilots_are_never_swapped(z):
    queue_pilot(z, 'manual', 'genoa', wait=20 * HOUR, autogrow=False)
    assert swap(z, fat_genoa_ssd=2) is None


def test_scratch_pilots_need_idle_scratch_fat(z):
    queue_pilot(z, 'ssd', 'genoa,rome', wait=10 * HOUR, scratch=True)
    assert swap(z, fat_genoa=3) is None                       # only plain fat idle
    assert swap(z, fat_rome_ssd=1)['fat_part'] == 'fat_rome'


def test_cancel_is_pending_only_and_autogrow_only(z, monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            calls.append(args)
            self.returncode = 0

        def communicate(self):
            return b'', b''

    monkeypatch.setattr(z, 'Popen', FakePopen)
    engine = queue_pilot(z, '123_4', 'genoa,rome', wait=10 * HOUR)
    assert z.engines.cancel_pending_compute_engine('123_4')
    assert calls[-1][:2] == ['scancel', '--state=PENDING']
    assert engine.status == z.PHASING_OUT                     # kept until Slurm confirms
    queue_pilot(z, 'm', 'genoa', autogrow=False)
    assert not z.engines.cancel_pending_compute_engine('m')
    running = queue_pilot(z, 'r', 'genoa')
    running.slurm_state = 'R'
    assert not z.engines.cancel_pending_compute_engine('r')
    assert len(calls) == 1


def test_start_time_parsing(module):
    assert module.zslurm_shared.parse_slurm_start_time('N/A') is None
    assert module.zslurm_shared.parse_slurm_start_time('') is None
    epoch = module.zslurm_shared.parse_slurm_start_time('2026-11-08T10:30:00')
    assert time.localtime(epoch)[:5] == (2026, 11, 8, 10, 30)


def fat_engine(cores=192, mem_gb=1440, used_fraction=1.0):
    return SimpleNamespace(partition='compute', cores=cores, totmem=mem_gb * 1024,
                           res_cpu_reserved=cores * used_fraction,
                           res_mem_reserved_mb=mem_gb * 1024 * used_fraction * 0.5,
                           has_ssd=True, timeleft=4 * 24 * HOUR, status='',
                           cluster_id=None, managed=True)


def test_existing_fat_fleet_absorbs_memory_pressure(z):
    add_jobs(z, 400, cpu=1, mem_mb=3 * 1024)   # backlog averages 3 GiB/core
    assert plan(z)['need_fat']                  # thin-only fleet: pressure
    fleet = [fat_engine() for _ in range(10)]   # busy fat nodes: ~3.75 GiB/core held
    with mock.patch.object(z.zslurm_shared, 'slurm_partition_state_counts_by_scratch',
                           return_value=states()):
        result = z.compute_autogrow_plan(fleet, 'compute')
    # Running jobs hold 3.75 GiB/core on a 7.5 GiB/core fleet: no pressure.
    assert not result['need_fat']
    assert 'fat' not in str(result['best_part'])
