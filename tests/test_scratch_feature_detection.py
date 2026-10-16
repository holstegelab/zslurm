import unittest

import zslurm_shared


class ScratchFeatureDetectionTests(unittest.TestCase):
    def test_site_feature_name_controls_scratch_classification(self):
        nodes = [{
            "partitions": ["normal"],
            "state": "IDLE",
            "features": {"amd", "ssd", "ssd12T"},
        }]

        spider = zslurm_shared._collect_states_by_scratch(
            nodes, feature_name="ssd"
        )
        snellius = zslurm_shared._collect_states_by_scratch(
            nodes, feature_name="scratch-node"
        )

        self.assertEqual(spider["normal"]["scratch"]["nodes"], 1)
        self.assertEqual(spider["normal"]["no_scratch"]["nodes"], 0)
        self.assertEqual(snellius["normal"]["scratch"]["nodes"], 0)
        self.assertEqual(snellius["normal"]["no_scratch"]["nodes"], 1)


if __name__ == "__main__":
    unittest.main()
