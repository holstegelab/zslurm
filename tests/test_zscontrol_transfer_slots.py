import importlib.machinery
import importlib.util
import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
ZSCONTROL_PATH = ROOT / "zscontrol"


def load_zscontrol():
    loader = importlib.machinery.SourceFileLoader(
        "zscontrol_under_test", str(ZSCONTROL_PATH)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeProxy:
    def __init__(self):
        self.calls = []

    def set_transfer_limits(self, *args):
        self.calls.append(args)
        return {"ok": True}


class TransferSlotCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zscontrol = load_zscontrol()

    def invoke(self, *arguments):
        proxy = FakeProxy()
        argv = ["zscontrol", "transfer-slots", *arguments]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            self.zscontrol, "_resolve_instance", return_value="test"
        ), mock.patch.object(
            self.zscontrol, "_proxy", return_value=proxy
        ), mock.patch.object(
            self.zscontrol, "_finish", side_effect=SystemExit(0)
        ):
            with self.assertRaises(SystemExit) as raised:
                self.zscontrol.main()
        self.assertEqual(raised.exception.code, 0)
        return proxy.calls

    def test_dcache_only_change_uses_old_manager_call_shape(self):
        self.assertEqual(
            self.invoke("--download", "3"),
            [(None, 3.0, None)],
        )

    def test_s3_change_uses_new_manager_call_shape(self):
        self.assertEqual(
            self.invoke("--s3-download", "5"),
            [(None, None, None, 5.0)],
        )


if __name__ == "__main__":
    unittest.main()
