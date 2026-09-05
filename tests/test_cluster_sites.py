import importlib.machinery
import importlib.util
import pathlib
import unittest
from unittest import mock


ZSLURM_PATH = pathlib.Path(__file__).resolve().parents[1] / "zslurm"


def load_zslurm():
    loader = importlib.machinery.SourceFileLoader(
        "zslurm_cluster_sites_under_test", str(ZSLURM_PATH)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ClusterSiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zslurm = load_zslurm()

    def test_spider_defaults_are_partial_and_core_memory_balanced(self):
        defaults = self.zslurm._cluster_defaults({"cluster_site": "spider"})

        self.assertEqual(defaults["default_partition"], "normal")
        self.assertEqual(defaults["default_engine_cores"], 30)
        self.assertEqual(defaults["autogrow_engine_cores"], 30)
        self.assertEqual(
            defaults["node_profiles"]["normal"],
            {"cores": 30, "mem_gb": 240},
        )
        self.assertFalse(defaults["autogrow_enable"])
        self.assertFalse(defaults["maintenance_window_enable"])

    def test_snellius_defaults_preserve_exclusive_engines(self):
        defaults = self.zslurm._cluster_defaults(
            {"cluster_site": "snellius"}
        )

        self.assertEqual(defaults["default_partition"], "genoa")
        self.assertEqual(defaults["default_engine_cores"], 0)
        self.assertEqual(defaults["autogrow_engine_cores"], 0)
        self.assertTrue(defaults["autogrow_enable"])
        self.assertTrue(defaults["maintenance_window_enable"])
        self.assertIn("fat_genoa", defaults["node_profiles"])

    def test_site_is_detected_from_fqdn(self):
        with mock.patch.object(
            self.zslurm.zslurm_shared,
            "get_full_hostname",
            return_value="ui-02.spider.surfsara.nl",
        ):
            self.assertEqual(
                self.zslurm._cluster_defaults()["cluster_site"], "spider"
            )

    def test_explicit_config_overrides_site_defaults(self):
        config = {
            "cluster_site": "spider",
            "default_engine_cores": 12,
            "autogrow_engine_cores": 16,
            "autogrow_enable": True,
            "autogrow_max_compute_nodes": 3,
        }
        self.zslurm._apply_cluster_config(config)

        self.assertEqual(self.zslurm.status.default_engine_cores, 12)
        self.assertEqual(self.zslurm.status.autogrow_engine_cores, 16)
        self.assertTrue(self.zslurm.status.autogrow_enable)
        self.assertEqual(config["autogrow_max_compute_nodes"], 3)


if __name__ == "__main__":
    unittest.main()
