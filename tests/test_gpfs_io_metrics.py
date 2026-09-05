import ast
import math
import os
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CHIEF_PATH = ROOT / "zslurm_chief"


def load_selected_nodes(*names):
    tree = ast.parse(CHIEF_PATH.read_text(encoding="utf-8"))
    selected = [
        node
        for node in tree.body
        if (
            isinstance(node, (ast.FunctionDef, ast.ClassDef))
            and node.name in names
        )
    ]
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    namespace = {"math": math, "os": os}
    exec(compile(module, str(CHIEF_PATH), "exec"), namespace)
    return namespace


def make_port(root, device, rate="100 Gb/sec (2X HDR)", state="4: ACTIVE"):
    port = root / device / "ports" / "1"
    counters = port / "counters"
    counters.mkdir(parents=True)
    (port / "rate").write_text(rate + "\n", encoding="utf-8")
    (port / "state").write_text(state + "\n", encoding="utf-8")
    (counters / "port_rcv_data").write_text("1000\n", encoding="utf-8")
    (counters / "port_xmit_data").write_text("2000\n", encoding="utf-8")
    return port


class GpfsIoMetricTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace = load_selected_nodes(
            "parse_link_rate_bps",
            "gpfs_rdma_port_path",
            "detect_gpfs_rdma_port",
            "GpfsIoMonitor",
        )

    def test_link_rate_parser(self):
        parse = self.namespace["parse_link_rate_bps"]

        self.assertEqual(parse("100 Gb/sec (2X HDR)"), 100e9)
        self.assertEqual(parse("25 Gb/sec (1X EDR)"), 25e9)
        self.assertIsNone(parse("unknown"))

    def test_compute_and_archive_choose_snellius_gpfs_ports(self):
        detect = self.namespace["detect_gpfs_rdma_port"]
        with tempfile.TemporaryDirectory(prefix="zslurm-gpfs-sysfs.") as tmp:
            root = pathlib.Path(tmp)
            archive = make_port(root, "mlx5_0", rate="25 Gb/sec (1X EDR)")
            compute = make_port(root, "mlx5_2")

            self.assertEqual(detect("compute", {}, str(root)), str(compute))
            self.assertEqual(detect("archive", {}, str(root)), str(archive))

    def test_configured_port_overrides_default(self):
        detect = self.namespace["detect_gpfs_rdma_port"]
        with tempfile.TemporaryDirectory(prefix="zslurm-gpfs-sysfs.") as tmp:
            root = pathlib.Path(tmp)
            custom = make_port(root, "mlx5_7")

            self.assertEqual(
                detect(
                    "compute",
                    {"gpfs_rdma_port": "mlx5_7/1"},
                    str(root),
                ),
                str(custom),
            )

    def test_rdma_counter_delta_becomes_throughput_and_utilization(self):
        monitor_class = self.namespace["GpfsIoMonitor"]
        with tempfile.TemporaryDirectory(prefix="zslurm-gpfs-sysfs.") as tmp:
            port = make_port(pathlib.Path(tmp), "mlx5_2")
            times = iter((10.0, 12.0))
            monitor = monitor_class(port, clock=lambda: next(times))

            first = monitor.sample()
            self.assertTrue(first["available"])
            self.assertEqual(first["io_pct"], 0.0)

            # 2.5 GB received in two seconds is 1.25 GB/s, or 10% of 100 Gb/s.
            (port / "counters" / "port_rcv_data").write_text(
                str(1000 + 625_000_000) + "\n", encoding="utf-8")
            (port / "counters" / "port_xmit_data").write_text(
                str(2000 + 125_000_000) + "\n", encoding="utf-8")
            sample = monitor.sample()

            self.assertAlmostEqual(sample["rx_mib_s"], 1.25e9 / 2**20)
            self.assertAlmostEqual(sample["tx_mib_s"], 0.25e9 / 2**20)
            self.assertAlmostEqual(sample["io_pct"], 10.0)

    def test_counter_reset_does_not_create_a_spike(self):
        monitor_class = self.namespace["GpfsIoMonitor"]
        with tempfile.TemporaryDirectory(prefix="zslurm-gpfs-sysfs.") as tmp:
            port = make_port(pathlib.Path(tmp), "mlx5_2")
            times = iter((10.0, 12.0))
            monitor = monitor_class(port, clock=lambda: next(times))
            monitor.sample()
            (port / "counters" / "port_rcv_data").write_text(
                "1\n", encoding="utf-8")
            (port / "counters" / "port_xmit_data").write_text(
                "1\n", encoding="utf-8")

            sample = monitor.sample()

            self.assertTrue(sample["available"])
            self.assertEqual(sample["io_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()
