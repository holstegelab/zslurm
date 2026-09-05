import importlib.machinery
import importlib.util
import io
import pathlib
import threading
import types
import unittest


ZSLURM_PATH = pathlib.Path(__file__).resolve().parents[1] / "zslurm"


def load_zslurm():
    loader = importlib.machinery.SourceFileLoader(
        "zslurm_memory_placement_under_test", str(ZSLURM_PATH)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class MemoryPlacementScoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zslurm = load_zslurm()
        cls.thin = (192.0, 336.0 * 1024.0)
        cls.fat = (192.0, 1440.0 * 1024.0)
        cls.profiles = [cls.thin, cls.fat]

    def score_on(self, profile, cores, mem_mb, priority=100, ssd=False):
        z = self.zslurm
        regret = z._placement_profile_regret(
            cores, mem_mb, profile[0], profile[1], self.profiles
        )
        return z._placement_score(
            priority,
            cores,
            mem_mb,
            profile[0],
            profile[1],
            profile[0],
            profile[1],
            profile_regret=regret,
            prefer_local_ssd=ssd,
        )

    def test_fat_node_prefers_kmer_shape_over_external_adapter(self):
        kmer = self.score_on(self.fat, 2.0, 38.0 * 1024.0)
        external_adapter = self.score_on(self.fat, 5.0, 14250.0)

        self.assertLess(kmer, external_adapter)
        self.assertEqual(kmer[4], 0.0)
        self.assertGreater(external_adapter[4], 0.0)

    def test_thin_node_prefers_align_shape_over_kmer(self):
        align = self.score_on(self.thin, 22.75, 40.0 * 1024.0)
        kmer = self.score_on(self.thin, 2.0, 38.0 * 1024.0)

        self.assertLess(align, kmer)
        self.assertEqual(align[4], 0.0)
        self.assertGreater(kmer[4], 0.0)

    def test_memory_overcommit_is_measured_in_core_equivalents(self):
        # This node has 1000 MB/core. The candidate exceeds free memory by
        # 10000 MB, which must therefore be a ten-core deficit (not 10/1024).
        score = self.zslurm._placement_score(
            100, 1.0, 11000.0, 10.0, 1000.0, 100.0, 100000.0
        )

        self.assertEqual(score[1], 1)
        self.assertAlmostEqual(score[2], 10.0)

    def test_fit_now_beats_nonfit_even_with_ssd_preference(self):
        fitting = self.zslurm._placement_score(
            100, 1.0, 1000.0, 1.0, 1000.0, 100.0, 100000.0
        )
        nonfitting_ssd = self.zslurm._placement_score(
            100,
            2.0,
            2000.0,
            1.0,
            1000.0,
            100.0,
            100000.0,
            prefer_local_ssd=True,
        )

        self.assertLess(fitting, nonfitting_ssd)

    def test_pipeline_priority_remains_strict(self):
        high_priority_nonfit = self.zslurm._placement_score(
            101, 2.0, 2000.0, 1.0, 1000.0, 100.0, 100000.0
        )
        low_priority_fit = self.zslurm._placement_score(
            100, 1.0, 1000.0, 1.0, 1000.0, 100.0, 100000.0
        )

        self.assertLess(high_priority_nonfit, low_priority_fit)

    def test_fractional_core_residual_is_not_rounded_up(self):
        score = self.zslurm._placement_score(
            100, 1.0, 1000.0, 1.25, 1250.0, 100.0, 100000.0
        )

        self.assertEqual(score[1], 0)
        self.assertAlmostEqual(score[5], 0.0)

    def test_ssd_preference_is_only_the_final_tiebreaker(self):
        plain = self.zslurm._placement_score(
            100, 1.0, 1000.0, 10.0, 10000.0, 100.0, 100000.0
        )
        preferred = self.zslurm._placement_score(
            100,
            1.0,
            1000.0,
            10.0,
            10000.0,
            100.0,
            100000.0,
            prefer_local_ssd=True,
        )

        self.assertEqual(plain[:-1], preferred[:-1])
        self.assertLess(preferred, plain)

    def test_no_ssd_job_preference_precedes_resource_packing(self):
        possible = self.zslurm._placement_score(
            100, 5.0, 5000.0, 10.0, 10000.0, 10.0, 10000.0
        )
        no_ssd = self.zslurm._placement_score(
            100,
            4.5,
            5000.0,
            10.0,
            10000.0,
            10.0,
            10000.0,
            prefer_no_ssd_job=True,
        )

        self.assertGreater(no_ssd[5], possible[5])
        self.assertLess(no_ssd, possible)

    def test_large_packing_advantage_beats_no_ssd_preference(self):
        well_packed_possible = self.zslurm._placement_score(
            100, 5.0, 5000.0, 10.0, 10000.0, 10.0, 10000.0
        )
        poorly_packed_no_ssd = self.zslurm._placement_score(
            100,
            3.5,
            5000.0,
            10.0,
            10000.0,
            10.0,
            10000.0,
            prefer_no_ssd_job=True,
        )

        self.assertLess(well_packed_possible, poorly_packed_no_ssd)

    def test_fitting_job_beats_nonfitting_no_ssd_preference(self):
        fitting_possible = self.zslurm._placement_score(
            100, 1.0, 1000.0, 1.0, 1000.0, 100.0, 100000.0
        )
        nonfitting_no_ssd = self.zslurm._placement_score(
            100,
            2.0,
            2000.0,
            1.0,
            1000.0,
            100.0,
            100000.0,
            prefer_no_ssd_job=True,
        )

        self.assertLess(fitting_possible, nonfitting_no_ssd)

    def test_higher_priority_still_beats_no_ssd_preference(self):
        higher_priority_possible = self.zslurm._placement_score(
            101, 1.0, 1000.0, 10.0, 10000.0, 100.0, 100000.0
        )
        lower_priority_no_ssd = self.zslurm._placement_score(
            100,
            1.0,
            1000.0,
            10.0,
            10000.0,
            100.0,
            100000.0,
            prefer_no_ssd_job=True,
        )

        self.assertLess(higher_priority_possible, lower_priority_no_ssd)

    def test_consolidation_uses_current_lease_for_running_jobs(self):
        job = types.SimpleNamespace(
            state="RUNNING",
            ncpu=24.0,
            mem=64000.0,
            held_ncpu=2.0,
            held_mem=12000.0,
        )

        self.assertEqual(
            self.zslurm._consolidation_job_resources(job),
            (2.0, 12000.0),
        )

    def test_consolidation_keeps_full_request_for_pending_jobs(self):
        job = types.SimpleNamespace(
            state="PENDING",
            ncpu=24.0,
            mem=64000.0,
            held_ncpu=2.0,
            held_mem=12000.0,
        )

        self.assertEqual(
            self.zslurm._consolidation_job_resources(job),
            (24.0, 64000.0),
        )

    def test_consolidation_supports_legacy_running_jobs_without_lease_fields(self):
        job = types.SimpleNamespace(state="RUNNING", ncpu=4.0, mem=8000.0)

        self.assertEqual(
            self.zslurm._consolidation_job_resources(job),
            (4.0, 8000.0),
        )

    def test_request_jobs_uses_profile_regret_before_ssd_preference(self):
        z = self.zslurm
        z.gb.lock = threading.RLock()
        z.gb.log_file = io.StringIO()
        z.status.config = {"job_walltime_buffer_sec": 0}
        z.status.lastin_first = False
        z.status.prio_fillmem_context = 20
        z.engines = z.EngineManager()
        z.engines.cluster_queued_by_partition = {}

        thin_engine = z.Engine(
            engine_id="thin-node",
            cores=self.thin[0],
            totmem=self.thin[1],
            partition="compute",
            cluster_id=False,
            managed=True,
        )
        fat_engine = z.Engine(
            engine_id="fat-node",
            cores=self.fat[0],
            totmem=self.fat[1],
            partition="compute",
            cluster_id=False,
            managed=True,
        )
        for engine in (thin_engine, fat_engine):
            engine.has_ssd = True
            engine.ssd_total_gb = 1800.0
            z.engines.engine_by_id[engine.engine_id] = engine

        jobs = z.JobManager()
        jobs.active_total = 100000
        jobs.dcache_total = 100000
        jobs.archive_total = 100000
        z.jobs = jobs

        kmer = z.Job(
            "kmer_sex_fused", "1", "true", "/tmp", {},
            2.0, 38.0 * 1024.0, 3600, 0, None,
            0, 0, 0, 0, 0, 0, "compute", 0, None, "",
            ssd_use="required", ssd_gb=1.0, priority=100,
        )
        external = z.Job(
            "external_adapter_fused", "2", "true", "/tmp", {},
            5.0, 14250.0, 3600, 0, None,
            0, 0, 0, 0, 0, 0, "compute", 0, None, "",
            ssd_use="possible", priority=100,
        )
        jobs.jobs_by_id[kmer.jobid] = kmer
        jobs.jobs_by_id[external.jobid] = external

        assigned = jobs.request_jobs(
            fat_engine.engine_id, 5.0, 40.0 * 1024.0, "compute"
        )

        self.assertEqual(assigned[0][0], kmer.jobid)

    def test_request_jobs_prefers_no_ssd_job_on_plain_engine(self):
        z = self.zslurm
        z.gb.lock = threading.RLock()
        z.gb.log_file = io.StringIO()
        z.status.config = {"job_walltime_buffer_sec": 0}
        z.status.lastin_first = False
        z.status.prio_fillmem_context = 20
        z.engines = z.EngineManager()
        z.engines.cluster_queued_by_partition = {}

        engine = z.Engine(
            engine_id="plain-node",
            cores=32.0,
            totmem=128000.0,
            partition="compute",
            cluster_id=False,
            managed=True,
        )
        engine.has_ssd = False
        z.engines.engine_by_id[engine.engine_id] = engine

        jobs = z.JobManager()
        jobs.active_total = 100000
        jobs.dcache_total = 100000
        jobs.archive_total = 100000
        z.jobs = jobs

        possible = z.Job(
            "possible", "1", "true", "/tmp", {},
            1.0, 1000.0, 3600, 0, None,
            0, 0, 0, 0, 0, 0, "compute", 0, None, "",
            ssd_use="possible", priority=100,
        )
        plain = z.Job(
            "plain", "2", "true", "/tmp", {},
            1.0, 1000.0, 3600, 0, None,
            0, 0, 0, 0, 0, 0, "compute", 0, None, "",
            ssd_use="no", priority=100,
        )
        jobs.jobs_by_id[possible.jobid] = possible
        jobs.jobs_by_id[plain.jobid] = plain

        assigned = jobs.request_jobs(
            engine.engine_id, 1.0, 1000.0, "compute"
        )

        self.assertEqual(assigned[0][0], plain.jobid)
        self.assertEqual(possible.state, "PENDING")


if __name__ == "__main__":
    unittest.main()
