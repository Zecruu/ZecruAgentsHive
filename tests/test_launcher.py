"""v1.10 launcher.py tests (Windows-only).

Mocks filesystem + PATH probes for detect_claude_desktop / detect_claude_code_cli.
Asserts command-line construction for launch_planner / launch_coder WITHOUT
actually subprocess.Popen-ing anything (we don't want a Claude window to pop
up during a test run).

Skipped entirely on non-Windows since launcher imports raise NotImplementedError
on import.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# v1.8 lesson: localhost guard. Even though this test file doesn't talk to a
# remote server, the rule applies — we're documenting intent.
import os as _os
assert _os.environ.get("AGENTSHIVE_BASE", "localhost").startswith(("http://localhost", "localhost", "127.0.0.1")) \
    or "AGENTSHIVE_BASE" not in _os.environ, \
    f"refusing to run with non-localhost AGENTSHIVE_BASE={_os.environ.get('AGENTSHIVE_BASE')}"

if sys.platform != "win32":
    print("test_launcher.py SKIPPED — Windows-only test suite", file=sys.stderr)
    sys.exit(0)


from agentshive import launcher
from agentshive.launcher import (
    ClaudeDesktopInstall,
    VARIANT_MS_STORE,
    VARIANT_INSTALLER,
    detect_claude_desktop,
    detect_claude_code_cli,
    launch_planner,
    launch_coder,
    claude_status,
    build_planner_mcp_url,
)


# ---------------------------------------------------------------- detect_claude_desktop

class DetectClaudeDesktopTests(unittest.TestCase):
    def test_ms_store_variant_detected_when_package_dir_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Packages" / "Claude_pzs8sxrjxfjjc").mkdir(parents=True)
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}, clear=False):
                result = detect_claude_desktop()
            self.assertIsNotNone(result)
            self.assertEqual(result.variant, VARIANT_MS_STORE)
            self.assertEqual(result.package_full_name, "Claude_pzs8sxrjxfjjc")
            self.assertIn("shell:AppsFolder", result.exe_path)

    def test_installer_variant_detected_when_exe_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            programs_dir = Path(tmp) / "Programs" / "Claude"
            programs_dir.mkdir(parents=True)
            exe = programs_dir / "Claude.exe"
            exe.write_text("")  # touch — detect only checks is_file()
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}, clear=False):
                result = detect_claude_desktop()
            self.assertIsNotNone(result)
            self.assertEqual(result.variant, VARIANT_INSTALLER)
            self.assertEqual(result.exe_path, str(exe))
            self.assertIsNone(result.package_full_name)

    def test_neither_present_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}, clear=False):
                self.assertIsNone(detect_claude_desktop())


# ---------------------------------------------------------------- detect_claude_code_cli

class DetectClaudeCodeCliTests(unittest.TestCase):
    def test_returns_path_when_on_PATH(self):
        with mock.patch("agentshive.launcher.shutil.which", return_value=r"C:\fake\bin\claude.exe"):
            self.assertEqual(detect_claude_code_cli(), r"C:\fake\bin\claude.exe")

    def test_returns_none_when_not_on_PATH(self):
        with mock.patch("agentshive.launcher.shutil.which", return_value=None):
            self.assertIsNone(detect_claude_code_cli())


# ---------------------------------------------------------------- launch_planner

class LaunchPlannerTests(unittest.TestCase):
    def test_ms_store_path_copies_url_to_clipboard_and_starts_appx_app(self):
        ms_install = ClaudeDesktopInstall(
            variant=VARIANT_MS_STORE,
            exe_path="shell:AppsFolder\\Claude_pzs8sxrjxfjjc!App",
            package_full_name="Claude_pzs8sxrjxfjjc",
        )
        with mock.patch("agentshive.launcher._copy_to_clipboard") as clip_mock, \
             mock.patch("agentshive.launcher._start_appx_app", return_value=True) as appx_mock:
            result = launch_planner(
                project_slug="zecru-widget",
                local_port=8765,
                bearer_key="dummy-key",
                install=ms_install,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["variant"], VARIANT_MS_STORE)
        self.assertEqual(result["mcp_url"], "http://127.0.0.1:8765/mcp?project=zecru-widget")
        clip_mock.assert_called_once_with("http://127.0.0.1:8765/mcp?project=zecru-widget")
        appx_mock.assert_called_once_with("Claude_pzs8sxrjxfjjc")

    def test_no_install_detected_returns_error(self):
        with mock.patch("agentshive.launcher.detect_claude_desktop", return_value=None):
            result = launch_planner(
                project_slug="zecru-widget",
                local_port=8765,
                bearer_key="dummy-key",
            )
        self.assertFalse(result["ok"])
        self.assertIn("not detected", result["error"])


# ---------------------------------------------------------------- launch_coder

class LaunchCoderTests(unittest.TestCase):
    def test_constructs_correct_cmd_K_composite(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("agentshive.launcher.subprocess.Popen") as popen_mock:
            result = launch_coder(
                project_slug="zecru-widget",
                local_path=tmp,
                local_port=8765,
                bearer_key="dummy-key",
                cli_path=r"C:\fake\bin\claude.exe",
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["mcp_entry"], "agentshive-zecru-widget")
        # Inspect the Popen call: it should be ["cmd", "/K", "<composite>"]
        popen_mock.assert_called_once()
        args, kwargs = popen_mock.call_args
        cmd = args[0]
        self.assertEqual(cmd[0], "cmd")
        self.assertEqual(cmd[1], "/K")
        composite = cmd[2]
        self.assertIn(f"cd /d", composite)
        self.assertIn(tmp, composite)
        self.assertIn("agentshive-zecru-widget", composite)
        self.assertIn("http://127.0.0.1:8765/mcp?project=zecru-widget", composite)
        self.assertIn("Authorization: Bearer dummy-key", composite)
        self.assertIn("--dangerously-skip-permissions", composite)
        # CREATE_NEW_CONSOLE = 0x00000010
        self.assertEqual(kwargs.get("creationflags"), 0x00000010)
        self.assertEqual(kwargs.get("cwd"), tmp)

    def test_missing_cli_returns_error(self):
        with mock.patch("agentshive.launcher.detect_claude_code_cli", return_value=None):
            result = launch_coder(
                project_slug="x", local_path=os.getcwd(),
                local_port=8765, bearer_key="k",
            )
        self.assertFalse(result["ok"])
        self.assertIn("not detected", result["error"])

    def test_missing_local_path_returns_error(self):
        result = launch_coder(
            project_slug="x", local_path=r"C:\no\such\dir-12345",
            local_port=8765, bearer_key="k",
            cli_path=r"C:\fake\claude.exe",
        )
        self.assertFalse(result["ok"])
        self.assertIn("does not exist", result["error"])

    def test_mcp_url_construction(self):
        url = build_planner_mcp_url(8765, "my-project")
        self.assertEqual(url, "http://127.0.0.1:8765/mcp?project=my-project")


# ---------------------------------------------------------------- claude_status

class ClaudeStatusTests(unittest.TestCase):
    def test_status_shape(self):
        with mock.patch("agentshive.launcher.detect_claude_desktop", return_value=None), \
             mock.patch("agentshive.launcher.detect_claude_code_cli", return_value=None):
            s = claude_status()
        self.assertIn("desktop_install", s)
        self.assertIn("cli_install", s)
        self.assertIsNone(s["desktop_install"])
        self.assertIsNone(s["cli_install"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
