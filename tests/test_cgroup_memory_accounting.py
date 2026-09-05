import unittest

import zslurm_shared


class CgroupMemoryAccountingTests(unittest.TestCase):
    def test_excludes_file_cache_but_keeps_shared_and_other_memory(self):
        # current = anon 300 + file 500 + kernel/other 100. Of file, 100 is
        # shmem and must remain; only the other 400 is ordinary page cache.
        stats = {"anon": 300, "file": 500, "shmem": 100, "kernel": 100}

        self.assertEqual(
            zslurm_shared.cgroup_memory_without_page_cache(900, stats),
            500,
        )

    def test_missing_shmem_counter_keeps_full_charge_conservatively(self):
        self.assertEqual(
            zslurm_shared.cgroup_memory_without_page_cache(
                900, {"file": 500}
            ),
            900,
        )

    def test_inconsistent_stat_cannot_make_usage_negative(self):
        self.assertEqual(
            zslurm_shared.cgroup_memory_without_page_cache(
                100, {"file": 500, "shmem": 0}
            ),
            0,
        )

    def test_v1_total_counters_are_supported_when_shmem_is_available(self):
        self.assertEqual(
            zslurm_shared.cgroup_memory_without_page_cache(
                1000,
                {
                    "cache": 200,
                    "shmem": 50,
                    "total_cache": 600,
                    "total_shmem": 150,
                },
            ),
            550,
        )

    def test_scheduler_availability_is_not_reduced_by_page_cache(self):
        # Raw cgroup headroom is only 100, but 400 of the current charge is
        # reclaimable page cache. The scheduler must therefore see 500 free.
        self.assertEqual(
            zslurm_shared.cgroup_memory_available_without_page_cache(
                1000, 900, {"file": 500, "shmem": 100}
            ),
            500,
        )

    def test_memory_stat_parser_ignores_malformed_and_negative_values(self):
        parsed = zslurm_shared.parse_cgroup_memory_stat(
            "anon 100\nfile 500\nshmem 75\nbroken\nnegative -20\n"
        )

        self.assertEqual(
            parsed,
            {"anon": 100.0, "file": 500.0, "shmem": 75.0, "negative": 0.0},
        )


if __name__ == "__main__":
    unittest.main()
