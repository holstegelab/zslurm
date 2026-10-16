import contextlib
import io
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

import yaml

from zslurm_config import update_site_config
from tests.test_version_handshake import load_manager


class ConfigUpdateTests(unittest.TestCase):
    def test_new_config_uses_spider_template_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as home:
            result = update_site_config("spider", home=home)
            path = Path(home) / ".zslurm" / "config.yaml"
            config = yaml.safe_load(path.read_text(encoding="utf-8"))

            self.assertEqual(result["path"], str(path))
            self.assertTrue(result["changed"])
            self.assertIsNone(result["backup"])
            self.assertEqual(config["cluster_site"], "spider")
            self.assertTrue(config["scratch_use_tmpdir"])
            self.assertEqual(config["scratch_capacity_gb_per_core"], 73)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

            repeated = update_site_config("spider", home=home)
            self.assertFalse(repeated["changed"])
            self.assertEqual(repeated["added"], [])
            self.assertIsNone(repeated["backup"])

    def test_legacy_file_preserves_comments_values_and_backup(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / ".zslurm"
            original = (
                "# Keep this comment and local choices\n"
                "port: 38800\n"
                "rpcpath: private-value\n"
                "scratch_use_tmpdir: false\n"
                "autogrow_enable: false\n"
            )
            path.write_text(original, encoding="utf-8")

            result = update_site_config("spider", home=home)
            config = yaml.safe_load(path.read_text(encoding="utf-8"))

            self.assertTrue(result["changed"])
            self.assertEqual(Path(result["backup"]).read_text(encoding="utf-8"), original)
            self.assertEqual(stat.S_IMODE(Path(result["backup"]).stat().st_mode), 0o600)
            self.assertIn("# Keep this comment", path.read_text(encoding="utf-8"))
            self.assertEqual(config["rpcpath"], "private-value")
            self.assertFalse(config["scratch_use_tmpdir"])
            self.assertFalse(config["autogrow_enable"])
            self.assertEqual(config["scratch_capacity_gb_per_core"], 73)
            self.assertIn("scratch_use_tmpdir", result["conflicts"])
            self.assertIn("autogrow_enable", result["conflicts"])
            self.assertNotIn("private-value", str(result))

    def test_dry_run_does_not_create_config_or_directory(self):
        with tempfile.TemporaryDirectory() as home:
            result = update_site_config("spider", home=home, dry_run=True)
            self.assertFalse(result["changed"])
            self.assertIn("scratch_use_tmpdir", result["added"])
            self.assertFalse((Path(home) / ".zslurm").exists())

    def test_rejects_another_site_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / ".zslurm"
            original = "cluster_site: snellius\nport: 38800\n"
            path.write_text(original, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "refusing to merge"):
                update_site_config("spider", home=home)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(Path(home).glob("*.bak.*")), [])

    def test_invalid_yaml_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / ".zslurm"
            original = "port: [unclosed\n"
            path.write_text(original, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid YAML"):
                update_site_config("spider", home=home)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_rejects_symlinked_config(self):
        with tempfile.TemporaryDirectory() as home:
            target = Path(home) / "actual.yaml"
            target.write_text("port: 38800\n", encoding="utf-8")
            (Path(home) / ".zslurm").symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlinked config"):
                update_site_config("spider", home=home)
            self.assertEqual(target.read_text(encoding="utf-8"), "port: 38800\n")

    def test_cli_dry_run_exits_without_starting_manager(self):
        manager = load_manager()
        result = {
            "path": "/test/config.yaml",
            "added": ["scratch_use_tmpdir"],
            "conflicts": [],
            "changed": False,
            "backup": None,
        }
        stdout = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch(
                "sys.argv", ["zslurm", "--update-config", "spider", "--dry-run"]))
            update = stack.enter_context(mock.patch(
                "zslurm_config.update_site_config", return_value=result))
            curses_wrapper = stack.enter_context(
                mock.patch.object(manager.curses, "wrapper"))
            stack.enter_context(mock.patch("sys.stdout", stdout))
            manager._cli_main()
        update.assert_called_once_with("spider", dry_run=True)
        curses_wrapper.assert_not_called()
        self.assertIn("Would add 1 missing site keys", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
