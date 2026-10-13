"""CCS GPU functionality on maintained main; no Slurm/GPU allocation needed."""
import importlib.machinery
import importlib.util
import ast
import io
import os
import runpy
import sys
import types
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest import mock
import xmlrpc.client

import pytest
import zslurm_gpu as gpu

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def module():
    loader = importlib.machinery.SourceFileLoader('zslurm_gpu_under_test', str(ROOT / 'zslurm'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    result = importlib.util.module_from_spec(spec)
    loader.exec_module(result)
    return result


@pytest.fixture
def z(module, monkeypatch):
    z = module
    monkeypatch.setattr(z, 'jobs', z.JobManager())
    monkeypatch.setattr(z, 'engines', z.EngineManager())
    monkeypatch.setattr(z, 'status', z.Status())
    monkeypatch.setattr(z, 'add_log_line', lambda *args: None)
    monkeypatch.setattr(z, 'set_status_message', lambda *args: None)
    z.gb.lock = threading.RLock()
    z.gb.log_file = None
    z.status.config = dict(job_walltime_buffer_sec=0, autogrow_max_gpu_nodes=5,
        consolidation_hysteresis_sec=0, consolidation_cooldown_sec=0,
        consolidation_min_runtime_sec=0)
    z.status.instance_name = 'gpu-test'
    z.status.manager_uuid = 'source-uuid'
    z.status.gpu_partition = 'gpu_a100_22c'
    z.status.node_profiles = {'normal': {'cores': 64, 'mem_gb': 192},
        'gpu_a100_22c': {'cores': 44, 'mem_gb': 320, 'gpus': 2, 'gpu_type': 'a100'}}
    z.status.reports_file = io.StringIO()
    z.status.lastin_first = False
    return z


def engine(z, name='gpu', devices=('GPU-A', 'GPU-B'), cores=44, memory=320*1024, cid=None):
    e = z.Engine(name, cores, memory, 'gpu' if devices else 'compute',
                 cluster_id=cid, managed=True, gpu_devices=devices)
    e.chief_version = z.VERSION
    e.slurm_state = 'R'
    z.engines.engine_by_id[name] = e
    if cid:
        z.engines.engine_by_clusterid[cid] = e
    return e


def submit(z, partition='gpu', count=1, dependency=None, priority=100, token=None, cores=4, memory=1024, runtime=30):
    return z.jobs.submit_job('test', '/bin/true', '/tmp', {}, cores, memory,
        runtime, 0, dependency, 0, 0, 0, 0, 0, 0, partition, 0, '', '', 'no', 0,
        'tester', token, priority, count)


def start(z, e, free_cpu=None, free_mem=None):
    grants = z.jobs.request_jobs(e.engine_id, e.cores if free_cpu is None else free_cpu,
                                 e.totmem if free_mem is None else free_mem, e.partition)
    for job in grants:
        assert z.jobs.can_run_assigned_job(e.engine_id, job[0], gpu.device_ids(job[4]['ZSLURM_GPU_DEVICES']))
    return grants


def pending(z, cid='123_0', state='PD'):
    profile = z.gpu_worker_profile()
    e = z.Engine(partition='gpu', cluster_id=cid, managed=True,
                 cores=profile['resources'][0], totmem=profile['resources'][1],
                 slurm_partition=z.status.gpu_partition)
    e.planned_gpus = profile['resources'][2]
    e.slurm_state = state
    z.engines.engine_by_clusterid[cid] = e
    return e


def test_two_devices_run_two_jobs_not_three(z):
    e = engine(z)
    ids = [submit(z) for _ in range(3)]
    grants = start(z, e)
    assert [g[0] for g in grants] == ids[:2]
    assert [g[4]['ZSLURM_GPU_DEVICES'] for g in grants] == ['GPU-A', 'GPU-B']
    assert e.res_cpu_reserved == 8
    assert z.jobs.jobs_by_id[ids[2]].state == 'PENDING'


def test_lost_grant_response_replays_same_devices_without_overbooking(z):
    e = engine(z)
    first, second, third = [submit(z) for _ in range(3)]
    grants = z.jobs.request_jobs(e.engine_id, e.cores, e.totmem, 'gpu')
    assert [g[0] for g in grants] == [first, second]
    assert z.jobs.jobs_by_id[first].state == 'ASSIGNED'
    replay = z.jobs.request_jobs(e.engine_id, e.cores, e.totmem, 'gpu')
    assert replay == grants
    assert gpu.available_devices(e) == []
    assert z.jobs.jobs_by_id[third].state == 'PENDING'
    assert z.jobs.can_run_assigned_job(e.engine_id, first, ['GPU-A'])
    assert z.jobs.can_run_assigned_job(e.engine_id, first, ['GPU-A'])
    assert e.res_cpu_reserved == 4


@pytest.mark.parametrize('code', [0, 1, -15, -20])
def test_terminal_outcomes_release_devices(z, code):
    e = engine(z)
    jid = submit(z, count=2)
    start(z, e)
    z.jobs.job_done(jid, code)
    assert gpu.available_devices(e) == ['GPU-A', 'GPU-B']
    assert e.res_cpu_reserved == 0


def test_cancel_keeps_running_devices_until_worker_completion(z):
    e = engine(z)
    jid = submit(z, count=2)
    start(z, e)
    z.jobs.cancel_job(jid)
    assert gpu.available_devices(e) == []
    z.jobs.job_done(jid, -15)
    assert gpu.available_devices(e) == ['GPU-A', 'GPU-B']


def test_failed_start_ack_releases_grant_devices(z):
    e = engine(z)
    jid = submit(z)
    z.jobs.request_jobs(e.engine_id, e.cores, e.totmem, 'gpu')
    e.cores = 1
    assert not z.jobs.can_run_assigned_job(e.engine_id, jid, ['GPU-A'])
    assert z.jobs.jobs_by_id[jid].state == 'PENDING'
    assert gpu.available_devices(e) == ['GPU-A', 'GPU-B']


def test_stale_gpu_ack_does_not_start_or_free_newer_grant(z):
    e = engine(z)
    jid = submit(z)
    z.jobs.request_jobs(e.engine_id, e.cores, e.totmem, 'gpu')
    assert not z.jobs.can_run_assigned_job(e.engine_id, jid)
    assert not z.jobs.can_run_assigned_job(e.engine_id, jid, ['GPU-B'])
    assert z.jobs.jobs_by_id[jid].state == 'ASSIGNED'
    assert e.gpu_allocations == {jid:['GPU-A']}
    assert z.jobs.can_run_assigned_job(e.engine_id, jid, ['GPU-A'])
    assert not z.jobs.can_run_assigned_job(e.engine_id, jid, ['GPU-B'])
    assert z.jobs.can_run_assigned_job(e.engine_id, jid, ['GPU-A'])


@pytest.mark.parametrize('acknowledged', [False, True])
def test_engine_loss_releases_devices_and_requeues(z, acknowledged):
    e = engine(z)
    jid = submit(z)
    if acknowledged:
        start(z, e)
    else:
        z.jobs.request_jobs(e.engine_id, e.cores, e.totmem, 'gpu')
    z.jobs.engine_removed(e.engine_id)
    assert not e.gpu_allocations
    assert not z.jobs.jobs_by_id[jid].gpu_devices
    assert z.jobs.jobs_by_id[jid].state in ('PENDING', 'REQUEUED')


def test_gpu_job_never_runs_on_cpu_or_wrong_partition(z):
    e = engine(z, 'cpu', devices=())
    submit(z)
    assert not z.jobs.request_jobs(e.engine_id, e.cores, e.totmem, 'gpu')
    assert not z.jobs.request_jobs(e.engine_id, e.cores, e.totmem, 'compute')


def test_devices_not_granted_without_current_cpu_and_memory(z):
    e = engine(z)
    jid = submit(z)
    assert not z.jobs.request_jobs(e.engine_id, 0, e.totmem, 'gpu')
    assert not e.gpu_allocations
    assert z.jobs.jobs_by_id[jid].state == 'PENDING'


def test_gpu_jobs_retain_dependency_enforcement_and_idempotency(z):
    e = engine(z)
    predecessor = submit(z, partition='compute', count=0)
    jid = submit(z, dependency='afterok:' + predecessor, token='gpu-token')
    assert submit(z, dependency='afterok:' + predecessor, token='gpu-token') == jid
    assert not start(z, e)
    z.jobs.job_done(predecessor, 0)
    assert start(z, e)[0][0] == jid


def test_failed_dependency_does_not_consume_gpu_or_grow(z):
    e = engine(z)
    before = submit(z, partition='compute', count=0)
    submit(z, dependency='afterok:' + before)
    z.jobs.job_done(before, 1)
    assert not start(z, e)
    assert z.compute_gpu_autogrow_plan([e])['best_nodes'] == 0


def test_gpu_requests_keep_pipeline_priority(z):
    e = engine(z)
    low = submit(z, count=2, priority=10)
    high = submit(z, count=2, priority=200)
    assert start(z, e)[0][0] == high
    assert z.jobs.jobs_by_id[low].state == 'PENDING'


def test_legacy_cpu_submission_and_listing_remain_compatible(z):
    args = ['old', 'true', '/tmp', {}, 1, 1024, 30, 0, None,
            0, 0, 0, 0, 0, 0, 'compute', 0, '', '', 'no', 0]
    jid = z.jobs.submit_job(*args)
    assert z.jobs.jobs_by_id[jid].ngpus == 0
    assert len(z.jobs.list_jobs()[0]) == 14
    assert len(z.jobs.list_jobs(None, True)[0]) == 15


def test_legacy_gpu_argument_position_is_rejected_before_id_allocation(z):
    counter = z.jobs.jobid_counter
    with pytest.raises(ValueError, match='Legacy GPU'):
        z.jobs.submit_job('old', 'true', '/tmp', {}, 1, 1024, 30, 0, None,
            0, 0, 0, 0, 0, 0, 'gpu', 0, '', '', 'no', 0, 'tester', 1)
    assert z.jobs.jobid_counter == counter


@pytest.mark.parametrize('partition,count', [('compute',1), ('gpu',0), ('gpu',-1), ('gpu',1.5), ('gpu',True), ('bad',0)])
def test_invalid_gpu_requests_are_rejected(z, partition, count):
    with pytest.raises(ValueError):
        submit(z, partition=partition, count=count)


def test_dynamic_cpu_memory_lease_does_not_free_gpu(z):
    e = engine(z)
    jid = submit(z)
    start(z, e)
    assert z.jobs.resize_running_job(e.engine_id, jid, 1, 512)['ok']
    assert len(gpu.available_devices(e)) == 1
    assert e.res_cpu_reserved == 1


def test_gpu_handover_preserves_running_devices_and_releases_unstarted_grants(z, monkeypatch):
    e = engine(z, cid='123')
    running = submit(z)
    start(z, e)
    assigned = submit(z)
    z.jobs.request_jobs(e.engine_id, e.cores, e.totmem, 'gpu')
    monkeypatch.setattr(z, '_manager_descriptor', lambda *a, **k: {'instance':'source', 'manager_uuid':'source-uuid'})
    snapshot = z._build_handover_snapshot('gpu-transfer', {'manager_uuid':'target-uuid'})
    snapshot = xmlrpc.client.loads(xmlrpc.client.dumps((snapshot,), allow_none=True))[0][0]
    z.jobs, z.engines = z.JobManager(), z.EngineManager()
    z._apply_handover_snapshot(snapshot)
    placeholder = z.engines.engine_by_clusterid['123']
    assert placeholder.gpu_allocations == {running:['GPU-A']}
    assert not z.jobs.jobs_by_id[assigned].gpu_devices
    assert z.jobs.jobs_by_id[assigned].state == 'PENDING'
    z.status.handover_mode = 'TARGET_IMPORTED'
    z.status.handover_expected_cluster_ids = {'123'}
    # A forwarded completion may arrive before the chief registers.
    z.jobs.job_done(running, 0)
    assert not placeholder.gpu_allocations


def test_legacy_cpu_handover_records_get_safe_gpu_defaults(z):
    e = engine(z, 'cpu', devices=(), cid='123')
    jid = submit(z, partition='compute', count=0)
    start(z, e)
    data = z._snapshot_job(z.jobs.jobs_by_id[jid])
    del data['ngpus'], data['gpu_devices']
    restored = z._job_from_handover(data)
    assert restored.ngpus == 0 and restored.gpu_devices == []


def test_corrupt_gpu_handover_is_rejected_before_target_import(z, monkeypatch):
    e = engine(z, cid='123')
    submit(z, count=2)
    start(z, e)
    monkeypatch.setattr(z, '_manager_descriptor', lambda *a, **k:{'instance':'source','manager_uuid':'source-uuid'})
    snapshot = z._build_handover_snapshot('broken', {'manager_uuid':'target-uuid'})
    snapshot['jobs'][0]['gpu_devices'] = ['GPU-A','GPU-A']
    z.jobs, z.engines = z.JobManager(), z.EngineManager()
    with pytest.raises(ValueError):
        z._apply_handover_snapshot(snapshot)
    assert not z.jobs.jobs_by_id and not z.engines.engine_by_clusterid


def test_gpu_handover_rejects_cpu_only_target_without_changing_source(z):
    submit(z)
    z.status.handover_secret = 'test-secret'
    z.status.handover_mode = 'ACTIVE'
    result = z.handover_to('test-secret', {'manager_uuid':'other'})
    assert not result['ok'] and 'GPU-capable' in result['error']
    assert z.status.handover_mode == 'ACTIVE' and len(z.jobs.jobs_by_id) == 1


def test_gpu_registration_cannot_drop_a_live_device(z):
    e = engine(z, cid='123')
    submit(z)
    start(z, e)
    with pytest.raises(ValueError, match='drop reserved'):
        z.engines.register('gpu', 44, 320*1024, 'gpu', '123', 0, 0, None, z.VERSION, ['GPU-B'])
    assert e.gpu_devices == ['GPU-A','GPU-B'] and len(e.gpu_allocations) == 1


def test_gpu_registration_preserves_manual_drain(z):
    node = z.engines.register('host', 44, 320*1024, 'gpu', '123', 0, 0, None, z.VERSION, ['GPU-A','GPU-B'])
    z.engines.toggle_phasing_out(node)
    again = z.engines.register('host', 44, 320*1024, 'gpu', '123', 0, 0, None, z.VERSION, ['GPU-A','GPU-B'])
    assert again == node
    assert z.engines.engine_by_id[node].status == z.PHASING_OUT
    assert z.engines.engine_by_id[node].phaseout_reason == 'manual'


def test_gpu_worker_registration_is_versioned_and_preserves_reservations(z):
    node = z.engines.register('host', 44, 320*1024, 'gpu', '123', 0, 0, None, z.VERSION, ['GPU-A','GPU-B'])
    e = z.engines.engine_by_id[node]
    e.slurm_state = 'R'
    jid = submit(z)
    start(z, e)
    again = z.engines.register('host', 44, 320*1024, 'gpu', '123', 0, 0, None, z.VERSION, ['GPU-A','GPU-B'])
    assert again == node and e.gpu_allocations == {jid:['GPU-A']}
    with pytest.raises(ValueError):
        z.engines.register('bad', 1, 1024, 'gpu', None, 0, 0)
    with pytest.raises(ValueError):
        z.engines.register('bad', 1, 1024, 'compute', None, 0, 0, None, z.VERSION, ['GPU-A'])


def test_gpu_autogrow_accounts_for_devices_not_spare_cpu(z):
    e = engine(z)
    for _ in range(5):
        submit(z)
    z.engines.cluster_running_engines_gpu = 1
    assert z.compute_gpu_autogrow_plan([e])['best_nodes'] == 2
    pending(z)
    pending(z, '123_1')
    z.engines.cluster_queued_engines_gpu = 2
    assert z.compute_gpu_autogrow_plan([e])['best_nodes'] == 0


def test_gpu_autogrow_needs_cpu_ram_and_devices_on_same_worker(z):
    e = engine(z)
    running = submit(z, memory=e.totmem)
    start(z, e)
    submit(z)
    assert len(gpu.available_devices(e)) == 1
    assert z.compute_gpu_autogrow_plan([e])['best_nodes'] == 1


def test_gpu_autogrow_does_not_combine_devices_from_different_workers(z):
    a, b = engine(z), engine(z, 'other', ('GPU-C','GPU-D'))
    submit(z)
    start(z, a, free_cpu=4, free_mem=1024)
    submit(z)
    start(z, b, free_cpu=4, free_mem=1024)
    submit(z, count=2)
    assert z.compute_gpu_autogrow_plan([a,b])['best_nodes'] == 1


def test_gpu_autogrow_does_not_credit_expiring_or_held_worker(z):
    e = pending(z)
    e.timeleft = 100
    submit(z, runtime=3600)
    assert z.compute_gpu_autogrow_plan([])['best_nodes'] == 1
    e.timeleft = 432000
    z.engines.actual_held_engine_cids.add(e.cluster_id)
    assert z.compute_gpu_autogrow_plan([])['best_nodes'] == 1


def test_queued_worker_memory_forecast_uses_chief_headroom(z):
    submit(z, memory=310*1024)
    plan = z.compute_gpu_autogrow_plan([])
    assert plan['best_nodes'] == 0 and plan['unfit_jobs'] == 1


def test_gpu_growth_respects_cap_and_independent_cooldown(z, monkeypatch):
    for _ in range(20):
        submit(z)
    z.status.config.update(autogrow_max_gpu_nodes=2, gpu_autogrow_cooldown_sec=30)
    calls = []
    monkeypatch.setattr(z.engines, 'start_slurm', lambda *a, **k: calls.append((a,k)) or [object()])
    assert z.manage_gpu_growth([], 100)['best_nodes'] == 2
    z.manage_gpu_growth([], 110)
    assert len(calls) == 1
    assert calls[0][0][1] == 2 and calls[0][0][3] == 'gpu_a100_22c'
    assert calls[0][1]['minimum_runtime_seconds'] >= 30


def test_no_gpu_growth_when_no_demand_or_storage_blocked(z):
    assert z.compute_gpu_autogrow_plan([])['best_nodes'] == 0
    jid = submit(z)
    z.jobs.jobs_by_id[jid].active_start_use_add = 100
    assert z.compute_gpu_autogrow_plan([])['best_nodes'] == 0


def test_gpu_growth_retains_transfer_concurrency_limits(z):
    for _ in range(10):
        jid = submit(z)
        z.jobs.jobs_by_id[jid].dcache_download_slots = 1
    z.jobs.dcache_download_total = 2
    assert z.compute_gpu_autogrow_plan([])['best_nodes'] == 1


def test_no_gpu_growth_for_unserviceable_runtime(z):
    submit(z, runtime=432001)
    plan = z.compute_gpu_autogrow_plan([])
    assert plan['best_nodes'] == 0 and plan['unfit_jobs'] == 1


def test_consolidation_keeps_all_busy_gpu_workers(z):
    a, b = engine(z), engine(z, 'other', ('GPU-C','GPU-D'))
    for e in (a,b):
        submit(z)
        start(z, e, free_cpu=4, free_mem=1024)
    idle = engine(z, 'spare', ('GPU-E','GPU-F'))
    plan = z.consolidate_gpu_engines([a,b,idle], time.time())
    assert plan['busy_workers'] == 2
    assert a.status == b.status == ''
    assert idle.status == z.PHASING_OUT


def test_consolidation_treats_unacknowledged_grant_as_busy(z):
    e = engine(z)
    submit(z)
    z.jobs.request_jobs(e.engine_id, e.cores, e.totmem, 'gpu')
    z.consolidate_gpu_engines([e], time.time())
    assert e.status == ''


def test_new_demand_restores_automatic_not_manual_gpu_drain(z):
    e = engine(z)
    z.consolidate_gpu_engines([e], time.time())
    assert e.status == z.PHASING_OUT
    submit(z)
    z.consolidate_gpu_engines([e], time.time())
    assert e.status == ''
    z.engines.toggle_phasing_out(e.engine_id)
    z.consolidate_gpu_engines([e], time.time())
    assert e.status == z.PHASING_OUT and e.phaseout_reason == 'manual'


def test_gpu_minimum_is_independent_from_cpu_minimum(z):
    e = engine(z)
    z.status.config['consolidation_min_engines'] = 3
    z.consolidate_gpu_engines([e], time.time())
    assert e.status == z.PHASING_OUT
    z.status.config['consolidation_min_gpu_engines'] = 1
    z.consolidate_gpu_engines([e], time.time())
    assert e.status == ''


def test_consolidation_cancels_only_surplus_pending_gpu_array_task(z, monkeypatch):
    e = engine(z)
    needed = pending(z)
    surplus = pending(z, '123_1')
    for _ in range(3):
        submit(z)
    calls = []
    process = SimpleNamespace(communicate=lambda: (b'',b''), returncode=0)
    monkeypatch.setattr(z, 'Popen', lambda args, **k: calls.append(args) or process)
    z.consolidate_gpu_engines([e], time.time())
    assert calls == [['scancel', '--state=PENDING', '123_1']]
    assert needed.status == '' and surplus.status == z.PHASING_OUT


@pytest.mark.parametrize('state', ['R', 'CG'])
def test_consolidation_never_cancels_running_unregistered_gpu_worker(z, state):
    e = pending(z, state=state)
    with mock.patch.object(z, 'Popen') as popen:
        z.consolidate_gpu_engines([], time.time())
        assert not z.engines.cancel_pending_gpu_engine(e.cluster_id)
        popen.assert_not_called()


def test_failed_gpu_pilot_cancellation_does_not_mark_draining(z, monkeypatch):
    e = pending(z)
    monkeypatch.setattr(z, 'Popen', lambda *a, **k: SimpleNamespace(communicate=lambda:(b'',b''), returncode=1))
    assert not z.engines.cancel_pending_gpu_engine(e.cluster_id)
    assert e.status == ''


def test_worker_gpu_request_has_profile_gres_and_pinned_runtime(z, monkeypatch):
    z.status.maintenance_window_enable = False
    calls = []
    monkeypatch.setattr(z, 'Popen', lambda args, **k: calls.append((args,k)) or SimpleNamespace(communicate=lambda:(b'123\n',b'')))
    monkeypatch.setattr(z, '_prepare_engine_runtime', lambda:'/pinned/slurm_to_zslurm')
    monkeypatch.setattr(z, '_engine_submission_environment', lambda:{'PINNED':'yes'})
    monkeypatch.setattr(z.time, 'sleep', lambda *a:None)
    result = z.engines.start_slurm(None, 2, '5-00:00:00', 'gpu', None, 0)
    args = calls[0][0]
    assert '--gres=gpu:a100:2' in args and '--cpus-per-task=44' in args
    assert '--mem=327680' in args and '--array=0-1' in args
    assert '/pinned/slurm_to_zslurm' in args and calls[0][1]['env'] == {'PINNED':'yes'}
    assert args[-4:] == ['-t','gpu','-c','44']
    assert len(result) == 2 and result[0].planned_gpus == 2


@pytest.mark.parametrize('site,part', [('spider','gpu_a100_22c'), ('snellius','gpu_a100')])
def test_gpu_site_defaults_are_portable(z, monkeypatch, site, part):
    monkeypatch.setattr(z, '_discover_slurm_partitions', lambda:None)
    z._apply_cluster_config({'cluster_site':site})
    assert z.status.gpu_partition == part
    # A physical GPU name alone is not permission to guess resources.
    with pytest.raises(ValueError, match='Configure cores'):
        z.resolve_worker_partition('gpu')


def test_absent_gpu_partition_is_disabled_and_wrong_site_route_rejected(z, monkeypatch):
    monkeypatch.setattr(z, '_discover_slurm_partitions', lambda:({'genoa','gpu_a100'}, {'gpu_a100'}))
    z._apply_cluster_config({'cluster_site':'snellius','gpu_partition':'gpu_a100_22c'}, discover_partitions=True)
    assert z.status.gpu_partition is None and z.status.partition_warnings
    with pytest.raises(ValueError):
        z.resolve_worker_partition('gpu_a100_22c')


def test_alternative_gpu_partition_and_mixed_routes(z):
    z.status.node_profiles['gpu_h100'] = {'cores':16,'mem_gb':180,'gpus':1,'gpu_type':'h100'}
    assert z.resolve_worker_partition('gpu_h100') == ('gpu_h100','gpu')
    with pytest.raises(ValueError):
        z.resolve_worker_partition('normal,gpu_h100')


def test_cpu_groups_do_not_select_gpu_workers(z):
    a = engine(z, cid='123')
    b = engine(z, 'cpu', devices=(), cid='124')
    assert not z.engines._matches_cluster_group(a, 'compute')
    assert z.engines._matches_cluster_group(a, 'gpu')
    z.engines.toggle_phasing_out('compute +1')
    assert b.status == z.PHASING_OUT and a.status == ''


@pytest.mark.parametrize('count', [1,2])
def test_child_visibility_and_container_overrides(count):
    assigned = ['GPU-A','GPU-B'][:count]
    env = gpu.child_environment(dict(CUDA_VISIBLE_DEVICES='99',
        APPTAINERENV_CUDA_VISIBLE_DEVICES='99', APPTAINERENV_NVIDIA_VISIBLE_DEVICES='all',
        SLURM_JOB_ID='submit-host', ZSLURM_GPU_DEVICES=','.join(assigned), ZSLURM_GPUS=str(count)),
        dict(CUDA_VISIBLE_DEVICES='GPU-A,GPU-B', SLURM_JOB_ID='worker'))
    assert env['CUDA_VISIBLE_DEVICES'] == env['APPTAINERENV_CUDA_VISIBLE_DEVICES'] == ','.join(assigned)
    assert env['NVIDIA_VISIBLE_DEVICES'] == env['APPTAINERENV_NVIDIA_VISIBLE_DEVICES'] == ','.join(assigned)
    assert env['SLURM_JOB_ID'] == 'worker'


def test_cpu_children_hide_worker_devices():
    env = gpu.child_environment({'CUDA_VISIBLE_DEVICES':'99'}, {'CUDA_VISIBLE_DEVICES':'0,1'})
    assert env['CUDA_VISIBLE_DEVICES'] == '' and env['NVIDIA_VISIBLE_DEVICES'] == 'void'
    # CPU pilots may inherit a submit-host sentinel; it is not a device grant.
    assert gpu.child_environment({}, {'CUDA_VISIBLE_DEVICES':'all'})['CUDA_VISIBLE_DEVICES'] == ''


@pytest.mark.parametrize('value', ['0,0', 'all', 'GPU-A,all'])
def test_bad_device_ids_are_rejected(value):
    with pytest.raises(ValueError):
        gpu.device_ids(value)


def test_invalid_child_assignment_fails_before_spawn():
    with pytest.raises(ValueError):
        gpu.child_environment({'ZSLURM_GPU_DEVICES':'9','ZSLURM_GPUS':'1'}, {'CUDA_VISIBLE_DEVICES':'0,1'})


def test_gpu_packing_cannot_combine_resource_or_runtime_fragments():
    request = dict(resources=(4,100,2), runtime=3600)
    workers = [dict(key=1, resources=(44,320,1),runtime=10000),
               dict(key=2, resources=(44,320,1),runtime=10000)]
    assert gpu.pack_workers([request], workers)[2] == 1
    workers = [dict(key=1,resources=(44,320,2),runtime=100)]
    assert gpu.pack_workers([request], workers)[2] == 1


def test_gpu_node_and_job_listing_are_explicit_extensions(z):
    e = engine(z)
    submit(z)
    start(z, e)
    assert len(z.engines.list_nodes()[0]) == 25
    assert z.engines.list_nodes(True)[0][25:] == (2,1,['GPU-A','GPU-B'])
    assert len(z.jobs.list_jobs()[0]) == 14
    assert z.jobs.list_jobs(None, True, True)[0][15:] == [1,['GPU-A']]
    assert z.jobs.queue_stats()['by_partition']['gpu']['running']['gpus'] == 1


def test_gpu_chief_requires_capability_and_registers_worker_local_devices(monkeypatch):
    from test_version_handshake import load_chief_registration
    namespace = load_chief_registration()
    namespace.update(os=os, zslurm_gpu=gpu)
    args = ('host', 44, 320*1024, 'gpu', '123', 0, 0, 'test')
    namespace['manager_supports_method'] = lambda *a: False
    with pytest.raises(RuntimeError, match='GPU-capable'):
        namespace['register_with_manager'](object(), *args)
    namespace['manager_supports_method'] = lambda *a: True
    calls = []
    proxy = SimpleNamespace(register_gpu_versioned=lambda *a: calls.append(a) or 'gpu')
    with mock.patch.dict(os.environ, {'CUDA_VISIBLE_DEVICES':'GPU-A,MIG-B'}):
        assert namespace['register_with_manager'](proxy, *args) == 'gpu'
    assert calls[0][-1] == ['GPU-A','MIG-B']


def test_chief_acknowledges_gpu_identity_but_keeps_cpu_rpc_unchanged():
    tree = ast.parse((ROOT/'zslurm_chief').read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == 'acknowledge_job_grant')
    namespace = {'zslurm_gpu':gpu}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                 str(ROOT/'zslurm_chief'), 'exec'), namespace)
    calls = []
    proxy = SimpleNamespace(can_run_assigned_job=lambda *a:calls.append(a) or True)
    args = ['J1','name','true','/tmp',{},1,1024,'ASSIGNED']
    assert namespace['acknowledge_job_grant'](proxy,'node',args)
    assert calls[-1] == ('node','J1')
    args[4] = {'ZSLURM_GPUS':'1','ZSLURM_GPU_DEVICES':'GPU-A'}
    assert namespace['acknowledge_job_grant'](proxy,'node',args)
    assert calls[-1] == ('node','J1',['GPU-A'])


@pytest.mark.parametrize('partition,gpus,code', [('gpu',2,0),('gpu',0,2),('compute',1,2)])
def test_gpu_cli_protocol_preserves_priority_token_and_seconds(monkeypatch, partition, gpus, code):
    calls = []
    shared = types.ModuleType('zslurm_shared')
    shared.get_job_url = lambda **k:'http://test.invalid/jobs'
    proxy = SimpleNamespace(ping=lambda:{'gpu_submit_protocol':1},
                            submit_job=lambda *a: calls.append(a) or '42')
    shared.TimeoutServerProxy = lambda *a, **k:proxy
    monkeypatch.setitem(sys.modules, 'zslurm_shared', shared)
    monkeypatch.setattr(sys, 'argv', ['zsbatch','--instance','test','-p',partition,
        '--gpus',str(gpus),'--priority','200','-t','24:00:00','/bin/true'])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(ROOT/'zsbatch'), run_name='__main__')
    assert stopped.value.code == code
    if code == 0:
        assert calls[0][6] == 86400
        assert calls[0][-2:] == (200,2)
        assert isinstance(calls[0][-3], str)  # GPU count never occupies token position.
    else:
        assert not calls


def test_gpu_cli_refuses_legacy_manager_before_submitting(monkeypatch):
    shared = types.ModuleType('zslurm_shared')
    shared.get_job_url = lambda **k:'http://test.invalid/jobs'
    shared.TimeoutServerProxy = lambda *a, **k:SimpleNamespace(ping=lambda:{},
        submit_job=lambda *a:pytest.fail('must not submit to legacy GPU protocol'))
    monkeypatch.setitem(sys.modules, 'zslurm_shared', shared)
    monkeypatch.setattr(sys, 'argv', ['zsbatch','--instance','test','-p','gpu','/bin/true'])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(ROOT/'zsbatch'), run_name='__main__')
    assert stopped.value.code == 1


def test_worker_group_slurm_counts_gpu_separately(z, monkeypatch):
    rows = (b'123|123|N/A|gpu-test|5-00:00:00|R|host|gpu_a100_22c|0|none|44|None\n'
            b'124|124|N/A|gpu-test|5-00:00:00|PD|host|gpu_a100_22c|0|none|44|Priority\n')
    monkeypatch.setattr(z, 'Popen', lambda *a, **k:SimpleNamespace(communicate=lambda:(rows,b''), returncode=0))
    assert z.engines._check_cluster_engines()
    assert z.engines.cluster_running_engines_gpu == 1
    assert z.engines.cluster_queued_engines_gpu == 1
    assert z.engines.cluster_running_engines == z.engines.cluster_queued_engines == 0
    assert z.engines.engine_by_clusterid['124'].planned_gpus == 2
