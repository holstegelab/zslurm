"""Idle retention must use the placement horizon of each existing pilot.

No Slurm RPCs, cancellation or live-manager operations are used here. The
controller-loop tests stop after one real iteration and inspect phase flags.
"""
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import threading

import pytest


@pytest.fixture(scope='module')
def module():
    loader = importlib.machinery.SourceFileLoader(
        'zslurm_consolidation_walltime_test', str(Path(__file__).parents[1] / 'zslurm'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    result = importlib.util.module_from_spec(spec)
    loader.exec_module(result)
    return result


@pytest.fixture
def z(module, monkeypatch):
    z = module
    monkeypatch.setattr(z, 'status', z.Status())
    monkeypatch.setattr(z, 'jobs', z.JobManager())
    monkeypatch.setattr(z, 'engines', z.EngineManager())
    monkeypatch.setattr(z, 'add_log_line', lambda *args: None)
    z.gb.lock = threading.RLock()
    z.gb.log_file = io.StringIO()
    z.status.autogrow_enable = False
    z.status.autoconsolidate_enable = True
    z.status.config = dict(job_walltime_buffer_sec=900,
        engine_drain_before_end_enable=False,
        consolidation_hysteresis_sec=0, consolidation_cooldown_sec=0,
        consolidation_min_runtime_sec=0)
    z.jobs.active_total = z.jobs.dcache_total = z.jobs.archive_total = 100000
    return z


def engine(z, name, hours=120, cores=30, memory=220708, partition='compute'):
    e = z.Engine(name, cores, memory, partition, cluster_id='cid-' + name, managed=True)
    e.slurm_state = 'R'
    e.chief_version = z.VERSION
    e.timeleft = hours * 3600
    e.starttime -= 10000
    z.engines.engine_by_id[name] = e
    return e


def job(z, name, hours=48, cores=1, memory=1000, partition='compute', **changes):
    j = z.Job(name, name, 'true', '/tmp', {}, cores, memory, hours * 3600,
              0, None, 0, 0, 0, 0, 0, 0, partition, 0, None, '')
    j.submit_ts -= 1000
    for key, value in changes.items():
        setattr(j, key, value)
    z.jobs.jobs_by_id[name] = j
    return j


def plan(z, fleet, safety=1.05, minimum=0):
    with z.jobs.lock:
        pending = z._consolidation_pending_jobs(fleet, z.time.time(), 60)
        held = [j for j in z.jobs.jobs_by_id.values()
                if j.partition == fleet[0].partition and j.state in ('RUNNING', 'ASSIGNED')]
        keep, cpu, mem = z._consolidation_keep_engines(fleet, pending, held, safety, minimum)
    return pending, {e.engine_id for e in keep}, cpu, mem


def iteration(z, monkeypatch):
    class Done(BaseException):
        pass

    def stop(_seconds):
        raise Done

    monkeypatch.setattr(z.engines, '_check_cluster_engines', lambda: True)
    monkeypatch.setattr(z.time, 'sleep', stop)
    for part in {e.partition for e in z.engines.engine_by_id.values()}:
        z.status.consolidation_want[part] = z.time.time() - 1000
    with pytest.raises(Done):
        z.thread_check_commands()


@pytest.mark.parametrize('site,shape', [('spider', (30, 220708)), ('snellius', (192, 344064))])
def test_fifteen_thousand_long_tasks_do_not_retain_nineteen_short_idle_pilots(z, monkeypatch, site, shape):
    z._apply_cluster_config({'cluster_site': site})
    z.status.autogrow_enable = False
    z.status.config.update(engine_drain_before_end_enable=False,
        consolidation_hysteresis_sec=0, consolidation_cooldown_sec=0,
        consolidation_min_runtime_sec=0)
    fleet = [engine(z, 'idle-' + str(i), 12 + i / 10, *shape) for i in range(19)]
    for i in range(15201):
        job(z, 'pending-' + str(i))
    busy = engine(z, 'busy', 14, *shape)
    assigned = engine(z, 'unacknowledged', 14, *shape)
    running = job(z, 'resident', state='RUNNING', node_id=busy.engine_id,
                  held_ncpu=2.0, held_mem=1200.0)
    grant = job(z, 'grant', state='ASSIGNED', node_id=assigned.engine_id)
    pending, keep, _, _ = plan(z, fleet + [busy, assigned])
    assert not pending and keep == {'busy', 'unacknowledged'}
    iteration(z, monkeypatch)
    assert all(e.status == z.PHASING_OUT for e in fleet)
    assert busy.status != z.PHASING_OUT and assigned.status != z.PHASING_OUT
    assert len(z.jobs.jobs_by_id) == 15203
    assert running.state == 'RUNNING' and grant.state == 'ASSIGNED'
    assert all(j.state == 'PENDING' for name, j in z.jobs.jobs_by_id.items()
               if name.startswith('pending-'))


def test_long_compatible_pilot_cannot_make_short_pilots_credit_long_demand(z):
    short = [engine(z, 'short-' + str(i), 12) for i in range(4)]
    long = engine(z, 'long', 120)
    for i in range(200):
        job(z, str(i))
    pending, keep, cpu, _ = plan(z, short + [long])
    assert len(pending) == 200 and cpu == 210
    assert all(fits == {'long'} for _, fits in pending)
    assert keep == {'long'}


def test_short_demand_retains_only_its_own_supply_beside_large_long_backlog(z):
    short = [engine(z, 'short-' + str(i), 12) for i in range(4)]
    long = engine(z, 'long', 120)
    for i in range(200):
        job(z, str(i))
    job(z, 'backfill', 2, cores=2)
    _, keep, _, _ = plan(z, short + [long])
    assert keep == {'short-0', 'long'}


@pytest.mark.parametrize('left,expected', [(4499, False), (4500, True), (4500.9, True)])
def test_exact_assignment_walltime_buffer_boundary(z, left, expected):
    e = engine(z, 'pilot')
    e.timeleft = left
    j = job(z, 'target', 1)
    pending, keep, _, _ = plan(z, [e])
    assert bool(pending) == expected and bool(keep) == expected
    assert bool(z.jobs.request_jobs(e.engine_id, e.cores, e.totmem, 'compute')) == expected
    assert j.state == ('ASSIGNED' if expected else 'PENDING')


@pytest.mark.parametrize('buffer,left,expected', [(0, 3600, True), (1200, 4799, False), (1200, 4800, True)])
def test_site_configured_headroom_is_used_not_a_new_hardcoded_margin(z, buffer, left, expected):
    z.status.config['job_walltime_buffer_sec'] = buffer
    e = engine(z, 'pilot')
    e.timeleft = left
    job(z, 'target', 1)
    assert bool(plan(z, [e])[0]) == expected


@pytest.mark.parametrize('fault', ['dependency', 'young', 'active', 'dcache', 'archive',
                                  'cpu', 'memory', 'stopping', 'unmanaged', 'unobserved'])
def test_non_runnable_requests_do_not_retain_idle_capacity(z, fault):
    e = engine(z, 'pilot')
    j = job(z, 'target', 1)
    if fault == 'dependency':
        j.dependency = 'afterok:999999999'
        j.dependency_released = False
    elif fault == 'young':
        j.submit_ts = z.time.time()
    elif fault in ('active', 'dcache', 'archive'):
        setattr(j, fault + '_start_use_add', 100001)
    elif fault == 'cpu':
        j.ncpu = e.cores + 1
    elif fault == 'memory':
        j.mem = e.totmem + 1
    elif fault == 'stopping':
        e.stopping = True
    elif fault == 'unmanaged':
        e.managed = False
    elif fault == 'unobserved':
        e.slurm_state = None
    pending, keep, _, _ = plan(z, [e])
    assert not pending and not keep and j.state == 'PENDING'


def test_cpu_memory_and_walltime_cannot_be_borrowed_from_different_pilots(z):
    cpu = engine(z, 'cpu', memory=1000)
    mem = engine(z, 'memory', cores=1, memory=100000)
    job(z, 'target', cores=2, memory=2000)
    assert plan(z, [cpu, mem])[:2] == ([], set())


def test_required_ssd_and_runtime_must_fit_the_same_pilot(z):
    short = engine(z, 'short-ssd', 12)
    short.has_ssd, short.ssd_total_gb = True, 1000
    long = engine(z, 'long-plain', 120)
    j = job(z, 'target', ssd_use='required', ssd_gb=100)
    assert plan(z, [short, long])[:2] == ([], set())
    j.reqtime = 2 * 3600
    assert plan(z, [short, long])[1] == {'short-ssd'}
    short.res_ssd_reserved_gb = 950
    assert plan(z, [short, long])[0] == []


@pytest.mark.parametrize('ssd_use,expected', [('no', {'ssd', 'plain'}),
    ('possible', {'plain'}), ('required', set())])
def test_ssd_possible_uses_the_existing_assignment_semantics(z, ssd_use, expected):
    ssd = engine(z, 'ssd')
    ssd.has_ssd, ssd.ssd_total_gb = True, 10
    plain = engine(z, 'plain')
    job(z, 'target', ssd_use=ssd_use, ssd_gb=20)
    pending = plan(z, [ssd, plain])[0]
    assert (pending[0][1] if pending else set()) == expected


@pytest.mark.parametrize('pool', ['dcache_download', 'dcache_upload', 's3_download'])
def test_unfit_long_request_does_not_use_backfills_only_transfer_slot(z, pool):
    e = engine(z, 'short', 12)
    setattr(z.jobs, pool + '_total', 1)
    slots = {pool + '_slots': 1}
    long = job(z, 'long', **slots)
    short = job(z, 'short', 1, **slots)
    later = job(z, 'later', 1, **slots)
    pending, keep, _, _ = plan(z, [e])
    assert [j for j, _ in pending] == [short]
    assert keep == {'short'}
    assert all(j.state == 'PENDING' for j in (long, short, later))
    assert getattr(z.jobs, pool + '_inuse') == 0


def test_requeued_jobs_remain_real_demand_without_reassigning_them(z):
    e = engine(z, 'pilot')
    j = job(z, 'target', 1, state='REQUEUED')
    assert plan(z, [e])[1] == {'pilot'} and j.state == 'REQUEUED'


def test_controller_capability_is_not_borrowed_from_a_different_pilot(z):
    short = engine(z, 'supported-short', 12)
    old = engine(z, 'unsupported-long', 120)
    old.chief_version = None
    j = job(z, 'controller', env={z.MONITOR_ROLE_ENV: 'controller'})
    assert plan(z, [short, old])[0] == []
    j.reqtime = 3600
    assert plan(z, [short, old])[1] == {'supported-short'}


@pytest.mark.parametrize('state', ['RUNNING', 'ASSIGNED'])
def test_canonical_busy_holdings_survive_missing_chief_telemetry(z, state):
    busy = engine(z, 'busy', 12)
    idle = engine(z, 'idle', 12)
    held = job(z, 'resident', state=state, node_id=busy.engine_id,
               held_ncpu=2.0, held_mem=500.0)
    assert not busy.jobs and busy.res_cpu_reserved == 0
    assert plan(z, [idle, busy])[1] == {'busy'}
    assert held.node_id == 'busy' and held.state == state


@pytest.mark.parametrize('reservation', ['jobs', 'res_cpu_reserved', 'res_mem_reserved_mb', 'res_ssd_reserved_gb'])
def test_busy_or_reserved_pilot_is_pinned_even_before_job_table_reconciliation(z, reservation):
    e = engine(z, 'reserved', 12)
    setattr(e, reservation, {'unreconciled-job'} if reservation == 'jobs' else 1)
    assert plan(z, [e])[1] == {'reserved'}


def test_busy_short_pilot_free_capacity_cannot_hide_long_idle_demand(z):
    busy = engine(z, 'busy-short', 12)
    long = engine(z, 'idle-long', 120)
    job(z, 'resident', state='RUNNING', node_id=busy.engine_id,
        held_ncpu=1, held_mem=1000)
    job(z, 'pending', cores=20)
    assert plan(z, [busy, long])[1] == {'busy-short', 'idle-long'}


def test_held_dynamic_lease_and_telemetry_are_maxed_not_double_counted(z):
    busy = engine(z, 'busy', cores=10, memory=10000)
    spare = engine(z, 'spare', cores=10, memory=10000)
    job(z, 'resident', state='RUNNING', node_id=busy.engine_id,
        cores=10, memory=10000, held_ncpu=2, held_mem=2000)
    busy.res_cpu_reserved, busy.res_mem_reserved_mb = 2, 2000
    job(z, 'backfill', cores=8, memory=8000)
    _, keep, cpu, mem = plan(z, [busy, spare], safety=1.0)
    assert keep == {'busy'} and (cpu, mem) == (10, 10000)


def test_already_pinned_capacity_precedes_idle_even_with_lower_keep_score(z):
    idle = engine(z, 'idle')
    busy = engine(z, 'busy')
    job(z, 'resident', state='ASSIGNED', node_id=busy.engine_id)
    job(z, 'pending', cores=10)
    assert plan(z, [idle, busy])[1] == {'busy'}


def test_configured_safety_fraction_and_minimum_are_still_retained(z):
    fleet = [engine(z, str(i), cores=10, memory=10000) for i in range(4)]
    job(z, 'target', cores=10, memory=10000)
    assert len(plan(z, fleet, safety=1.0)[1]) == 1
    assert len(plan(z, fleet, safety=1.05)[1]) == 2
    z.jobs.jobs_by_id.clear()
    assert plan(z, fleet, minimum=2)[1] == {'0', '1'}


def test_archive_runnable_queue_keeps_fifo_no_consolidation_semantics(z, monkeypatch):
    fleet = [engine(z, str(i), partition='archive') for i in range(3)]
    for e in fleet:
        e.status = z.PHASING_OUT
    j = job(z, 'stage', 1, partition='archive')
    j.submit_ts = z.time.time()
    assert len(plan(z, fleet)[0]) == 1  # Archive has no minimum queue age.
    iteration(z, monkeypatch)
    assert all(e.status == '' for e in fleet)
    assert j.state == 'PENDING' and z.status.consolidation_want['archive'] is None


@pytest.mark.parametrize('guard', ['hysteresis', 'cooldown', 'minimum-runtime'])
def test_controller_still_honors_phaseout_delays(z, monkeypatch, guard):
    e = engine(z, 'idle', 12)
    if guard == 'hysteresis':
        z.status.config['consolidation_hysteresis_sec'] = 2000
    elif guard == 'cooldown':
        z.status.config['consolidation_cooldown_sec'] = 2000
        z.status.consolidation_last_action_ts_by_part['compute'] = z.time.time()
    else:
        z.status.config['consolidation_min_runtime_sec'] = 20000
    iteration(z, monkeypatch)
    assert e.status != z.PHASING_OUT


def test_consolidation_does_not_override_automatic_end_of_life_drain(z, monkeypatch):
    e = engine(z, 'eol', 8)
    e.status, e.end_of_life_drain = z.PHASING_OUT, True
    job(z, 'resident', state='RUNNING', node_id=e.engine_id)
    assert plan(z, [e])[1] == {'eol'}
    iteration(z, monkeypatch)
    assert e.status == z.PHASING_OUT and e.end_of_life_drain

