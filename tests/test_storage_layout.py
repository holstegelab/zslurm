import pathlib
import tempfile
import unittest

import zslurm_shared


class StorageLayoutTests(unittest.TestCase):
    def test_new_installation_uses_zslurm_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = pathlib.Path(temporary)
            layout = zslurm_shared.default_storage_layout(home)

        self.assertEqual(layout["config_home"], str(home / ".zslurm"))
        self.assertEqual(
            layout["instance_dir"], str(home / ".zslurm" / "instances")
        )
        self.assertEqual(
            layout["config_file"], str(home / ".zslurm" / "config.yaml")
        )

    def test_legacy_config_file_gets_separate_runtime_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = pathlib.Path(temporary)
            legacy = home / ".zslurm"
            legacy.write_text("port: 38800\n", encoding="utf-8")
            layout = zslurm_shared.default_storage_layout(home)

        self.assertEqual(layout["config_home"], str(home / ".zslurm.d"))
        self.assertEqual(
            layout["instance_dir"], str(home / ".zslurm.d" / "instances")
        )
        self.assertEqual(layout["config_file"], str(legacy))


if __name__ == "__main__":
    unittest.main()
