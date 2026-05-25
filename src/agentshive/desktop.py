"""v1.10 AgentsHive Desktop — pywebview shell + bundled uvicorn subprocess.

End-user experience: double-click the installed `AgentsHive` shortcut, see the
dashboard render in a native window. No manual MCP-entry setup per project, no
Claude Desktop config-file edits. Local-first: bound to 127.0.0.1, local
sqlite DB, locally-stored bearer key.

Architecture:
  - This module is the PyInstaller entry point (via `scripts/build_desktop.bat`).
  - On launch it:
      1. checks `sys.platform == "win32"` (Mac/Linux is v1.11+)
      2. acquires a single-instance file lock at %LOCALAPPDATA%/AgentsHive/instance.lock,
         using a PID-staleness check (via ctypes OpenProcess) so a crashed prior
         launch doesn't leave the app un-launchable
      3. if another instance is alive, signals it to raise its window via a
         Windows named pipe and exits cleanly
      4. auto-generates a local bearer key at %LOCALAPPDATA%/AgentsHive/local.key
         on first launch (32-char URL-safe token via secrets.token_urlsafe) and
         keeps it forever per Planner Q3 (rotation = manual file delete)
      5. spawns uvicorn as a subprocess with AGENTSHIVE_DESKTOP=1 plus the
         local key + local DB path in the env so the bundled server picks them up
      6. polls http://127.0.0.1:8765/healthz until 200 OK (10s timeout)
      7. opens a pywebview window pointing at /dashboard with a DesktopAPI
         js_api instance so the dashboard JS can call back into Python for
         native folder dialogs (v1.10 commit 2) and Claude launch (commit 3)
  - On window close: SIGTERM the uvicorn subprocess (2s grace, then SIGKILL),
    release the lock, exit.

Why a subprocess and not in-process uvicorn:
  - Clean kill semantics — pywebview owns the GUI thread, uvicorn owns its
    asyncio loop; mixing them in one process needs careful loop-bridging and
    crashes on close. A subprocess is the boring, correct boundary.
  - PyInstaller packaging is also easier — uvicorn is run as a CLI module
    that PyInstaller's own bundle handles, not as a custom asgi-in-thread.
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("agentshive.desktop")

# v1.10 hard requirement — Mac/Linux is v1.11+ per Planner Q4. Module-level
# guard so any code path that imports desktop on a non-Windows platform fails
# loudly rather than producing subtle bugs around %LOCALAPPDATA%, named pipes,
# WebView2, etc.
if sys.platform != "win32":  # pragma: no cover — non-Windows is not in v1.10 scope
    raise NotImplementedError(
        "AgentsHive Desktop is Windows-only in v1.10. "
        "Mac/Linux support is queued for v1.11+. The Railway-hosted server "
        "(https://agentshive-production.up.railway.app/dashboard) works from any "
        "browser on any OS today."
    )

# Local-only constants. Port fixed per Planner Q1 — random would make
# `claude mcp add` registrations fragile across restarts.
LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 8765
HEALTHZ_URL = f"http://{LOCAL_HOST}:{LOCAL_PORT}/healthz"
DASHBOARD_URL = f"http://{LOCAL_HOST}:{LOCAL_PORT}/dashboard"

# Single-instance named pipe — second launches use this to send "raise window"
# to the first instance instead of double-spawning a server.
PIPE_NAME = r"\\.\pipe\AgentsHive-singleinstance"
PIPE_MSG_RAISE = b"RAISE_WINDOW\n"


def appdata_dir() -> Path:
    """`%LOCALAPPDATA%\\AgentsHive` — auto-created on first reference."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    p = Path(base) / "AgentsHive"
    p.mkdir(parents=True, exist_ok=True)
    return p


def lock_path() -> Path:
    return appdata_dir() / "instance.lock"


def key_path() -> Path:
    return appdata_dir() / "local.key"


def db_path() -> Path:
    return appdata_dir() / "data.db"


# --------------------------------------------------------------------- bearer key


def ensure_local_key() -> str:
    """Return the local bearer key; generate + persist on first launch.

    Per Planner Q3 (never rotate): the key is written once and never touched
    again by AgentsHive. Users can rotate by deleting the file — that's the
    documented escape hatch in the README. The key never leaves 127.0.0.1.
    """
    kp = key_path()
    if kp.exists():
        existing = kp.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    key = secrets.token_urlsafe(32)
    kp.write_text(key, encoding="utf-8")
    # Best-effort tighten perms on Windows — relies on filesystem ACL inheritance
    # from %LOCALAPPDATA% which is per-user by default. No-op if it fails.
    try:
        import stat
        os.chmod(kp, stat.S_IREAD | stat.S_IWRITE)
    except OSError:
        pass
    return key


# --------------------------------------------------------------------- single-instance lock


def _pid_alive(pid: int) -> bool:
    """Windows-only PID existence check via ctypes OpenProcess.

    Avoids the psutil dependency for a single isolated check (~15 lines vs a
    ~5MB transitive dep — Planner Q2 nudge).
    """
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # ERROR_INVALID_PARAMETER (87) and ERROR_ACCESS_DENIED (5). Treat
        # ACCESS_DENIED as alive (process exists but elevated), INVALID as dead.
        last_err = kernel32.GetLastError()
        return last_err == 5
    # Process is alive if exit code is STILL_ACTIVE (259); otherwise it's a
    # zombie waiting for handle release — treat as dead so we can reclaim.
    exit_code = ctypes.c_ulong()
    kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
    kernel32.CloseHandle(handle)
    return exit_code.value == 259


def acquire_lock() -> bool:
    """Atomic single-instance lock. Returns True on acquire, False if another
    live instance holds it (caller should then signal-and-exit).

    Lock file format: `<pid>\\n<iso-timestamp>`. On startup we open with
    O_CREAT|O_EXCL — if that races, fall back to reading the existing file
    and checking whether the PID is still alive. Stale (dead PID) lock is
    reclaimed atomically.
    """
    lp = lock_path()
    my_payload = f"{os.getpid()}\n{time.time()}\n".encode("utf-8")

    # Fast path: file doesn't exist, create exclusive.
    try:
        fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        try:
            os.write(fd, my_payload)
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        pass

    # Slow path: file exists. Inspect PID.
    try:
        existing = lp.read_text(encoding="utf-8")
        first_line = existing.splitlines()[0] if existing.strip() else ""
        other_pid = int(first_line) if first_line.isdigit() else 0
    except (OSError, ValueError):
        other_pid = 0

    if other_pid and _pid_alive(other_pid):
        return False  # genuine collision — caller signals existing instance

    # Stale lock — owner is dead. Reclaim by overwriting.
    try:
        lp.write_bytes(my_payload)
        return True
    except OSError:
        log.warning("could not reclaim stale lock at %s", lp)
        return False


def release_lock() -> None:
    """Best-effort delete of our lock file. Safe to call multiple times."""
    try:
        lock_path().unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.debug("release_lock: %s", e)


# --------------------------------------------------------------------- named pipe IPC


def signal_existing_instance_to_raise() -> bool:
    """Send PIPE_MSG_RAISE to the already-running instance. Returns True on
    successful send, False if the pipe isn't there (e.g. instance died between
    the lock check and now — caller should fall back to acquiring anew).
    """
    try:
        with open(PIPE_NAME, "wb") as p:
            p.write(PIPE_MSG_RAISE)
            p.flush()
        return True
    except OSError:
        return False


def _serve_raise_pipe(on_raise) -> None:
    """Background pipe server. Loops accepting connections; on PIPE_MSG_RAISE
    calls `on_raise()` so the existing window can bring itself to the front.

    Implemented via Win32 CreateNamedPipe directly because the file-based
    `open(PIPE_NAME)` only works for clients — server side needs the kernel
    handle. Uses MESSAGE-mode + WAIT semantics, single instance at a time
    (we never expect concurrent senders since signals are infrequent).
    """
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.windll.kernel32

    PIPE_ACCESS_INBOUND = 0x00000001
    PIPE_TYPE_MESSAGE = 0x00000004
    PIPE_READMODE_MESSAGE = 0x00000002
    PIPE_WAIT = 0x00000000
    PIPE_UNLIMITED_INSTANCES = 255
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    while True:
        h = kernel32.CreateNamedPipeW(
            PIPE_NAME, PIPE_ACCESS_INBOUND,
            PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
            PIPE_UNLIMITED_INSTANCES, 512, 512, 0, None,
        )
        if h == INVALID_HANDLE_VALUE:
            log.warning("CreateNamedPipe failed err=%s", kernel32.GetLastError())
            time.sleep(1.0)
            continue
        # Block until a client connects.
        if not kernel32.ConnectNamedPipe(h, None):
            # ERROR_PIPE_CONNECTED (535) is benign — client connected before
            # we called ConnectNamedPipe.
            if kernel32.GetLastError() != 535:
                kernel32.CloseHandle(h)
                continue
        buf = ctypes.create_string_buffer(512)
        bytes_read = wintypes.DWORD(0)
        kernel32.ReadFile(h, buf, 512, ctypes.byref(bytes_read), None)
        kernel32.DisconnectNamedPipe(h)
        kernel32.CloseHandle(h)
        if buf.raw.startswith(PIPE_MSG_RAISE):
            try:
                on_raise()
            except Exception:  # pragma: no cover
                log.exception("on_raise callback failed")


# --------------------------------------------------------------------- server subprocess


def start_server_subprocess(local_key: str) -> subprocess.Popen:
    """Spawn the server as a subprocess of THIS executable in --server mode.

    Why `[sys.executable, "--server"]` instead of `python -m agentshive.main`:
      - In a PyInstaller bundle there's no Python interpreter to invoke via
        `-c "..."` or `-m module` — sys.executable IS the bundled .exe. The
        only way to "spawn a child Python" is to re-exec ourselves with a
        flag the entry script branches on.
      - In source-tree dev runs, sys.executable is the venv's python.exe, so
        `[sys.executable, "--server"]` becomes `python.exe --server` which
        hits desktop.py's argparse (the same script is the entry point).
      - Either way the child process eventually calls `agentshive.main.main()`
        which runs uvicorn — same code path as Railway, same kill semantics
        as a normal subprocess.

    The --server mode handler is implemented in main() below; it short-circuits
    before the GUI bootstrap.
    """
    env = os.environ.copy()
    env["AGENTSHIVE_API_KEY"] = local_key
    env["AGENTSHIVE_DESKTOP"] = "1"
    env["DATABASE_URL"] = f"sqlite:///{db_path()}"
    env["PORT"] = str(LOCAL_PORT)
    env["AGENTSHIVE_BASE_URL"] = f"http://{LOCAL_HOST}:{LOCAL_PORT}"

    # CREATE_NO_WINDOW so the server doesn't pop a console behind the
    # pywebview window when launched from the installed .exe (which is
    # --windowed and otherwise wouldn't have a console anyway).
    creationflags = 0x08000000  # CREATE_NO_WINDOW
    return subprocess.Popen(
        [sys.executable, "--server"],
        env=env,
        creationflags=creationflags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_healthz(timeout_seconds: float = 10.0) -> bool:
    """Poll the server's /healthz until 200 OK or timeout. ~50ms intervals."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(HEALTHZ_URL, timeout=0.5) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(0.05)
    return False


def stop_server_subprocess(proc: subprocess.Popen) -> None:
    """Graceful 2s SIGTERM, then SIGKILL. Tolerates already-dead processes."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        log.info("server didn't terminate in 2s — killing")
        proc.kill()
    except OSError:
        pass


# --------------------------------------------------------------------- JS bridge API


class DesktopAPI:
    """Methods exposed to dashboard JS via pywebview's js_api bridge.

    Dashboard JS calls them as `await window.pywebview.api.<method>()`. The
    methods run in the GUI thread, which is what pywebview's
    `window.create_file_dialog` and Win32 calls require. The folder picker
    therefore goes through this bridge rather than an HTTP endpoint — the
    server subprocess doesn't own the pywebview window.
    """

    # Set by main() once the window is created so pick_folder can call
    # window.create_file_dialog. None until then.
    window: object = None

    def is_desktop_mode(self) -> bool:
        return True

    def pick_folder(self) -> str | None:
        """v1.10 Commit 2: native OS folder picker. Returns the selected
        absolute path or None on cancel. Called by the dashboard "+ New
        project" form's Pick Folder button when desktop mode is detected.
        """
        if self.window is None:  # pragma: no cover — window must be created first
            return None
        import webview
        result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return None
        # create_file_dialog returns a tuple of selected paths (folder mode
        # always yields exactly one) — return the first element as a plain str.
        return str(result[0]) if result else None


# --------------------------------------------------------------------- main entry


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AgentsHive Desktop — local-first dashboard for AI Planner ↔ Coder bridge"
    )
    parser.add_argument("--headless", action="store_true",
                        help="Run the server only, no GUI (for testing / dev)")
    parser.add_argument("--server", action="store_true",
                        help="Internal: run as the server-only subprocess "
                             "(used by the desktop shell to spawn uvicorn inside "
                             "the PyInstaller bundle). Reads PORT + AGENTSHIVE_* "
                             "from env.")
    parser.add_argument("--debug", action="store_true",
                        help="Enable verbose logging + pywebview debug mode")
    args = parser.parse_args()

    # --server mode: hand off to agentshive.main.main() and never return.
    # This branch is what the parent desktop process Popen-spawns; it has to
    # short-circuit BEFORE the GUI bootstrap / lock acquisition.
    if args.server:
        from agentshive.main import main as server_main
        server_main()
        return 0

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Single-instance check — if another live instance has the lock, signal
    # it to raise its window and exit. If the signal fails (instance died
    # between lock check and pipe write), fall through and try to reclaim.
    if not acquire_lock():
        if signal_existing_instance_to_raise():
            log.info("AgentsHive is already running; raised its window. Exiting.")
            return 0
        # The other instance died after we read the lock; one more try.
        if not acquire_lock():
            log.error("could not acquire single-instance lock; another AgentsHive may be running")
            return 1

    server_proc = None
    try:
        local_key = ensure_local_key()
        server_proc = start_server_subprocess(local_key)
        if not wait_for_healthz(timeout_seconds=15.0):
            log.error("server did not become healthy in 15s; check for port %d conflict", LOCAL_PORT)
            stop_server_subprocess(server_proc)
            return 2

        log.info("AgentsHive Desktop ready at %s", DASHBOARD_URL)

        if args.headless:
            # Sleep until SIGINT — for development / smoke tests of the server.
            log.info("--headless mode; press Ctrl+C to stop")
            try:
                while True:
                    if server_proc.poll() is not None:
                        log.error("server subprocess exited unexpectedly")
                        return 3
                    time.sleep(1)
            except KeyboardInterrupt:
                return 0

        # GUI mode: open the pywebview window. Bridge a "show this window"
        # callback to the named-pipe server so second launches raise us.
        import webview
        api = DesktopAPI()
        window = webview.create_window(
            title="AgentsHive",
            url=DASHBOARD_URL,
            js_api=api,
            width=1200,
            height=800,
            min_size=(800, 600),
        )
        api.window = window  # pick_folder needs this to call create_file_dialog

        def _raise_window():
            try:
                window.show()
                window.restore()
            except Exception:  # pragma: no cover
                log.exception("raise window failed")

        import threading
        pipe_thread = threading.Thread(
            target=_serve_raise_pipe, args=(_raise_window,), daemon=True
        )
        pipe_thread.start()

        webview.start(debug=args.debug)
        return 0
    finally:
        if server_proc is not None:
            stop_server_subprocess(server_proc)
        release_lock()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
