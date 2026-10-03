"""Assignment must fit current free capacity, not merely the pilot's total."""
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import threading
import unittest


def load_zslurm():
    path = Path(__file__).parents[1]/'zslurm'
    loader = importlib.machinery.SourceFileLoader('zslurm_capacity_under_test', str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec);loader.exec_module(module)
    return module


class CurrentCapacityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.z = load_zslurm()

    def setUp(self):
        z = self.z
        z.gb.lock = threading.RLock();z.gb.log_file = io.StringIO()
        z.status.config = {'job_walltime_buffer_sec': 0}
        z.status.lastin_first = False;z.status.prio_fillmem_context = 20
        z.engines = z.EngineManager();z.engines.cluster_queued_by_partition = {}
        self.node = z.Engine('spider-node', 30, 220708, 'compute', cluster_id=False, managed=True)
        z.engines.engine_by_id[self.node.engine_id] = self.node
        self.jobs = z.JobManager();z.jobs = self.jobs
        self.jobs.active_total = self.jobs.dcache_total = self.jobs.archive_total = 100000
        self.jobs.configure_transfer_limits({'dcache_transfer_slots': 4})

    def add_job(self, jobid, cpu=8, mem=24000, priority=100, slots=0):
        job = self.z.Job('capacity-'+jobid, jobid, 'true', '/tmp', {}, cpu, mem, 3600,
            0, None, 0, 0, 0, 0, 0, 0, 'compute', 0, None, '',
            priority=priority, dcache_transfer_slots=slots)
        self.jobs.jobs_by_id[jobid] = job
        return job

    def request(self, cpu, mem):
        return self.jobs.request_jobs(self.node.engine_id, cpu, mem, 'compute')

    def running(self, jobid, cpu=8, mem=24000):
        job = self.add_job(jobid, cpu=cpu, mem=mem)
        job.assigned(self.node.engine_id)
        self.assertTrue(self.jobs.can_run_assigned_job(self.node.engine_id, jobid))
        self.z.engines.job_running(self.node.engine_id, job)
        return job

    def test_native_stale_chief_capacity_cannot_grant_fourth_eight_core_job(self):
        for i in range(3):
            self.running('running'+str(i))
        pending = self.add_job('pending', slots=1)
        self.assertEqual(self.request(14, 172708), [])
        self.assertEqual(pending.state, 'PENDING')
        self.assertEqual(self.jobs.dcache_download_inuse, 0)
        self.assertEqual(self.node.res_cpu_reserved, 24)

    def test_stale_chief_can_only_backfill_actual_remaining_capacity(self):
        for i in range(3):
            self.running('running'+str(i))
        self.add_job('small', cpu=2, mem=1000)
        too_large = self.add_job('large', cpu=8, mem=1000, priority=1000)
        self.assertEqual([r[0] for r in self.request(14, 172708)], ['small'])
        self.assertEqual(too_large.state, 'PENDING')

    def test_legacy_overbooked_pilot_receives_no_additional_grant(self):
        for i in range(4):
            job = self.add_job('running'+str(i))
            job.started(self.node.engine_id)
            self.z.engines.job_running(self.node.engine_id, job)
        self.node.res_cpu_reserved = 32
        self.node.res_mem_reserved_mb = 96000
        pending = self.add_job('pending', cpu=2, mem=8000)
        self.assertEqual(self.request(6, 148708), [])
        self.assertEqual(pending.state, 'PENDING')

    def test_authoritative_memory_holding_caps_stale_chief_report(self):
        self.node.totmem = 64000
        self.running('running', cpu=2, mem=60000)
        pending = self.add_job('pending', cpu=2, mem=8000)
        self.assertEqual(self.request(28, 64000), [])
        self.assertEqual(pending.state, 'PENDING')

    def test_pending_grants_count_before_any_start_acknowledgement(self):
        for i in range(4):
            self.add_job(str(i))
        first = self.request(30, 220708)
        replay = self.request(30, 220708)
        self.assertEqual(replay, first)
        self.assertEqual(self.jobs.jobs_by_id['3'].state, 'PENDING')
        self.assertEqual(sum(r[5] for r in replay), 24)

    def test_own_grant_replay_does_not_compete_with_new_jobs_for_same_slot(self):
        self.running('running', cpu=28, mem=1000)
        self.add_job('first', cpu=2, mem=1000, slots=1)
        initial = self.request(2, 1000)
        other = self.add_job('other', cpu=2, mem=1000, priority=1000)
        self.assertEqual(self.request(2, 1000), initial)
        self.assertEqual(other.state, 'PENDING')
        self.assertEqual(self.jobs.dcache_download_inuse, 1)

    def test_released_dynamic_lease_not_immutable_maximum_defines_headroom(self):
        job = self.running('running', cpu=24, mem=60000)
        resized = self.jobs.resize_running_job(self.node.engine_id, job.jobid, 2, 1000)
        self.assertTrue(resized['ok'])
        self.add_job('pending', cpu=24, mem=60000)
        self.assertEqual([r[0] for r in self.request(28, 219708)], ['pending'])

    def test_start_acknowledgement_rejects_unsafe_legacy_assignment_without_restart(self):
        for i in range(3):
            self.running('running'+str(i))
        pending = self.add_job('pending', slots=1)
        pending.assigned(self.node.engine_id)
        self.jobs._reserve_dcache_transfer_locked(pending)
        self.assertFalse(self.jobs.can_run_assigned_job(self.node.engine_id, pending.jobid))
        self.assertEqual(pending.state, 'PENDING')
        self.assertIsNone(pending.node_id)
        self.assertEqual(self.node.res_cpu_reserved, 24)
        self.assertEqual(self.jobs.dcache_download_inuse, 0)

    def test_resize_cannot_steal_capacity_from_an_unacknowledged_grant(self):
        job = self.running('running', cpu=24, mem=60000)
        self.assertTrue(self.jobs.resize_running_job(self.node.engine_id, job.jobid, 2, 1000)['ok'])
        self.add_job('pending', cpu=24, mem=60000)
        self.assertEqual([r[0] for r in self.request(28, 219708)], ['pending'])
        result = self.jobs.resize_running_job(self.node.engine_id, job.jobid, 24, 60000)
        self.assertFalse(result['ok'])
        self.assertEqual(job.held_ncpu, 2)

    def test_snellius_headroom_uses_existing_holding_and_fractional_grants(self):
        self.node.cores, self.node.totmem = 192, 336*1024
        self.running('running', cpu=160, mem=300000)
        self.add_job('small', cpu=22.75, mem=40000)
        self.add_job('large', cpu=40, mem=1000, priority=1000)
        self.assertEqual([r[0] for r in self.request(192, 336*1024)], ['small'])

    def test_eight_core_job_remains_unclaimed_with_only_six_free_cores(self):
        job = self.add_job('1', slots=1)
        self.assertEqual(self.request(6, 148708), [])
        self.assertEqual(job.state, 'PENDING')
        self.assertIsNone(job.node_id)
        self.assertEqual(self.jobs.dcache_download_inuse, 0)
        self.assertEqual(self.jobs.dcache_upload_inuse, 0)

    def test_fit_to_total_memory_is_not_fit_to_free_memory(self):
        job = self.add_job('1', mem=64000)
        self.assertEqual(self.request(8, 60000), [])
        self.assertEqual(job.state, 'PENDING')

    def test_thirty_core_pilot_never_grants_four_eight_core_jobs(self):
        tasks = [self.add_job(str(i)) for i in range(4)]
        assigned = self.request(30, 220708)
        self.assertEqual([row[0] for row in assigned], ['0', '1', '2'])
        self.assertLessEqual(sum(row[5] for row in assigned), 30)
        self.assertLessEqual(sum(row[6] for row in assigned), 220708)
        self.assertEqual(tasks[3].state, 'PENDING')

    def test_fitting_backfill_does_not_get_blocked_by_nonfitting_priority(self):
        high = self.add_job('high', priority=1000)
        lower = self.add_job('lower', cpu=4, priority=100)
        self.assertEqual([row[0] for row in self.request(6, 148708)], ['lower'])
        self.assertEqual(high.state, 'PENDING')
        self.assertEqual(lower.state, 'ASSIGNED')

    def test_fitting_job_outside_memory_packing_window_is_found(self):
        for i in range(40): self.add_job(str(i), priority=1000)
        self.add_job('fits', cpu=2, priority=100)
        self.assertEqual([row[0] for row in self.request(6, 148708)], ['fits'])

    def test_current_capacity_is_rechecked_after_each_grant(self):
        self.add_job('large', cpu=4, mem=5000, priority=1000)
        residual = self.add_job('residual', cpu=4, mem=5000, priority=900)
        self.add_job('small', cpu=2, mem=5000, priority=100)
        self.assertEqual([row[0] for row in self.request(6, 10000)], ['large', 'small'])
        self.assertEqual(residual.state, 'PENDING')

    def test_fractional_cpu_and_zero_capacity_cannot_be_rounded_up(self):
        self.add_job('1', cpu=1, mem=100)
        for cpu, mem in ((0.5, 1000), (0, 1000), (1, 0)):
            with self.subTest(cpu=cpu, mem=mem): self.assertEqual(self.request(cpu, mem), [])

    def test_unclaimed_job_can_start_on_another_available_pilot(self):
        job = self.add_job('1', slots=1)
        self.assertEqual(self.request(6, 148708), [])
        other = self.z.Engine('snellius-node', 192, 336*1024, 'compute', cluster_id=False, managed=True)
        self.z.engines.engine_by_id[other.engine_id] = other
        grant = self.jobs.request_jobs(other.engine_id, 192, 336*1024, 'compute')
        self.assertEqual([row[0] for row in grant], ['1'])
        self.assertEqual(job.node_id, other.engine_id)
        self.assertTrue(self.jobs.can_run_assigned_job(other.engine_id, job.jobid))
        self.assertEqual(job.state, 'RUNNING')
        self.assertEqual(self.jobs.dcache_download_inuse, 1)

    def test_lost_reply_replays_exact_fitting_grant_without_duplicate_slots(self):
        self.add_job('1', cpu=2, mem=1000, slots=1)
        initial = self.request(2, 1000)
        replay = self.request(2, 1000)
        self.assertEqual([row[0] for row in initial], ['1'])
        self.assertEqual(replay, initial)
        self.assertEqual(self.jobs.dcache_download_inuse, 1)

    def test_snellius_fractional_cpu_and_memory_bound_shapes_fit_current_slot(self):
        self.node.cores = 192
        self.node.totmem = 336*1024
        tasks = [self.add_job(str(i), cpu=22.75, mem=40000) for i in range(10)]
        grant = self.request(192, 336*1024)
        self.assertEqual(len(grant), 8)
        self.assertLessEqual(sum(row[5] for row in grant), 192)
        self.assertLessEqual(sum(row[6] for row in grant), 336*1024)
        self.assertTrue(all(job.state == 'PENDING' for job in tasks[8:]))


if __name__ == '__main__':
    unittest.main()
