"""Runtime-aware growth without replacing the per-worker placement checks."""

import importlib.machinery
import importlib.util
import io
import pathlib
import random
import threading
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_zslurm():
    loader = importlib.machinery.SourceFileLoader(
        "zslurm_runtime_autogrow_under_test", str(ROOT / "zslurm")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class RuntimeAutogrowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zslurm = load_zslurm()

    def setUp(self):
        z = self.zslurm
        z.gb.lock = threading.RLock()
        z.gb.log_file = io.StringIO()
        z._apply_cluster_config({"cluster_site": "spider"})
        z.status.autogrow_runtime_default = "5-00:00:00"
        z.status.job_walltime_buffer_sec = 900.0
        z.engines = z.EngineManager()
        self.jobs = z.JobManager()
        self.jobs.active_total = 100000
        self.jobs.dcache_total = 100000
        self.jobs.archive_total = 100000
        z.jobs = self.jobs

    def add_job(self, name="controller", seconds=48 * 3600, cores=2,
                mem_mb=8000, ssd_use="no", transfer_slots=0):
        z = self.zslurm
        job = z.Job(
            name, name, "true", "/tmp", {}, cores, mem_mb, seconds, 0, None,
            0, 0, 0, 0, 0, 0, "compute", 0, None, "",
            ssd_use=ssd_use, dcache_transfer_slots=transfer_slots,
        )
        self.jobs.jobs_by_id[job.jobid] = job
        return job

    def pilot(self, seconds=43 * 3600, **kwargs):
        fields = dict(
            partition="compute", cores=30, totmem=220708,
            res_cpu_reserved=0, res_mem_reserved_mb=0, timeleft=seconds,
            has_ssd=False, stopping=False, status="RUNNING", managed=True,
        )
        fields.update(kwargs)
        return types.SimpleNamespace(**fields)

    def queued_pilot(self, seconds=5 * 86400, cores=2, mem_mb=14628,
                     scratch=False):
        z = self.zslurm
        part = z.status.default_partition
        z.engines.cluster_queued_by_partition = {part: 1}
        z.engines.queued_engines_detail = [("queued", part, 60.0, scratch)]
        z.engines.queued_engine_capacity_by_cid = {"queued": (cores, mem_mb)}
        z.engines.engine_by_clusterid["queued"] = self.pilot(seconds)

    def plan(self, fleet=()):
        availability = {
            part: {
                "scratch": {"states": {"IDLE": 10}},
                "no_scratch": {"states": {"IDLE": 10}},
            }
            for part in self.zslurm.status.node_profiles
        }
        with mock.patch.object(
            self.zslurm.zslurm_shared,
            "slurm_partition_state_counts_by_scratch",
            return_value=availability,
        ):
            return self.zslurm.compute_autogrow_plan(list(fleet), "compute")

    def test_expiring_free_pilot_cannot_mask_long_controller(self):
        self.add_job()
        plan = self.plan([self.pilot()])
        self.assertEqual(plan["best_nodes"], 1)
        self.assertEqual(plan["engine_cores"], 2)
        self.assertEqual(plan["runtime_residual"]["cpu"], 2)
        self.assertEqual(plan["runtime_residual"]["mem_mb"], 8000)

    def test_snellius_grows_exclusive_pilot_for_same_runtime_mismatch(self):
        self.zslurm._apply_cluster_config({"cluster_site": "snellius"})
        self.add_job()
        plan = self.plan([self.pilot()])
        self.assertEqual(plan["best_nodes"], 1)
        self.assertEqual(plan["engine_cores"], 0)

    def test_fresh_running_capacity_prevents_growth_on_both_sites(self):
        for site in ("spider", "snellius"):
            with self.subTest(site=site):
                self.zslurm._apply_cluster_config({"cluster_site": site})
                self.add_job()
                plan = self.plan([self.pilot(5 * 86400)])
                self.assertEqual(plan["plan_nodes"], 0)
                self.assertEqual(plan["runtime_residual"]["cpu"], 0)

    def test_queued_fresh_partial_pilot_prevents_duplicate_growth(self):
        self.add_job()
        self.queued_pilot()
        self.assertEqual(self.plan([self.pilot()])["plan_nodes"], 0)

    def test_queued_short_pilot_does_not_mask_long_demand(self):
        self.add_job()
        self.queued_pilot(seconds=86400)
        self.assertEqual(self.plan([self.pilot()])["best_nodes"], 1)

    def test_queued_memory_is_not_inflated_by_running_capacity_margin(self):
        self.add_job(mem_mb=15000)
        self.queued_pilot()
        plan = self.plan([self.pilot()])
        self.assertEqual(plan["best_nodes"], 1)
        self.assertEqual(plan["runtime_residual"]["mem_mb"], 372)

    def test_runtime_cutoff_uses_same_configured_margin_as_assignment(self):
        self.add_job(seconds=3600)
        self.zslurm.status.config["job_walltime_buffer_sec"] = 1800
        self.assertEqual(self.plan([self.pilot(5399)])["best_nodes"], 1)
        self.assertEqual(self.plan([self.pilot(5400)])["plan_nodes"], 0)

    def test_reserved_capacity_is_not_credited_twice(self):
        self.add_job()
        plan = self.plan([self.pilot(
            5 * 86400, res_cpu_reserved=29, res_mem_reserved_mb=220000,
        )])
        self.assertEqual(plan["best_nodes"], 1)
        self.assertAlmostEqual(plan["runtime_residual"]["cpu"], 0.9)
        self.assertAlmostEqual(plan["runtime_residual"]["mem_mb"], 7221.2)

    def test_stopping_unmanaged_or_other_partition_cannot_mask_demand(self):
        for attributes in (
            {"stopping": True},
            {"cluster_id": "123", "managed": False},
            {"partition": "archive"},
        ):
            with self.subTest(attributes=attributes):
                self.add_job()
                self.assertEqual(
                    self.plan([self.pilot(5 * 86400, **attributes)])["best_nodes"], 1
                )

    def test_draining_pilot_with_no_backfill_cannot_mask_demand(self):
        self.add_job(seconds=3600)
        pilot = self.pilot(5 * 86400, status=self.zslurm.PHASING_OUT, jobs=[])
        self.assertEqual(self.plan([pilot])["best_nodes"], 1)

    def test_safe_draining_window_still_counts_short_backfill(self):
        self.add_job(seconds=3600)
        pilot = self.pilot(5 * 86400, status=self.zslurm.PHASING_OUT)
        with mock.patch.object(self.zslurm, "phaseout_backfill_window", return_value=7200):
            self.assertEqual(self.plan([pilot])["plan_nodes"], 0)
        self.jobs.jobs_by_id.clear()
        self.add_job(seconds=7200)
        with mock.patch.object(self.zslurm, "phaseout_backfill_window", return_value=7200):
            self.assertEqual(self.plan([pilot])["best_nodes"], 1)

    def test_long_ssd_demand_needs_long_scratch_capacity(self):
        self.add_job(ssd_use="required")
        fleet = [self.pilot(5 * 86400), self.pilot(has_ssd=True)]
        plan = self.plan(fleet)
        self.assertEqual(plan["best_nodes"], 1)
        self.assertTrue(plan["plan_use_scratch"])
        self.assertEqual(plan["runtime_residual"]["ssd_cpu"], 2)
        self.queued_pilot(scratch=True)
        self.assertEqual(self.plan(fleet)["plan_nodes"], 0)

    def test_plain_queued_capacity_never_covers_ssd_only_runtime_demand(self):
        self.add_job(ssd_use="required")
        self.queued_pilot(scratch=False)
        plan = self.plan([self.pilot(has_ssd=True)])
        self.assertEqual(plan["best_nodes"], 1)
        self.assertTrue(plan["plan_use_scratch"])

    def test_over_default_runtime_stays_pending_without_empty_growth(self):
        job = self.add_job(seconds=5 * 86400)
        plan = self.plan()
        self.assertEqual(plan["best_nodes"], 0)
        self.assertEqual(plan["unserviceable_runtime_n"], 1)
        self.assertIn("exceeds", plan["plan_reason"])
        self.assertEqual(job.state, "PENDING")
        self.assertIs(self.jobs.jobs_by_id[job.jobid], job)

    def test_unserviceable_job_does_not_exhaust_transfer_slots_for_valid_job(self):
        self.jobs.configure_transfer_limits({"dcache_transfer_slots": 1})
        self.add_job("too-long", seconds=5 * 86400, transfer_slots=1)
        self.add_job("valid", transfer_slots=1)
        plan = self.plan()
        self.assertEqual(plan["best_nodes"], 1)
        self.assertEqual(plan["eligible_n"], 1)
        self.assertEqual(plan["unserviceable_runtime_n"], 1)
        self.assertEqual(self.jobs.dcache_download_inuse, 0)

    def test_empty_or_already_running_work_does_not_provision_pilots(self):
        self.assertEqual(self.plan().get("best_nodes", 0), 0)
        self.add_job().state = "RUNNING"
        self.assertEqual(self.plan().get("best_nodes", 0), 0)

    def test_duration_prefixes_are_not_summed_or_double_counted(self):
        z = self.zslurm
        jobs = [self.add_job("long", seconds=7200, cores=4, mem_mb=400),
                self.add_job("short", seconds=3600, cores=8, mem_mb=800)]
        caps = [(9000, 2, 200, False, True), (6000, 4, 400, False, True)]
        residual = z._autogrow_runtime_residual(jobs, caps, 900)
        self.assertEqual(residual["cpu"], 6)
        self.assertEqual(residual["mem_mb"], 600)
        self.assertEqual(residual["ssd_cpu"], 0)

    def test_duration_event_sweep_matches_exhaustive_prefix_oracle(self):
        rng = random.Random(20261001)
        z = self.zslurm
        for trial in range(100):
            jobs = [types.SimpleNamespace(
                reqtime=rng.randrange(1, 20) * 3600,
                ncpu=rng.randrange(1, 10), mem=rng.randrange(1, 10000),
                ssd_use=rng.choice(["no", "required"]),
            ) for _ in range(rng.randrange(1, 30))]
            caps = [(
                rng.randrange(1, 25) * 3600, rng.randrange(1, 30),
                rng.randrange(1, 30000), rng.choice([False, True]),
                rng.choice([False, True]),
            ) for _ in range(rng.randrange(0, 10))]

            def oracle(ssd_only):
                pending = [j for j in jobs if not ssd_only or j.ssd_use == "required"]
                fleet = [c for c in caps if not ssd_only or c[3]]
                result = [0.0, 0.0]
                for cutoff in {j.reqtime + 900 for j in pending}:
                    demand = [j for j in pending if j.reqtime + 900 >= cutoff]
                    fitting = [c for c in fleet if c[0] >= cutoff]
                    for index, field in ((1, "ncpu"), (2, "mem")):
                        supply = sum(c[index] * (1 if ssd_only or c[4] else 1.1)
                                     for c in fitting)
                        result[index - 1] = max(
                            result[index - 1], sum(getattr(j, field) for j in demand) - supply
                        )
                return result

            general, ssd = oracle(False), oracle(True)
            actual = z._autogrow_runtime_residual(jobs, caps, 900)
            with self.subTest(trial=trial):
                self.assertAlmostEqual(actual["cpu"], general[0])
                self.assertAlmostEqual(actual["mem_mb"], general[1])
                self.assertAlmostEqual(actual["ssd_cpu"], ssd[0])
                self.assertAlmostEqual(actual["ssd_mem_mb"], ssd[1])


if __name__ == "__main__":
    unittest.main()
