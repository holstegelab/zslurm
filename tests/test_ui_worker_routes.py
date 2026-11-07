"""Interactive engine-start routing: worker type first, then a local partition."""
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def module():
    loader = importlib.machinery.SourceFileLoader('zslurm_ui_routes_under_test', str(ROOT / 'zslurm'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    result = importlib.util.module_from_spec(spec)
    loader.exec_module(result)
    return result


@pytest.fixture
def site(module, monkeypatch):
    def apply(name, discovered=None, **config):
        monkeypatch.setattr(module, 'status', module.Status())
        monkeypatch.setattr(module, '_discover_slurm_partitions', lambda: discovered)
        module._apply_cluster_config(dict(config, cluster_site=name), discover_partitions=True)
        return module
    return apply


def test_snellius_lists_each_worker_type_with_its_slurm_partitions(site):
    z = site('snellius')
    assert z.worker_slurm_partitions('compute') == ['genoa', 'rome', 'fat_genoa', 'fat_rome']
    assert z.worker_slurm_partitions('archive') == ['staging']
    assert z.worker_type_prompt() == 'Worker type (compute/archive/gpu) [compute]: '
    assert z.slurm_partition_prompt('compute', z.worker_slurm_partitions('compute'), 53) == \
        'Partition genoa/rome/fat_genoa/fat_rome [genoa]: '


def test_spider_offers_no_archive_workers(site):
    z = site('spider')
    assert z.worker_slurm_partitions('archive') == []
    assert 'archive' not in z.worker_type_prompt()
    assert z.worker_types_shown() == ['compute', 'gpu']
    # Existing archive work stays visible even without a route.
    assert z.worker_types_shown(['archive']) == ['compute', 'archive', 'gpu']
    with pytest.raises(ValueError, match='No archive worker partition'):
        z.select_worker_route('archive')


def test_route_selection_checks_partition_belongs_to_type(site):
    z = site('snellius')
    assert z.select_worker_route('', '') == ('genoa', 'compute')
    assert z.select_worker_route('c', 'rome') == ('rome', 'compute')
    assert z.select_worker_route('staging') == ('staging', 'archive')
    with pytest.raises(ValueError, match='staging is a archive partition, not compute'):
        z.select_worker_route('compute', 'staging')
    # Former single prompt: a Slurm partition typed directly still works.
    assert z.select_worker_route('fat_genoa') == ('fat_genoa', 'compute')


def test_discovery_hides_partitions_slurm_does_not_have(site):
    z = site('snellius', discovered=({'genoa', 'staging'}, set()))
    assert z.worker_slurm_partitions('compute') == ['genoa']
    assert z.worker_slurm_partitions('gpu') == []
    assert z.worker_type_prompt() == 'Worker type (compute/archive) [compute]: '


def test_gpu_route_uses_profiled_partition(site):
    z = site('spider', node_profiles={
        'normal': {'cores': 30, 'mem_gb': 240},
        'gpu_a100_22c': {'cores': 44, 'mem_gb': 320, 'gpus': 2, 'gpu_type': 'a100'}})
    assert z.worker_slurm_partitions('gpu') == ['gpu_a100_22c']
    assert z.select_worker_route('gpu') == ('gpu_a100_22c', 'gpu')


def test_long_partition_list_falls_back_to_log(module):
    prompt = module.slurm_partition_prompt('compute', ['a' * 30, 'b' * 30], 53)
    assert prompt == 'compute partition [' + 'a' * 30 + '], choices in log: '


def test_transfer_slots_use_status_pane_style(module):
    assert module.format_slot_usage(0, 2) == '0 / 2'
    assert module.format_slot_usage(0.5, 2.0) == '0.5 / 2'


def test_site_gpu_profiles_and_autogrow_cap_default_on(site):
    z = site('spider')
    profile = z.status.node_profiles['gpu_a100_22c']
    assert (profile['cores'], profile['gpus'], profile['has_ssd']) == (44, 2, True)
    # Matches the chief's 73 GiB/core scratch cap on a 44-core worker.
    assert profile['ssd_gb'] <= 73 * 44
    assert z.worker_slurm_partitions('gpu') == ['gpu_a100_22c']
    z = site('snellius')
    assert z.status.node_profiles['gpu_a100']['gpus'] == 4
    assert not z.status.node_profiles['gpu_a100']['has_ssd']


def test_autogrow_cap_default_and_explicit_zero(module, monkeypatch):
    monkeypatch.setattr(module, 'status', module.Status())
    monkeypatch.setattr(module, '_discover_slurm_partitions', lambda: None)
    for site_name, cap in (('spider', 2), ('snellius', 1), ('generic', 0)):
        config = {'cluster_site': site_name}
        module._apply_cluster_config(config)
        assert config['autogrow_max_gpu_nodes'] == cap
    config = {'cluster_site': 'spider', 'autogrow_max_gpu_nodes': 0}
    module._apply_cluster_config(config)
    assert config['autogrow_max_gpu_nodes'] == 0


def test_own_node_profiles_keep_site_gpu_profile(site):
    z = site('spider', node_profiles={'normal': {'cores': 20, 'mem_gb': 160}})
    assert z.status.node_profiles['normal']['cores'] == 20
    assert 'gpu_a100_22c' in z.status.node_profiles
    z = site('spider', node_profiles={'gpu_a100_22c': {'cores': 22, 'mem_gb': 160, 'gpus': 1}})
    assert z.status.node_profiles['gpu_a100_22c']['cores'] == 22


def test_generic_gpu_route_without_profile_is_not_offered(site):
    z = site('generic', gpu_partition='gpu_x')
    assert z.worker_slurm_partitions('gpu') == []
    assert 'gpu' not in z.worker_type_prompt()


def _submit(z, partition, ngpus=None):
    return z.jobs.submit_job('job', ['true'], '/', {}, 1, 1024, 60, 0, '', 0, 0, 0, 0, 0, 0,
                             partition, 0, '', ngpus=ngpus)


def test_unroutable_submissions_are_rejected(site, module, monkeypatch):
    monkeypatch.setattr(module, 'jobs', module.JobManager())
    monkeypatch.setattr(module, 'engines', module.EngineManager())
    monkeypatch.setattr(module, 'add_log_line', lambda *args, **kwargs: None)
    z = site('spider')
    with pytest.raises(ValueError, match='No archive workers'):
        _submit(z, 'archive')
    assert _submit(z, 'gpu') and _submit(z, 'compute')
    z = site('generic', gpu_partition=None)
    with pytest.raises(ValueError, match='No GPU workers'):
        _submit(z, 'gpu')
    assert _submit(site('snellius'), 'archive')


def test_hand_started_worker_keeps_its_route_open(site, module, monkeypatch):
    monkeypatch.setattr(module, 'jobs', module.JobManager())
    monkeypatch.setattr(module, 'engines', module.EngineManager())
    monkeypatch.setattr(module, 'add_log_line', lambda *args, **kwargs: None)
    z = site('spider')
    z.engines.engine_by_id['e1'] = type('E', (), {'partition': 'archive'})()
    assert z.job_route_available('archive')
    assert _submit(z, 'archive')
