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
