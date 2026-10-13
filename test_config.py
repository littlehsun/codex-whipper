import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from env_config import read_env
from install_service import render_service, systemd_quote
from whipper import parse_args


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.file = self.root / ".env"
        self.file.write_text("# isolated test settings\n")
        # Public tests must not depend on the developer's private environment.
        self.environment = patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                                  if not k.startswith("WHIPPER_")}, clear=True)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def args(self, *extra):
        return parse_args(["--env-file", str(self.file), *extra])

    def test_file_environment_and_cli_precedence(self):
        self.file.write_text('WHIPPER_MESSAGE="continue carefully"\nWHIPPER_DELAY=10\nWHIPPER_THREADS=first,second\nWHIPPER_DRY_RUN=true\n')
        self.assertEqual(self.args().message, "continue carefully")
        os.environ["WHIPPER_MESSAGE"] = "environment message"
        self.assertEqual(self.args().message, "environment message")
        args = self.args("--message", "CLI message", "--thread", "third", "--no-dry-run")
        self.assertEqual(args.message, "CLI message")
        self.assertEqual(args.thread, ["third"])
        self.assertFalse(args.dry_run)
        self.assertEqual(self.args().thread, ["first", "second"])

    def test_dry_run_cannot_consume_configured_live_state(self):
        state = self.root / "live.json"
        self.file.write_text(f'WHIPPER_STATE="{state}"\nWHIPPER_DRY_RUN=true\n')
        self.assertEqual(self.args().state, self.root / "live.dry-run.json")
        self.assertEqual(self.args("--no-dry-run").state, state)

    def test_paths_relative_to_project_and_home_expansion(self):
        self.file.write_text('WHIPPER_LOG_FILE="logs/example log.txt"\nWHIPPER_HERDR_BIN=~/.local/bin/herdr\n')
        from env_config import ROOT
        args = self.args()
        self.assertEqual(args.log_file, ROOT / "logs/example log.txt")
        self.assertEqual(args.herdr_bin, str(Path.home() / ".local/bin/herdr"))

    def test_no_shell_execution_or_variable_expansion(self):
        marker = self.root / "must-not-exist"
        self.file.write_text(f'WHIPPER_MESSAGE="$(touch {marker}) $HOME"\n')
        self.assertEqual(self.args().message, f"$(touch {marker}) $HOME")
        self.assertFalse(marker.exists())

    def test_invalid_settings_do_not_echo_private_values(self):
        for text in ['WHIPPER_MESSAGE="private incomplete', 'UNKNOWN_KEY=private-value',
                     'WHIPPER_MESSAGE=a b', 'WHIPPER_DELAY=1\nWHIPPER_DELAY=2']:
            self.file.write_text(text)
            with self.assertRaises(ValueError) as ctx:
                read_env(self.file)
            self.assertNotIn("private", str(ctx.exception))

    def test_service_render_quotes_install_paths_and_uses_env_file(self):
        checkout = self.root / "project with spaces"
        checkout.mkdir()
        (checkout / "whipper.py").write_text("# fixture")
        interpreter = checkout / "python"
        interpreter.write_text("# fixture")
        interpreter.chmod(0o755)
        self.file.write_text(f'WHIPPER_INSTALL_DIR="{checkout}"\nWHIPPER_PYTHON="{interpreter}"\nWHIPPER_LOG_FILE=logs/whipper.log\n')
        service = render_service(self.file)
        self.assertIn(f'ExecStart="{interpreter}" "{checkout / "whipper.py"}"', service)
        self.assertNotIn("WorkingDirectory=", service)
        self.assertIn(f'--env-file "{self.file}"', service)
        self.assertNotIn("@INSTALL_DIR@", service)
        self.assertNotIn("--herdr-thread", service)

    def test_service_paths_reject_directive_injection(self):
        for value in ["/tmp/project\nExecStart=command", "/tmp/\0project"]:
            with self.assertRaises(ValueError):
                systemd_quote(value)
        self.assertEqual(systemd_quote('/tmp/100% "quoted"'), '"/tmp/100%% \\"quoted\\""')
        self.assertEqual(systemd_quote('/tmp/$HOME', exec_argument=True), '"/tmp/$$HOME"')


if __name__ == "__main__":
    unittest.main()
