import pytest

import zslurm_shared as shared


def test_explicit_missing_instance_never_falls_back(monkeypatch):
    monkeypatch.setattr(shared, 'get_instance_names', lambda: ['sole-manager'])
    assert shared.resolve_instance_name('missing') is None
    assert shared.resolve_instance_name('sole-manager') == 'sole-manager'
    assert shared.resolve_instance_name() == 'sole-manager'
    for get_url in (shared.get_job_url, shared.get_manager_url):
        with pytest.raises(KeyError, match='missing'):
            get_url('missing')


def test_ambiguous_omitted_instance_does_not_choose(monkeypatch):
    monkeypatch.setattr(shared, 'get_instance_names', lambda: ['one', 'two'])
    assert shared.resolve_instance_name() is None
    assert shared.resolve_instance_name('two') == 'two'
