import unittest

import zslurm_shared


class SpiderOccupancyTests(unittest.TestCase):
    def test_configured_ssd_feature_and_mixed_cpu_capacity(self):
        nodes = [
            {
                "partitions": ["normal"], "state": "MIXED",
                "features": {"amd", "ssd"}, "cpu_alloc": 30,
                "cpu_tot": 60,
            },
            {
                "partitions": ["normal"], "state": "IDLE",
                "features": {"amd", "ssd"}, "cpu_alloc": 0,
                "cpu_tot": 120,
            },
        ]
        parts = zslurm_shared._collect_states_by_scratch(nodes, "ssd")
        ssd = parts["normal"]["scratch"]

        self.assertEqual(ssd["states"], {"MIXED": 1, "IDLE": 1})
        self.assertEqual(ssd["free_cpus"], 150)
        self.assertEqual(ssd["free_cpus_by_node"], [30, 120])
        self.assertEqual(parts["normal"]["no_scratch"]["nodes"], 0)


if __name__ == "__main__":
    unittest.main()
