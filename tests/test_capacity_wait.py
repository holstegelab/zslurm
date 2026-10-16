"""Bounded one-pilot accumulation, using actual grants/leases/completions."""
import time
import xmlrpc.client
from unittest import mock

import pytest
from . import test_request_capacity as base


@pytest.fixture
def fleet():
    base.CurrentCapacityTests.setUpClass()
    f = base.CurrentCapacityTests()
    f.setUp()
    f.node.timeleft = 100000
    f.z.status.prio_fillmem_context = 10000
    f.running_jobs = [f.running('original-' + str(i), cpu=2, mem=16000)
                      for i in range(12)]
    f.running_jobs.append(f.running('original-wide', cpu=4, mem=24000))
    f.high = f.add_job('qualification', cpu=4, mem=32000, priority=1000, slots=1)
    f.high.submit_ts = time.time() - 7200
    for i in range(40):
        f.add_job('backfill-' + str(i), cpu=2, mem=16000)
    for i in range(20):
        f.add_job('wide-' + str(i), cpu=4, mem=24000)
    return f


def poll(f, node=None):
    node = node or f.node
    return f.jobs.request_jobs(node.engine_id,
        *f.jobs._engine_headroom_locked(node.engine_id), 'compute')


def finish(f, index):
    f.jobs.job_done(f.running_jobs[index].jobid, 0)


def arm(f):
    finish(f, 0)
    assert poll(f) == []
    assert f.high.state == 'PENDING'
    assert f.high.node_id is None
    assert f.jobs.dcache_download_inuse == 0
    assert f.jobs.capacity_waits['compute']['jobid'] == f.high.jobid


def other_node(f, cores=30, mem=220708):
    node = f.z.Engine('other', cores, mem, 'compute', cluster_id=False, managed=True)
    node.timeleft = 100000
    f.z.engines.engine_by_id[node.engine_id] = node
    return node


def test_observed_spider_fragmentation_accumulates_second_completion(fleet):
    f = fleet
    arm(f)
    finish(f, 1)
    granted = poll(f)
    assert [r[0] for r in granted] == ['qualification']
    assert f.high.state == 'ASSIGNED'
    assert f.jobs.dcache_download_inuse == 1
    assert poll(f) == granted  # Lost reply, not a second reservation.
    assert f.jobs.can_run_assigned_job(f.node.engine_id, f.high.jobid)
    assert f.high.state == 'RUNNING'
    assert f.node.res_mem_reserved_mb <= f.node.totmem
    assert f.node.res_cpu_reserved <= f.node.cores


def test_disabled_control_reproduces_forty_starving_completions(fleet):
    f = fleet
    f.z.status.config['capacity_wait_max_sec'] = 0
    running = list(f.running_jobs)
    for _ in range(40):
        f.jobs.job_done(running.pop(0).jobid, 0)
        grants = poll(f)
        assert f.high.state == 'PENDING'
        for row in grants:
            job = f.jobs.jobs_by_id[row[0]]
            assert f.jobs.can_run_assigned_job(f.node.engine_id, job.jobid)
            running.append(job)
    assert not f.jobs.capacity_waits


def test_only_one_pilot_waits_other_busy_pilot_keeps_backfilling(fleet):
    f = fleet
    arm(f)
    second = other_node(f)
    busy = f.add_job('second-busy', cpu=26, mem=200000)
    busy.started(second.engine_id)
    grants = poll(f, second)
    assert grants and all(row[0] != 'qualification' for row in grants)
    assert f.jobs.capacity_waits['compute']['nodeid'] == f.node.engine_id


def test_target_stays_available_to_idle_pilot_and_wait_is_released(fleet):
    f = fleet
    arm(f)
    second = other_node(f)
    grants = poll(f, second)
    assert grants[0][0] == 'qualification'
    assert f.high.node_id == second.engine_id
    assert poll(f)  # Original pilot may use its hole again.
    assert not f.jobs.capacity_waits


@pytest.mark.parametrize('invalid', [
    'fresh', 'equal_priority', 'dependency', 'oversize_cpu', 'oversize_memory',
    'walltime', 'required_ssd', 'ssd_space', 'transfer', 'active', 'dcache', 'archive',
])
def test_ineligible_or_fresh_target_never_blocks_backfill(fleet, invalid):
    f = fleet
    if invalid == 'fresh':
        f.high.submit_ts = time.time()
    elif invalid == 'equal_priority':
        f.high.priority = 100
    elif invalid == 'dependency':
        f.high.dependency = 'afterok:missing'
        f.high.dependency_released = False
    elif invalid == 'oversize_cpu':
        f.high.ncpu = 31
    elif invalid == 'oversize_memory':
        f.high.mem = 230000
    elif invalid == 'walltime':
        f.high.reqtime = f.node.timeleft + 1
    elif invalid == 'required_ssd':
        f.high.ssd_use = 'required'
    elif invalid == 'ssd_space':
        f.node.has_ssd = True
        f.node.ssd_total_gb = 10
        f.high.ssd_use = 'required'
        f.high.ssd_gb = 11
    elif invalid == 'transfer':
        f.jobs.dcache_download_inuse = f.jobs.dcache_download_total
    else:
        setattr(f.high, invalid + '_start_use_add', 100001)
    finish(f, 0)
    assert poll(f)
    assert not f.jobs.capacity_waits
    assert f.high.state == 'PENDING'


@pytest.mark.parametrize('change', ['cancel', 'dependency', 'transfer', 'ssd', 'time'])
def test_changed_target_releases_existing_wait(fleet, change):
    f = fleet
    arm(f)
    if change == 'cancel':
        f.high.state = 'CANCELLED'
    elif change == 'dependency':
        f.high.dependency = 'afterok:missing'
        f.high.dependency_released = False
    elif change == 'transfer':
        f.jobs.dcache_download_inuse = f.jobs.dcache_download_total
    elif change == 'ssd':
        f.high.ssd_use = 'required'
    else:
        f.high.reqtime = f.node.timeleft + 1
    assert poll(f)
    assert not f.jobs.capacity_waits


def test_expiry_has_real_cooldown_not_immediate_rearming(fleet):
    f = fleet
    arm(f)
    deadline = f.jobs.capacity_waits['compute']['deadline']
    with mock.patch.object(f.z.time, 'time', return_value=deadline + 1):
        grants = poll(f)
        assert grants
        assert not f.jobs.capacity_waits
        assert f.jobs.capacity_wait_cooldowns['compute'] == deadline + 301
        assert poll(f) == grants  # Unacknowledged grants replay unchanged.
    with mock.patch.object(f.z.time, 'time', return_value=deadline + 302):
        assert poll(f) == grants
        assert f.high.state == 'PENDING'


def test_wait_is_bounded_by_remaining_pilot_walltime(fleet):
    f = fleet
    f.node.timeleft = f.high.reqtime + 120
    before = time.time()
    arm(f)
    assert before + 120 <= f.jobs.capacity_waits['compute']['deadline'] <= time.time() + 120


def test_higher_priority_work_can_use_protected_space(fleet):
    f = fleet
    arm(f)
    urgent = f.add_job('urgent', cpu=2, mem=16000, priority=2000)
    assert [r[0] for r in poll(f)] == [urgent.jobid]
    assert f.high.state == 'PENDING'


def test_surplus_backfill_does_not_consume_accumulated_shape(fleet):
    f = fleet
    arm(f)
    # Need memory, not CPUs. On a larger core shape, surplus CPU+memory can
    # still run tiny work while preserving the 32GB target's available memory.
    f.node.cores = 40
    f.node.totmem = 236708
    f.high.ncpu = 16
    tiny = f.add_job('tiny', cpu=1, mem=1000)
    # 14 free cores: target still cannot fit. No CPU surplus, so no new grant.
    assert poll(f) == []
    f.node.cores = 44
    # 18 free cores and 36.7GB now fit target: it must precede tiny backfill.
    grants = poll(f)
    assert grants[0][0] == f.high.jobid
    assert tiny.jobid in [r[0] for r in grants]


def test_snellius_fractional_shape_accumulates_without_rounding(fleet):
    f = fleet
    f.node.cores, f.node.totmem = 192, 344064
    # Replace observed Spider holdings with a saturated Snellius pilot.
    for job in f.running_jobs:
        f.jobs.job_done(job.jobid, 0)
    f.running('snellius-hold', cpu=169.25, mem=320000)
    f.high.ncpu, f.high.mem = 22.75, 40000
    assert poll(f) == []
    assert f.high.state == 'PENDING'
    # A dynamic lease releases enough memory; immutable maxima are unchanged.
    assert f.jobs.resize_running_job(f.node.engine_id, 'snellius-hold', 169.25, 300000)['ok']
    grants = poll(f)
    assert [row[0] for row in grants] == ['qualification']
    assert grants[0][5] == 22.75


def test_draining_or_removed_owner_cannot_hold_another_pilot(fleet):
    f = fleet
    arm(f)
    f.node.status = f.z.PHASING_OUT
    second = other_node(f)
    assert poll(f, second)[0][0] == f.high.jobid
    assert not f.jobs.capacity_waits


def test_disable_live_control_releases_wait_immediately(fleet):
    f = fleet
    arm(f)
    f.z.status.config['capacity_wait_max_sec'] = 0
    assert poll(f)
    assert not f.jobs.capacity_waits


def test_handover_recomputes_hint_without_preclaiming_or_losing_lease(fleet):
    f = fleet
    arm(f)
    z = f.z
    f.node.cluster_id = '12345'
    f.node.slurm_state = 'R'
    z.engines.engine_by_clusterid['12345'] = f.node
    with mock.patch.object(z, '_manager_descriptor', return_value={
            'instance': 'old', 'manager_uuid': 'old-uuid'}):
        snapshot = z._build_handover_snapshot('capacity-transfer', {'manager_uuid': 'new-uuid'})
    # Actual RPC serialization, not shared old-manager Python objects.
    snapshot = xmlrpc.client.loads(xmlrpc.client.dumps((snapshot,), allow_none=True))[0][0]
    assert 'capacity_waits' not in snapshot['manager']
    before = f.jobs._engine_headroom_locked(f.node.engine_id)
    z.jobs = z.JobManager()
    z.engines = z.EngineManager()
    z._apply_handover_snapshot(snapshot)
    f.jobs = z.jobs
    f.node = z.engines.engine_by_clusterid['12345']
    # Model the chief's re-registration after import.
    z.engines.engine_by_id[f.node.engine_id] = f.node
    f.high = f.jobs.jobs_by_id['qualification']
    assert not f.jobs.capacity_waits
    assert f.jobs._engine_headroom_locked(f.node.engine_id) == before
    assert f.jobs.dcache_download_inuse == 0
    assert poll(f) == []
    assert f.high.state == 'PENDING' and f.high.node_id is None
    assert f.jobs.capacity_waits['compute']['jobid'] == f.high.jobid
    f.jobs.job_done('original-1', 0)
    assert poll(f)[0][0] == f.high.jobid
    assert f.jobs.dcache_download_inuse == 1
