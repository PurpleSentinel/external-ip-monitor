import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ipwatch.cli import main
from ipwatch.config import ConfigError, load_config
from ipwatch.systemd import UnitError, exec_arg, units, user_unit_dir, write_units


class SystemdUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.yaml = self.root / "config.yaml"
        self.yaml.write_text("interval_minutes: 5\n")
        self.config = load_config(self.yaml)

    def test_service_and_timer_content(self):
        files = units(self.config)
        self.assertEqual(set(files), {"ipwatch.service", "ipwatch.timer"})
        service, timer = files["ipwatch.service"], files["ipwatch.timer"]
        self.assertIn(f"ExecStart={sys.executable} -m ipwatch run --config {self.yaml}\n", service)
        self.assertIn("Type=oneshot\n", service)
        self.assertIn("SuccessExitStatus=1 3\n", service)
        self.assertIn("OnCalendar=*-*-* *:*:00\n", timer)
        self.assertIn("AccuracySec=1s\n", timer)
        self.assertIn("WantedBy=timers.target\n", timer)
        self.assertIn("ipwatch-ipv6.service", units(self.config, "ipwatch-ipv6")["ipwatch-ipv6.timer"])

    def test_exec_arg_escaping(self):
        self.assertEqual(exec_arg("/plain/path.yaml"), "/plain/path.yaml")
        self.assertEqual(exec_arg("/a b/c.yaml"), '"/a b/c.yaml"')
        self.assertEqual(exec_arg("/p%h/$HOME"), "/p%%h/$$HOME")
        self.assertEqual(exec_arg('/x "q" \\ ;'), '"/x \\"q\\" \\\\ ;"')
        with self.assertRaises(ConfigError):
            exec_arg("/bad\nline")

    def test_invalid_names(self):
        for name in ("", "-x", "a b", "ipwatch@", "../x", "a/b", "x" * 65):
            with self.subTest(name=name), self.assertRaises(ConfigError):
                units(self.config, name)

    def test_write_is_idempotent_and_refuses_silent_overwrite(self):
        target = self.root / "units"
        files = units(self.config)
        self.assertEqual([s for _, s in write_units(files, target)], ["written", "written"])
        self.assertEqual([s for _, s in write_units(files, target)], ["unchanged", "unchanged"])
        changed = units(self.config, "ipwatch") | {"ipwatch.timer": "[Timer]\nOnCalendar=hourly\n"}
        with self.assertRaisesRegex(UnitError, "--overwrite"):
            write_units(changed, target)
        self.assertEqual((target / "ipwatch.timer").read_text(), files["ipwatch.timer"])
        write_units(changed, target, overwrite=True)
        self.assertEqual((target / "ipwatch.timer").read_text(), "[Timer]\nOnCalendar=hourly\n")
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["ipwatch.service", "ipwatch.timer"])

    def test_user_unit_dir_honours_xdg(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "xdg")}):
            self.assertEqual(user_unit_dir(), self.root / "xdg/systemd/user")
        with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.home", return_value=self.root):
            self.assertEqual(user_unit_dir(), self.root / ".config/systemd/user")

    def test_cli_print_and_write(self):
        with patch("builtins.print") as printed:
            self.assertEqual(main(["systemd-units", "--config", str(self.yaml)]), 0)
        output = "\n".join(str(c.args[0]) for c in printed.call_args_list)
        self.assertIn("# ipwatch.service", output)
        self.assertIn("# ipwatch.timer", output)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "xdg")}), patch("builtins.print"):
            self.assertEqual(main(["systemd-units", "--config", str(self.yaml), "--write"]), 0)
            (self.root / "xdg/systemd/user/ipwatch.timer").write_text("edited\n")
            with patch("sys.stderr"):
                self.assertEqual(main(["systemd-units", "--config", str(self.yaml), "--write"]), 2)
            self.assertEqual(main(["systemd-units", "--config", str(self.yaml), "--write", "--overwrite"]), 0)
        self.assertNotEqual((self.root / "xdg/systemd/user/ipwatch.timer").read_text(), "edited\n")

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze required")
    def test_systemd_accepts_units_for_awkward_paths(self):
        odd = self.root / "odd dir %h $HOME it's"
        odd.mkdir()
        (odd / "my conf.yaml").write_text("interval_minutes: 5\n")
        out = self.root / "verify"
        write_units(units(load_config(odd / "my conf.yaml"), "ipwatch-check"), out)
        result = subprocess.run(["systemd-analyze", "--user", "verify", "ipwatch-check.service", "ipwatch-check.timer"],
            cwd=out, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
