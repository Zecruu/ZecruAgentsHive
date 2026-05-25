"""v1.10 desktop.py shell tests (Windows-only).

Covers the local.key auto-generation, the single-instance file lock
(acquire/release/stale-cleanup), and the AGENTSHIVE_DESKTOP=1 mode toggle
on /api/dashboard/mode.

We DON'T exercise pywebview (would open an actual window) or the named-pipe
IPC (requires Win32 service-loop testing infrastructure). Those are smoke-
verified by the manual installer test.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# v1.8 lesson: localhost guard.
assert os.environ.get("AGENTSHIVE_BASE", "localhost").startswith(
    ("http://localhost", "localhost", "127.0.0.1")
) or "AGENTSHIVE_BASE" not in os.environ, \
    f"refusing to run with non-localhost AGENTSHIVE_BASE={os.environ.get('AGENTSHIVE_BASE')}"

if sys.platform != "win32":
    print("test_desktop.py SKIPPED — Windows-only test suite", file=sys.stderr)
    sys.exit(0)


# Imports below trigger the desktop module's Windows-only guard.
from agentshive import desktop


# ---------------------------------------------------------------- local.key

class LocalKeyTests(unittest.TestCase):
    def test_first_launch_generates_and_persists_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}, clear=False):
                key1 = desktop.ensure_local_key()
                self.assertTrue(len(key1) >= 32, f"key suspiciously short: {key1!r}")
                # Persists — second call returns the SAME key (no rotation, Q3)
                key2 = desktop.ensure_local_key()
                self.assertEqual(key1, key2)
                # File exists with the key content
                kp = Path(tmp) / "AgentsHive" / "local.key"
                self.assertTrue(kp.exists())
                self.assertEqual(kp.read_text().strip(), key1)


# ---------------------------------------------------------------- single-instance lock

class LockTests(unittest.TestCase):
    def test_acquire_when_no_lock_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}, clear=False):
                self.assertTrue(desktop.acquire_lock())
                lp = Path(tmp) / "AgentsHive" / "instance.lock"
                self.assertTrue(lp.exists())
                content = lp.read_text().splitlines()
                self.assertEqual(int(content[0]), os.getpid())

    def test_release_lock_removes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}, clear=False):
                desktop.acquire_lock()
                desktop.release_lock()
                lp = Path(tmp) / "AgentsHive" / "instance.lock"
                self.assertFalse(lp.exists())

    def test_acquire_rejected_when_live_pid_holds_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lp = Path(tmp) / "AgentsHive"
            lp.mkdir()
            (lp / "instance.lock").write_text(f"{os.getpid()}\n{time.time()}\n")
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}, clear=False):
                # Our own PID is alive, so acquire should return False
                self.assertFalse(desktop.acquire_lock())

    def test_acquire_reclaims_stale_lock_dead_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            lp = Path(tmp) / "AgentsHive"
            lp.mkdir()
            # Pick a PID that's astronomically unlikely to be alive — Windows
            # PIDs are 32-bit but typically < 1e6. 1 is reserved (System Idle
            # Process on Windows but OpenProcess refuses with ACCESS_DENIED so
            # treats it as "alive"). Use 999999 instead.
            (lp / "instance.lock").write_text(f"999999\n{time.time()}\n")
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}, clear=False):
                # Stale dead-PID lock — should be reclaimable.
                self.assertTrue(desktop.acquire_lock())
                # Lock now holds our PID
                content = (lp / "instance.lock").read_text().splitlines()
                self.assertEqual(int(content[0]), os.getpid())


# ---------------------------------------------------------------- desktop mode flag

class DesktopModeFlagTests(unittest.TestCase):
    """The /api/dashboard/mode endpoint returns desktop_mode based on the
    AGENTSHIVE_DESKTOP env var, which the desktop shell sets to "1" when
    spawning the server subprocess. Verifies the toggle works correctly.
    """

    def test_mode_endpoint_reports_true_when_env_set(self):
        from starlette.testclient import TestClient
        with mock.patch.dict(os.environ, {
            "AGENTSHIVE_DESKTOP": "1",
            "AGENTSHIVE_API_KEY": "test-key",
            "DATABASE_URL": "sqlite:///:memory:",
        }, clear=False):
            from agentshive.main import build_app
            app, _ = build_app()
            with TestClient(app, base_url="http://localhost:8765") as c:
                r = c.get("/api/dashboard/mode", headers={"Authorization": "Bearer test-key"})
                self.assertEqual(r.status_code, 200)
                self.assertTrue(r.json()["desktop_mode"])

    def test_mode_endpoint_reports_false_when_env_unset(self):
        # Build a fresh app with the env explicitly unset.
        from starlette.testclient import TestClient
        env_no_desktop = {k: v for k, v in os.environ.items() if k != "AGENTSHIVE_DESKTOP"}
        env_no_desktop.update({
            "AGENTSHIVE_API_KEY": "test-key",
            "DATABASE_URL": "sqlite:///:memory:",
        })
        with mock.patch.dict(os.environ, env_no_desktop, clear=True):
            # Reload modules so the cached AGENTSHIVE_DESKTOP check happens fresh.
            # (The handler reads os.environ at call time, so reload isn't strictly
            # required, but be defensive in case future code caches at import.)
            from agentshive.main import build_app
            app, _ = build_app()
            with TestClient(app, base_url="http://localhost:8765") as c:
                r = c.get("/api/dashboard/mode", headers={"Authorization": "Bearer test-key"})
                self.assertEqual(r.status_code, 200)
                self.assertFalse(r.json()["desktop_mode"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
