import importlib.machinery
import importlib.util
import pathlib
import threading
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
ZSLURM_PATH = ROOT / "zslurm"


def load_zslurm():
    loader = importlib.machinery.SourceFileLoader(
        "zslurm_priority_overrides_under_test", str(ZSLURM_PATH)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FolderPriorityOverrideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zslurm = load_zslurm()

    def setUp(self):
        z = self.zslurm
        z.gb.lock = threading.RLock()
        z.gb.log_file = None
        z.status.lastin_first = False
        z.status.prio_fillmem_context = 500
        z.status.autogrow_enable = True
        z.status.autoconsolidate_enable = True
        z.status.config = {"autogrow_max_compute_nodes": 40}
        z.status.instance_name = "source"
        z.status.manager_uuid = "source-uuid"
        z.status.handover_source_instance = None
        z.status.handover_owned_aliases = set()
        z.jobs = z.JobManager()
        z.engines = z.EngineManager()
        self.jobs = z.jobs

    def add_job(self, jobid, name, cwd, priority=100):
        z = self.zslurm
        job = z.Job(
            name, jobid, "true", cwd, {}, 1, 1000, 3600, 0, None,
            0, 0, 0, 0, 0, 0, "compute", 0, None, "",
            priority=priority,
        )
        self.jobs.jobs_by_id[jobid] = job
        return job

    def submit_job(self, name, cwd, priority):
        jobid = self.jobs.submit_job(
            name, "true", cwd, {}, 1, 1000, 3600, 0, None,
            0, 0, 0, 0, 0, 0, "compute", 0, None,
            priority=priority,
        )
        return self.jobs.jobs_by_id[str(jobid)]

    def test_folder_component_matches_without_prefix_collision(self):
        fus1 = self.add_job(
            "1", "align", "/work/exome_runs/FUS1", priority=70
        )
        fus10 = self.add_job(
            "2", "align", "/work/exome_runs/FUS10", priority=70
        )
        named = self.add_job(
            "3", "FUS1_special", "/work/exome_runs/OTHER", priority=70
        )

        self.assertEqual(
            {row[0] for row in self.jobs.match_jobs("FUS1")},
            {fus1.jobid, named.jobid},
        )
        self.assertEqual(
            {row[0] for row in self.jobs.match_jobs("FUS1", cwd_only=True)},
            {fus1.jobid},
        )
        self.assertNotIn(
            fus10.jobid,
            {row[0] for row in self.jobs.match_jobs("FUS1")},
        )

    def test_one_shot_prioritize_accepts_folder_but_stays_in_band(self):
        first = self.add_job("1", "align", "/work/OTHER", priority=70)
        selected = self.add_job("2", "align", "/work/FUS1", priority=70)

        self.jobs.prioritize("FUS1")

        self.assertEqual(
            [job.jobid for job in self.jobs._jobs_in_priority_order(False)],
            [selected.jobid, first.jobid],
        )
        self.assertEqual(selected.priority, 70)

    def test_active_override_changes_existing_and_future_jobs(self):
        existing = self.add_job(
            "1", "align", "/work/exome_runs/FUS1", priority=70
        )
        other = self.add_job(
            "2", "align", "/work/exome_runs/FBS", priority=90
        )

        result = self.jobs.set_cwd_priority_override("FUS1", 101)
        future = self.submit_job(
            "markdup", "/work/exome_runs/FUS1", priority=70
        )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(existing.priority, 101)
        self.assertEqual(existing.base_priority, 70)
        self.assertEqual(existing.priority_override_pattern, "FUS1")
        self.assertEqual(future.priority, 101)
        self.assertEqual(future.base_priority, 70)
        self.assertEqual(other.priority, 90)

        cleared = self.jobs.clear_cwd_priority_override("FUS1")
        self.assertEqual(cleared["previous_priority"], 101)
        self.assertEqual(existing.priority, 70)
        self.assertEqual(future.priority, 70)
        self.assertIsNone(existing.priority_override_pattern)

    def test_later_overlapping_rule_wins_and_clear_recomputes(self):
        job = self.add_job(
            "1", "align", "/work/projectmine2/b2a", priority=50
        )
        self.jobs.set_cwd_priority_override("projectmine2", 80)
        self.jobs.set_cwd_priority_override("projectmine2/b2a", 120)
        self.assertEqual(job.priority, 120)

        self.jobs.clear_cwd_priority_override("projectmine2/b2a")
        self.assertEqual(job.priority, 80)
        self.assertEqual(job.priority_override_pattern, "projectmine2")

    def test_status_reports_active_overrides(self):
        self.add_job("1", "align", "/work/FUS1", priority=70)
        self.jobs.set_cwd_priority_override("FUS1", 101)

        status = self.jobs.get_status_json()

        self.assertEqual(status["scheduler"]["cwd_priority_overrides"], [{
            "pattern": "FUS1",
            "priority": 101,
            "matching_active_jobs": 1,
            "matching_waiting_jobs": 1,
        }])

    def test_active_override_survives_live_handover(self):
        z = self.zslurm
        existing = self.add_job(
            "1", "align", "/work/exome_runs/FUS1", priority=70
        )
        self.jobs.set_cwd_priority_override("FUS1", 101)
        self.assertEqual(existing.priority, 101)

        descriptor = {"instance": "target", "manager_uuid": "target-uuid"}
        source_descriptor = {
            "instance": "source", "manager_uuid": "source-uuid"
        }
        with mock.patch.object(
            z, "_manager_descriptor", return_value=source_descriptor
        ), mock.patch.object(
            z, "_instance_names_owned_by", return_value=["source"]
        ):
            snapshot = z._build_handover_snapshot("handover-1", descriptor)

        z.jobs = z.JobManager()
        z.engines = z.EngineManager()
        z.status.instance_name = "target"
        z._apply_handover_snapshot(snapshot)
        self.jobs = z.jobs

        imported = self.jobs.jobs_by_id["1"]
        future = self.submit_job(
            "markdup", "/work/exome_runs/FUS1", priority=70
        )
        self.assertEqual(imported.base_priority, 70)
        self.assertEqual(imported.priority, 101)
        self.assertEqual(future.priority, 101)
        self.assertEqual(
            self.jobs.list_cwd_priority_overrides()[0]["pattern"], "FUS1"
        )


if __name__ == "__main__":
    unittest.main()
