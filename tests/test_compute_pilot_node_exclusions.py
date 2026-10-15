"""Explicit physical-node exclusions are set at pilot submission time."""

import importlib.machinery
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import xmlrpc.client

import pytest


ZSLURM_PATH = Path(__file__).resolve().parents[1] / "zslurm"


def load_zslurm():
    loader = importlib.machinery.SourceFileLoader(
        "zslurm_compute_exclusions_under_test", str(ZSLURM_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def z(monkeypatch):
    module = load_zslurm()
    monkeypatch.setattr(module, "status", module.Status())
    monkeypatch.setattr(module, "jobs", module.JobManager())
    monkeypatch.setattr(module, "engines", module.EngineManager())
    monkeypatch.setattr(module, "add_log_line", lambda *args: None)
    monkeypatch.setattr(module, "set_status_message", lambda *args: None)
    monkeypatch.setattr(module, "_prepare_engine_runtime", lambda: "/pinned/slurm_to_zslurm")
    monkeypatch.setattr(module, "_engine_submission_environment", lambda: {})
    monkeypatch.setattr(module.time, "sleep", lambda *args: None)
    module.gb.log_file = io.StringIO()
    module.status.instance_name = "node-exclusion-test"
    module.status.address = "127.0.0.1"
    module.status.port = 12345
    return module


def configure(z, **options):
    config = {"cluster_site": "spider", **options}
    z.status.config = dict(config)
    z._apply_cluster_config(config)


def submit(z, monkeypatch, partition, *, n=1, cores=2, floor=None):
    commands = []

    def fake_popen(args, **kwargs):
        commands.append(tuple(args))
        return SimpleNamespace(communicate=lambda: (b"12345\n", b""))

    monkeypatch.setattr(z, "Popen", fake_popen)
    started = z.engines.start_slurm(
        z.gb, n, "5-00:00:00", partition, None, cores,
        minimum_runtime_seconds=floor)
    assert len(commands) == 1
    assert commands[0][0] == "sbatch"
    assert len(started) == n
    return commands[0]


def test_default_is_empty_on_spider_and_snellius(z, monkeypatch):
    for site, partition, cores in (
            ("spider", "normal", 2), ("snellius", "genoa", 0)):
        config = {"cluster_site": site}
        z.status.config = config
        z._apply_cluster_config(config)
        assert z.status.compute_pilot_exclude_nodes == ()
        assert not any(arg.startswith("--exclude=") for arg in
                       submit(z, monkeypatch, partition, cores=cores))


@pytest.mark.parametrize("n,cores,floor", [
    (1, 2, None),                 # manual/normal pilot
    (3, 30, 49 * 3600),          # autogrow array with workload-aware floor
])
def test_configured_spider_compute_exclusion_is_in_original_sbatch(
        z, monkeypatch, n, cores, floor):
    configure(z, compute_pilot_exclude_nodes=["wn-la-11"])
    assert z.health()["compute_pilot_node_exclusions"] is True
    assert z.health()["compute_pilot_exclude_nodes"] == ["wn-la-11"]
    assert z.get_status_json()["scheduler"]["compute_pilot_exclude_nodes"] == ["wn-la-11"]
    command = submit(z, monkeypatch, "normal", n=n, cores=cores, floor=floor)
    assert command.count("--exclude=wn-la-11") == 1
    assert not any(arg == "scontrol" for arg in command)
    if n > 1:
        assert "--array=0-2" in command
        assert "--time-min" in command


def test_staging_retains_its_dynamic_exclusion_without_compute_policy(
        z, monkeypatch):
    configure(z, staging_partition="staging",
              compute_pilot_exclude_nodes=["wn-la-11"])
    z.engines.archive_failed_nodes.add("wn-staging-02")
    z.engines.engine_by_id["archive99"] = z.Engine(partition="archive")
    command = submit(z, monkeypatch, "staging")
    assert "--exclude=archive99,wn-staging-02" in command
    assert "wn-la-11" not in ",".join(command)


def test_gpu_pilot_is_not_rewritten_by_compute_policy(z, monkeypatch):
    configure(z, compute_pilot_exclude_nodes=["wn-la-11"],
              node_profiles={"normal": {"cores": 30, "mem_gb": 240},
                             "gpu_a100_22c": {"cores": 44, "mem_gb": 320,
                                               "gpus": 2, "gpu_type": "a100"}})
    command = submit(z, monkeypatch, "gpu", cores=0)
    assert "--gres=gpu:a100:2" in command
    assert not any(arg.startswith("--exclude=") for arg in command)


def test_yaml_policy_survives_fresh_manager_config_load(z, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "cluster_site: spider\ncompute_pilot_exclude_nodes:\n  - wn-la-11\n",
        encoding="utf-8")
    for _ in range(2):
        z.status = z.Status()
        config = z.zslurm_shared.get_config(config_path=str(config_path))
        z.status.config = config
        z._apply_cluster_config(config)
        assert z.status.compute_pilot_exclude_nodes == ("wn-la-11",)


def test_legacy_source_handover_keeps_new_target_policy_from_persistent_yaml(
        z, tmp_path, monkeypatch):
    config_path = tmp_path / "legacy-spider-config.yaml"
    config_path.write_text(
        "port: 38864\nreports_file: report.tsv\nrpcpath: legacy\n"
        "compute_pilot_exclude_nodes: [wn-la-11]\n",
        encoding="utf-8")
    monkeypatch.setattr(z.zslurm_shared, "get_full_hostname",
                        lambda: "wn-la-04.spider.surfsara.nl")
    # This is the exact source shape before the exclusion feature existed.
    source_snapshot = {
        "schema_version": z.HANDOVER_SCHEMA_VERSION,
        "target_manager_uuid": "target-uuid",
        "handover_id": "old-source-transfer",
        "source": {"instance": "old-source", "manager_uuid": "source-uuid"},
        "source_instance_names": ["old-source"],
        "scheduler": {"autogrow_enable": True,
                      "autogrow_max_compute_nodes": 15},
        "jobs": [], "engines": [],
    }
    assert "compute_pilot_exclude_nodes" not in source_snapshot["scheduler"]
    z.status = z.Status()
    target_config = z.zslurm_shared.get_config(config_path=str(config_path))
    z.status.config = target_config
    z._apply_cluster_config(target_config)
    assert z.status.cluster_site == "spider"
    z.status.instance_name = "target"
    z.status.manager_uuid = "target-uuid"
    z.status.handover_secret = "secret"
    assert z.health()["compute_pilot_exclude_nodes"] == ["wn-la-11"]
    result = z.receive_handover("secret", "old-source-transfer", source_snapshot)
    assert result["ok"] is True
    assert z.status.handover_mode == "TARGET_IMPORTED"
    assert z.health()["compute_pilot_exclude_nodes"] == ["wn-la-11"]
    assert z.status.config["compute_pilot_exclude_nodes"] == ["wn-la-11"]


def test_exclusion_survives_serialized_manager_handover(z, monkeypatch):
    configure(z, compute_pilot_exclude_nodes=["wn-la-11"])
    z.status.manager_uuid = "source-uuid"
    pending = z.Engine(cluster_id="41521997_0", partition="compute",
                       slurm_partition="normal", managed=True,
                       instance="old-source")
    pending.slurm_state = "PD"
    z.engines.engine_by_clusterid[pending.cluster_id] = pending
    z.engines.last_observed_cids = {pending.cluster_id}
    monkeypatch.setattr(z, "_manager_descriptor", lambda *args, **kwargs: {
        "instance": "source", "manager_uuid": "source-uuid"})
    monkeypatch.setattr(z, "_instance_names_owned_by", lambda *args: [])
    snapshot = z._build_handover_snapshot(
        "transfer", {"manager_uuid": "target-uuid"})
    snapshot = xmlrpc.client.loads(
        xmlrpc.client.dumps((snapshot,), allow_none=True))[0][0]
    assert snapshot["scheduler"]["compute_pilot_exclude_nodes"] == ["wn-la-11"]

    z.jobs = z.JobManager()
    z.engines = z.EngineManager()
    z.status = z.Status()
    configure(z)  # Target starts empty even if its local config differs.
    z.status.instance_name = "target"
    z._apply_handover_snapshot(snapshot)
    assert z.status.compute_pilot_exclude_nodes == ("wn-la-11",)
    assert z.status.config["compute_pilot_exclude_nodes"] == ["wn-la-11"]
    assert z.health()["compute_pilot_exclude_nodes"] == ["wn-la-11"]
    assert z.health()["list_pilot_allocations"] is True
    registry = z.list_pilot_allocations()
    assert registry["manager_uuid"] == z.status.manager_uuid
    assert "old-source" in registry["owned_instance_names"]
    assert registry["allocations"] == [{
        "cluster_id": "41521997_0", "slurm_job_id": "41521997_0",
        "partition": "compute", "slurm_partition": "normal",
        "slurm_state": "PD", "instance": "old-source",
        "managed": True, "observed_in_last_squeue": False,
    }]
    assert "--exclude=wn-la-11" in submit(z, monkeypatch, "normal")


def test_registry_lists_queued_arrays_and_running_pilots_without_slurm_mutation(
        z, monkeypatch):
    configure(z, compute_pilot_exclude_nodes=["wn-la-11"])
    z.status.manager_uuid = "manager-uuid"
    z.status.handover_owned_aliases = {"former-instance"}
    for cid, state, kind in (("41521997_0", "PD", "compute"),
                             ("41521997_1", "PD", "compute"),
                             ("41508463", "R", "compute"),
                             ("41500000", "PD", "gpu")):
        pilot = z.Engine(cluster_id=cid, partition=kind,
                         slurm_partition="normal" if kind == "compute" else "gpu",
                         managed=True, instance="former-instance")
        pilot.slurm_state = state
        z.engines.engine_by_clusterid[cid] = pilot
    z.engines.last_observed_cids = {"41521997_0", "41521997_1", "41508463"}
    with mock.patch.object(z, "Popen") as popen:
        registry = z.list_pilot_allocations()
        popen.assert_not_called()
    assert registry["ok"] and registry["schema_version"] == 1
    assert registry["instance"] == z.status.instance_name
    assert registry["owned_instance_names"] == ["former-instance", "node-exclusion-test"]
    assert [(row["cluster_id"], row["slurm_state"], row["partition"])
            for row in registry["allocations"]] == [
                ("41500000", "PD", "gpu"),
                ("41508463", "R", "compute"),
                ("41521997_0", "PD", "compute"),
                ("41521997_1", "PD", "compute"),
            ]
    assert registry["allocations"][-1]["observed_in_last_squeue"] is True


@pytest.mark.parametrize("value", [
    "wn-la-11,,wn-la-12", "wn-la-11;--partition=evil", "--nodelist=evil",
    "wn-la-[01-03]", "wn-la-11\nother", ["wn-la-11", 12],
    {"node": "wn-la-11"}, list(map(str, range(65))),
])
def test_invalid_exclusion_is_rejected_at_config_load(z, value):
    with pytest.raises(ValueError, match="compute_pilot_exclude_nodes"):
        z._apply_cluster_config({"cluster_site": "spider",
                                 "compute_pilot_exclude_nodes": value})


def test_corrupted_runtime_policy_is_rejected_before_sbatch(z, monkeypatch):
    configure(z, compute_pilot_exclude_nodes=["wn-la-11"])
    z.status.compute_pilot_exclude_nodes = ["wn-la-11;bad"]
    with mock.patch.object(z, "Popen") as popen:
        with pytest.raises(ValueError, match="compute_pilot_exclude_nodes"):
            z.engines.start_slurm(z.gb, 1, "1-00:00:00", "normal", None, 2)
        popen.assert_not_called()


@pytest.mark.parametrize("entry", ["main", "main_headless"])
def test_invalid_startup_policy_fails_before_instance_registration(
        z, monkeypatch, entry):
    monkeypatch.setattr(z.zslurm_shared, "get_config", lambda **kwargs: {
        "compute_pilot_exclude_nodes": "wn-la-11;bad"})
    with mock.patch.object(z, "_ensure_unique_instance") as register:
        with pytest.raises(ValueError, match="compute_pilot_exclude_nodes"):
            if entry == "main":
                z.main(None)
            else:
                z.main_headless(SimpleNamespace())
        register.assert_not_called()


def test_invalid_handover_policy_does_not_change_target_state(z):
    z.status.manager_uuid = "target-uuid"
    z.status.handover_secret = "secret"
    snapshot = {
        "schema_version": z.HANDOVER_SCHEMA_VERSION,
        "target_manager_uuid": "target-uuid",
        "scheduler": {"compute_pilot_exclude_nodes": ["wn-la-11;bad"]},
    }
    result = z.receive_handover("secret", "transfer", snapshot)
    assert result["ok"] is False and result["code"] == 3
    assert z.status.handover_mode == "ACTIVE"
    assert z.status.compute_pilot_exclude_nodes == ()
