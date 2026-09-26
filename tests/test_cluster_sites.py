import importlib.machinery
import importlib.util
import pathlib
import unittest
from unittest import mock

import yaml


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
        self.assertEqual(defaults["staging_partition"], "__disabled__")
        self.assertTrue(defaults["enable_ssd_prompt"])
        self.assertFalse(defaults["enable_feature_prompt"])
        self.assertFalse(defaults["gpfs_io_enable"])
        self.assertEqual(defaults["default_engine_cores"], 30)
        self.assertEqual(defaults["autogrow_engine_cores"], 30)
        self.assertTrue(defaults["autogrow_dynamic_engine_cores"])
        self.assertFalse(defaults["autogrow_require_idle_nodes"])
        self.assertEqual(defaults["autogrow_engine_min_cores"], 2)
        self.assertEqual(defaults["autogrow_engine_memory_mb_per_core"], 8000.0)
        self.assertEqual(defaults["autogrow_max_nonssd_fraction"], 1.0)
        self.assertEqual(
            defaults["autogrow_prefer_partitions"], [("normal", True)]
        )
        self.assertEqual(
            defaults["node_profiles"]["normal"],
            {"cores": 30, "mem_gb": 240},
        )
        self.assertTrue(defaults["autogrow_enable"])
        self.assertEqual(defaults["autogrow_max_compute_nodes"], 15)
        self.assertFalse(defaults["maintenance_window_enable"])

    def test_spider_site_file_enables_bounded_ssd_autogrow(self):
        path = pathlib.Path(__file__).resolve().parents[1] / "config/sites/spider.yaml"
        config = yaml.safe_load(path.read_text(encoding="utf-8"))

        self.assertTrue(config["enable_ssd_prompt"])
        self.assertEqual(config["ssd_feature_name"], "ssd")
        self.assertTrue(config["scratch_use_tmpdir"])
        self.assertEqual(config["scratch_capacity_gb_per_core"], 73)
        self.assertTrue(config["autogrow_enable"])
        self.assertEqual(config["autogrow_max_compute_nodes"], 15)

    def test_snellius_defaults_preserve_exclusive_engines(self):
        defaults = self.zslurm._cluster_defaults(
            {"cluster_site": "snellius"}
        )

        self.assertEqual(defaults["default_partition"], "genoa")
        self.assertTrue(defaults["enable_ssd_prompt"])
        self.assertTrue(defaults["enable_feature_prompt"])
        self.assertTrue(defaults["gpfs_io_enable"])
        self.assertEqual(defaults["default_engine_cores"], 0)
        self.assertEqual(defaults["autogrow_engine_cores"], 0)
        self.assertFalse(defaults["autogrow_dynamic_engine_cores"])
        self.assertTrue(defaults["autogrow_require_idle_nodes"])
        self.assertTrue(defaults["autogrow_enable"])
        self.assertTrue(defaults["maintenance_window_enable"])
        self.assertEqual(defaults["autogrow_max_nonssd_fraction"], 0.5)
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
            "autogrow_dynamic_engine_cores": False,
            "autogrow_require_idle_nodes": True,
            "autogrow_max_nonssd_fraction": 0.75,
            "enable_feature_prompt": False,
            "gpfs_io_enable": False,
            "autogrow_enable": True,
            "autogrow_max_compute_nodes": 3,
        }
        self.zslurm._apply_cluster_config(config)

        self.assertEqual(self.zslurm.status.default_engine_cores, 12)
        self.assertEqual(self.zslurm.status.autogrow_engine_cores, 16)
        self.assertFalse(self.zslurm.status.autogrow_dynamic_engine_cores)
        self.assertTrue(self.zslurm.status.autogrow_require_idle_nodes)
        self.assertEqual(self.zslurm.status.autogrow_max_nonssd_fraction, 0.75)
        self.assertFalse(self.zslurm.status.enable_feature_prompt)
        self.assertFalse(self.zslurm.status.gpfs_io_enable)
        self.assertTrue(self.zslurm.status.autogrow_enable)
        self.assertEqual(config["autogrow_max_compute_nodes"], 3)

    def test_spider_status_hides_inapplicable_gpfs_value(self):
        label, value = self.zslurm._cpu_io_display(
            73.4, [11.0, 17.0], gpfs_io_enable=False
        )

        self.assertEqual(label, "Host CPU (%): ")
        self.assertEqual(value, "73")

    def test_snellius_status_keeps_gpfs_value(self):
        label, value = self.zslurm._cpu_io_display(
            73.4, [11.0, 17.0], gpfs_io_enable=True
        )

        self.assertEqual(label, "CPU/GPFS (%): ")
        self.assertEqual(value, "73 | 14")


if __name__ == "__main__":
    unittest.main()
