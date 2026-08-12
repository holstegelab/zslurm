import importlib.machinery
import importlib.util
import io
import pathlib
import threading
import time
import unittest


ZSLURM_PATH = pathlib.Path(__file__).resolve().parents[1] / "zslurm"


def load_zslurm():
    loader = importlib.machinery.SourceFileLoader(
        "zslurm_dependencies_under_test", str(ZSLURM_PATH)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class JobDependencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zslurm = load_zslurm()

    def setUp(self):
        z = self.zslurm
        z.gb.lock = threading.RLock()
        z.gb.log_file = io.StringIO()
        z.status.config = {"job_walltime_buffer_sec": 0}
        z.status.lastin_first = False
        z.status.prio_fillmem_context = 20
        z.engines = z.EngineManager()
        z.engines.cluster_queued_by_partition = {}
        self.engine = z.Engine(
            engine_id="dependency-node",
            cores=32,
            totmem=128000,
            partition="compute",
            cluster_id=False,
            managed=True,
        )
        z.engines.engine_by_id[self.engine.engine_id] = self.engine
        self.jobs = z.JobManager()
        self.jobs.active_total = 100000
        self.jobs.dcache_total = 100000
        self.jobs.archive_total = 100000
        z.jobs = self.jobs

    def submit(
        self, name="job", dependency=None, requeue=0, owner="alice",
        active_use_add=0,
    ):
        jobid = self.jobs.submit_job(
            job_name=name,
            cmd="true",
            cwd="/tmp",
            env={"USER": owner},
            ncpu=1,
            mem=100,
            reqtime=60,
            requeue=requeue,
            dependency=dependency,
            arch_use_add=0,
            arch_use_remove=0,
            dcache_use_add=0,
            dcache_use_remove=0,
            active_use_add=active_use_add,
            active_use_remove=0,
            partition="compute",
            info_input_mb=0,
            info_output_file=None,
            owner=owner,
        )
        return self.jobs.jobs_by_id[jobid]

    def dispatch(self, cores=32):
        return self.jobs.request_jobs(
            self.engine.engine_id, cores, 128000, "compute"
        )

    def dependency_state(self, job):
        return self.jobs.dependency_status(job)["state"]

    def test_parser_supports_and_or_delay_and_bare_afterany(self):
        parse = self.zslurm.parse_job_dependency
        parsed = parse("afterok:10:11,afterany:12")
        self.assertEqual(parsed["mode"], "all")
        self.assertEqual(len(parsed["conditions"]), 3)

        parsed = parse("afterok:10?afternotok:11")
        self.assertEqual(parsed["mode"], "any")
        parsed = parse("after:10+5")
        self.assertEqual(parsed["conditions"][0]["delay_seconds"], 300)
        self.assertEqual(parse("10")["conditions"][0]["type"], "afterany")

    def test_invalid_or_unknown_dependency_rejects_submission_atomically(self):
        before_counter = self.jobs.jobid_counter
        before_total = dict(self.jobs.total_jobs)
        for dependency in (
            "afterok:99999999",
            "afterok:1,afterany:2?afternotok:3",
            "expand:1",
            "afterok:not-a-job",
        ):
            with self.subTest(dependency=dependency):
                with self.assertRaises(ValueError):
                    self.submit(dependency=dependency)
        self.assertEqual(self.jobs.jobid_counter, before_counter)
        self.assertEqual(self.jobs.total_jobs, before_total)
        self.assertEqual(len(self.jobs.jobs_by_id), 0)

    def test_afterok_waits_for_success_and_then_dispatches(self):
        parent = self.submit("parent")
        child = self.submit("child", "afterok:%s" % parent.jobid)

        assigned = self.dispatch()
        self.assertEqual([row[0] for row in assigned], [parent.jobid])
        self.assertEqual(child.state, "PENDING")
        self.assertEqual(self.dependency_state(child), "waiting")

        self.jobs.job_done(parent.jobid, self.zslurm.RC_SUCCESS)
        assigned = self.dispatch()
        self.assertEqual([row[0] for row in assigned], [child.jobid])
        self.assertEqual(self.dependency_state(child), "satisfied")

    def test_afterok_failure_is_never_satisfied_and_reserves_nothing(self):
        parent = self.submit("parent")
        child = self.submit(
            "child", "afterok:%s" % parent.jobid, active_use_add=5
        )
        self.jobs.job_done(parent.jobid, 1)

        self.assertEqual(self.dependency_state(child), "never_satisfied")
        self.assertEqual(self.dispatch(), [])
        self.assertEqual(child.state, "PENDING")
        self.assertEqual(self.jobs.active_inuse, 0)
        detailed = self.jobs.list_jobs_detailed(states=["PENDING"])[0]
        self.assertEqual(detailed["dependency"], "afterok:%s" % parent.jobid)
        self.assertEqual(detailed["dependency_state"], "never_satisfied")
        self.assertIn("did not complete successfully", detailed["dependency_reason"])

    def test_afterany_and_afternotok_terminal_semantics(self):
        failed_parent = self.submit("failed-parent")
        afterany = self.submit(
            "after-any", "afterany:%s" % failed_parent.jobid
        )
        afternotok = self.submit(
            "after-failure", "afternotok:%s" % failed_parent.jobid
        )
        self.jobs.job_done(failed_parent.jobid, 7)
        self.assertEqual(self.dependency_state(afterany), "satisfied")
        self.assertEqual(self.dependency_state(afternotok), "satisfied")

        successful_parent = self.submit("successful-parent")
        wrong_branch = self.submit(
            "wrong-branch", "afternotok:%s" % successful_parent.jobid
        )
        self.jobs.job_done(successful_parent.jobid, self.zslurm.RC_SUCCESS)
        self.assertEqual(
            self.dependency_state(wrong_branch), "never_satisfied"
        )

    def test_or_releases_on_one_match_and_and_latches_failure(self):
        first = self.submit("first")
        second = self.submit("second")
        either = self.submit(
            "either",
            "afterok:%s?afterok:%s" % (first.jobid, second.jobid),
        )
        both = self.submit(
            "both",
            "afterok:%s,afterok:%s" % (first.jobid, second.jobid),
        )

        self.jobs.job_done(first.jobid, 1)
        self.assertEqual(self.dependency_state(either), "waiting")
        self.assertEqual(self.dependency_state(both), "never_satisfied")
        self.jobs.job_done(second.jobid, self.zslurm.RC_SUCCESS)
        self.assertEqual(self.dependency_state(either), "satisfied")
        self.assertEqual(self.dependency_state(both), "never_satisfied")

    def test_after_releases_on_start_and_honours_delay(self):
        parent = self.submit("parent")
        child = self.submit("child", "after:%s+1" % parent.jobid)
        parent.state = "ASSIGNED"
        self.assertEqual(self.dependency_state(child), "waiting")

        parent.state = "RUNNING"
        parent.starttime = time.time()
        parent.first_starttime = parent.starttime
        self.assertEqual(self.dependency_state(child), "waiting")
        parent.first_starttime -= 61
        self.assertEqual(self.dependency_state(child), "satisfied")

    def test_requeue_is_not_terminal_for_completion_dependencies(self):
        parent = self.submit("parent", requeue=1)
        child = self.submit("child", "afterany:%s" % parent.jobid)

        self.jobs.job_done(parent.jobid, 1)
        self.assertEqual(parent.state, "REQUEUED")
        self.assertEqual(self.dependency_state(child), "waiting")
        self.jobs.job_done(parent.jobid, self.zslurm.RC_SUCCESS)
        self.assertEqual(self.dependency_state(child), "satisfied")

    def test_singleton_captures_only_earlier_same_name_and_owner_jobs(self):
        first = self.submit("same-name", owner="alice")
        second = self.submit("same-name", dependency="singleton", owner="alice")
        other_owner = self.submit(
            "same-name", dependency="singleton", owner="bob"
        )
        later = self.submit("same-name", owner="alice")

        self.assertEqual(self.dependency_state(second), "waiting")
        self.assertEqual(self.dependency_state(other_owner), "satisfied")
        captured = second.dependency_spec["conditions"][0]["jobids"]
        self.assertEqual(captured, [first.jobid])
        self.assertNotIn(later.jobid, captured)

        self.jobs.job_done(first.jobid, 9)
        self.assertEqual(self.dependency_state(second), "satisfied")

    def test_recent_terminal_job_can_be_used_at_submission(self):
        parent = self.submit("parent")
        parent_id = parent.jobid
        self.jobs.job_done(parent_id, self.zslurm.RC_SUCCESS)

        child = self.submit("child", "afterok:%s" % parent_id)
        self.assertEqual(self.dependency_state(child), "satisfied")

    def test_status_counts_dependency_blocked_jobs(self):
        parent = self.submit("parent")
        waiting = self.submit(
            "waiting", "afterany:%s" % parent.jobid, active_use_add=5
        )
        impossible_parent = self.submit("impossible-parent")
        impossible = self.submit(
            "impossible", "afterok:%s" % impossible_parent.jobid,
            active_use_add=7,
        )
        self.jobs.job_done(impossible_parent.jobid, 3)

        status = self.jobs.get_status_json()
        self.assertEqual(status["dependency_waiting_jobs"], 1)
        self.assertEqual(status["dependency_never_satisfied_jobs"], 1)
        self.assertEqual(status["budgeted_pending_jobs"], 0)
        self.assertEqual(status["budgets"]["active"]["pending_add"], 0)
        self.assertEqual(self.dependency_state(waiting), "waiting")
        self.assertEqual(self.dependency_state(impossible), "never_satisfied")

    def test_eligible_counts_and_autogrow_ignore_dependency_blocked_jobs(self):
        parent = self.submit("parent")
        self.submit("child", "afterany:%s" % parent.jobid)

        _total, _running, eligible, _failed, _done = self.jobs.get_job_stats()
        self.assertEqual(eligible, {"compute": 1})
        plan = self.zslurm.compute_autogrow_plan([self.engine], "compute")
        self.assertEqual(plan["eligible_n"], 1)


if __name__ == "__main__":
    unittest.main()
