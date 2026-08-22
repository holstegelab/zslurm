import importlib.machinery
import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_zsstats():
    loader = importlib.machinery.SourceFileLoader(
        "zsstats_leases_under_test", str(ROOT / "zsstats")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class LeaseAwareStatsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zsstats = load_zsstats()

    def test_dynamic_integrals_override_original_reservation(self):
        agg = self.zsstats.Agg()
        agg.add(
            {
                "retcode": "0",
                "runtime": "10",
                "cores_reserved": "24",
                "mem_reserved_mb": "64000",
                "avg_cpu_percentage": "8",
                "lease_reserved_core_seconds": "20",
                "lease_used_core_seconds": "10",
                "lease_reserved_mem_mb_seconds": "120000",
                "lease_pss_mb_seconds": "60000",
                "pss_avg_mb": "6000",
            }
        )

        result = agg.finalize()
        self.assertEqual(result["effective_reserved_cores"], 2.0)
        self.assertEqual(result["used_core_hours"], 10.0 / 3600.0)
        self.assertEqual(result["cpu_efficiency"], 0.5)
        self.assertEqual(result["effective_reserved_mem_mb"], 12000.0)
        self.assertEqual(result["used_pss_gib_hours"], 60000.0 / 1024.0 / 3600.0)
        self.assertEqual(result["memory_efficiency"], 0.5)

    def test_old_report_uses_jobwide_fallbacks(self):
        agg = self.zsstats.Agg()
        agg.add(
            {
                "retcode": "0",
                "runtime": "10",
                "cores_reserved": "4",
                "mem_reserved_mb": "8000",
                "avg_cpu_percentage": "2",
                "memory_over_time": "1000;3000",
            }
        )

        result = agg.finalize()
        self.assertEqual(result["effective_reserved_cores"], 4.0)
        self.assertEqual(result["cpu_efficiency"], 0.5)
        self.assertEqual(result["effective_reserved_mem_mb"], 8000.0)
        self.assertEqual(result["pss_mb_mean"], 2000.0)
        self.assertEqual(result["memory_efficiency"], 0.25)


if __name__ == "__main__":
    unittest.main()
