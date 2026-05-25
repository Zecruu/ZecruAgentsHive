"""v1.10 Claude Desktop + Claude Code launchers (Windows-only).

The dashboard exposes two buttons per project:
  - Launch Planner → spawn Claude Desktop, configure it for this project's
    AgentsHive MCP endpoint
  - Launch Coder → open a new terminal in the project's local_path, run
    `claude mcp add agentshive-<slug> ...` then `claude --dangerously-skip-permissions`

Two Claude Desktop install variants:
  - **Microsoft Store** (`Claude_pzs8sxrjxfjjc` package): per the v1.7 user-env
    memory, mcpServers config files are silently dropped by this variant.
    Fallback: open Claude + copy MCP URL to clipboard + show user a toast
    asking them to paste it into Settings → Developer.
  - **.exe installer** (`%LOCALAPPDATA%\\Programs\\Claude\\Claude.exe`): can
    write to claude_desktop_config.json. NOT testable on the dev machine
    (only the MS Store variant is installed) — implementation path is
    TODO-commented for v1.10.x once we have a VM with the installer build.

NOT cross-platform — Windows-only per Planner Q4. Mac/Linux is v1.11+.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("agentshive.launcher")

if sys.platform != "win32":  # pragma: no cover
    raise NotImplementedError(
        "AgentsHive launcher is Windows-only in v1.10 (Mac/Linux in v1.11+)."
    )


# Install variant constants — used by detect_claude_desktop and launch_planner.
VARIANT_MS_STORE = "microsoft_store"
VARIANT_INSTALLER = "installer"


@dataclass
class ClaudeDesktopInstall:
    variant: str  # "microsoft_store" | "installer"
    exe_path: str  # absolute path to the Claude.exe to launch
    # MS Store variant: the path to launch via shell:AppsFolder\<PackageFamilyName>!App
    # Installer variant: the actual Claude.exe path
    package_full_name: Optional[str] = None


# --------------------------------------------------------------------- detection


def detect_claude_desktop() -> Optional[ClaudeDesktopInstall]:
    """Look for Claude Desktop on this machine. Returns the first variant found
    or None.

    Detection order: MS Store first (more common per Microsoft's distribution
    push), .exe installer second.
    """
    localappdata = os.environ.get("LOCALAPPDATA")
    if not localappdata:
        return None

    # MS Store variant — package dir at %LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc.
    # We don't read the exe path directly from `C:\Program Files\WindowsApps\` (which
    # is ACL-locked); instead we use the shell:AppsFolder protocol with the package's
    # AppUserModelID to launch. The presence of the package dir is enough evidence
    # the install exists.
    ms_store_dir = Path(localappdata) / "Packages" / "Claude_pzs8sxrjxfjjc"
    if ms_store_dir.is_dir():
        # The launch handle for MS Store apps is `shell:AppsFolder\<PFN>!App`. We
        # synthesize the PFN from the package family name (publisher hash is the
        # tail of the package dir name). For Claude it's well-known:
        package_full_name = "Claude_pzs8sxrjxfjjc"
        # exe_path here is a synthetic shell URI — passed to start_appx_app() below,
        # NOT directly to Popen. We keep the str shape for the dataclass.
        return ClaudeDesktopInstall(
            variant=VARIANT_MS_STORE,
            exe_path=f"shell:AppsFolder\\{package_full_name}!App",
            package_full_name=package_full_name,
        )

    # .exe installer variant — the standard Anthropic installer drops here.
    installer_exe = Path(localappdata) / "Programs" / "Claude" / "Claude.exe"
    if installer_exe.is_file():
        return ClaudeDesktopInstall(
            variant=VARIANT_INSTALLER,
            exe_path=str(installer_exe),
        )

    return None


def detect_claude_code_cli() -> Optional[str]:
    """shutil.which('claude'). Returns the absolute path or None."""
    return shutil.which("claude")


# --------------------------------------------------------------------- launchers


def _copy_to_clipboard(text: str) -> None:
    """Pipe `text` into the Windows `clip` command. Returns silently on failure
    (we'd rather not crash the launcher over a clipboard hiccup)."""
    try:
        # `clip` is built into Windows; no external dep. Use UTF-16-LE which is
        # what `clip` natively expects on modern Windows.
        proc = subprocess.run(
            ["clip"], input=text.encode("utf-16-le"),
            check=False, timeout=2.0,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            log.warning("clip exited %d while copying MCP URL", proc.returncode)
    except (OSError, subprocess.TimeoutExpired) as e:  # pragma: no cover
        log.warning("clipboard copy failed: %s", e)


def _start_appx_app(package_full_name: str) -> bool:
    """Launch a UWP/MSIX app via the shell:AppsFolder protocol.

    `explorer.exe shell:AppsFolder\\<PFN>!App` is the documented way to start an
    MSIX-packaged app from a script. Returns True on apparent success.
    """
    try:
        subprocess.Popen(
            ["explorer.exe", f"shell:AppsFolder\\{package_full_name}!App"],
            close_fds=True,
        )
        return True
    except OSError as e:
        log.error("failed to start MS Store Claude (%s): %s", package_full_name, e)
        return False


def build_planner_mcp_url(local_port: int, project_slug: str) -> str:
    """The MCP URL the Planner needs to add as a custom connector."""
    return f"http://127.0.0.1:{local_port}/mcp?project={project_slug}"


def launch_planner(
    project_slug: str,
    local_port: int,
    bearer_key: str,
    install: Optional[ClaudeDesktopInstall] = None,
) -> dict:
    """Open Claude Desktop pre-configured (or as-configured-as-possible) for
    this project. Returns a dict the dashboard renders as a toast.

    Branches by install variant:
      - MS Store: open Claude + copy the MCP URL to clipboard. Tell the user
        in the returned message to paste it under Settings → Connectors →
        Add custom connector. We can't programmatically write the config —
        the v1.7 OAuth flow is the real fix for this gap, but for v1.10 the
        clipboard handoff is the realistic UX.
      - Installer: TODO — write claude_desktop_config.json with the MCP
        entry and launch. Not testable on this dev machine.
    """
    install = install or detect_claude_desktop()
    if install is None:
        return {
            "ok": False,
            "error": "Claude Desktop not detected. Install from claude.ai/download.",
        }

    mcp_url = build_planner_mcp_url(local_port, project_slug)

    if install.variant == VARIANT_MS_STORE:
        # MS Store: clipboard + open + toast. The bearer is already in the URL's
        # eventual Authorization header for OAuth (v1.7) — for the MS Store
        # variant the user pastes the URL into the "Add custom connector"
        # dialog which prompts for OAuth, so the bearer isn't part of the
        # clipboard payload.
        _copy_to_clipboard(mcp_url)
        ok = _start_appx_app(install.package_full_name)
        if not ok:
            return {"ok": False, "error": "Could not start Claude Desktop (MS Store)."}
        return {
            "ok": True,
            "variant": VARIANT_MS_STORE,
            "message": (
                "Claude Desktop opening. The MCP URL is on your clipboard — "
                "paste it into Settings → Connectors → Add custom connector. "
                "(Microsoft Store Claude doesn't accept programmatic config; "
                "one-time paste required per project.)"
            ),
            "mcp_url": mcp_url,
        }

    if install.variant == VARIANT_INSTALLER:
        # TODO(v1.10.x): write %APPDATA%\Claude\claude_desktop_config.json with the
        # MCP server entry, then Popen Claude.exe. The shape:
        #   {"mcpServers": {f"agentshive-{slug}": {"transport": {"type": "http",
        #    "url": mcp_url, "headers": {"Authorization": f"Bearer {bearer_key}"}}}}}
        # Untested on this dev machine — only MS Store variant is installed here.
        try:
            subprocess.Popen([install.exe_path], close_fds=True)
        except OSError as e:  # pragma: no cover
            return {"ok": False, "error": f"Could not start Claude.exe: {e}"}
        _copy_to_clipboard(mcp_url)
        return {
            "ok": True,
            "variant": VARIANT_INSTALLER,
            "message": (
                "Claude Desktop launched. MCP URL is on clipboard — paste into "
                "Settings → Connectors → Add custom connector. (Programmatic "
                "config-file write is queued for v1.10.x — needs VM testing.)"
            ),
            "mcp_url": mcp_url,
        }

    return {"ok": False, "error": f"Unknown Claude Desktop variant: {install.variant}"}


def launch_coder(
    project_slug: str,
    local_path: str,
    local_port: int,
    bearer_key: str,
    cli_path: Optional[str] = None,
) -> dict:
    """Open a new terminal in `local_path`, register the AgentsHive MCP entry,
    then launch Claude Code in dangerously-skip-permissions mode.

    Single command line via `cmd /K` so the user can see the output and the
    Claude Code REPL stays open. CREATE_NEW_CONSOLE so the terminal is its
    own window (not nested in our process).
    """
    cli_path = cli_path or detect_claude_code_cli()
    if cli_path is None:
        return {
            "ok": False,
            "error": "Claude Code CLI not detected on PATH. Run `claude doctor` or install via npm.",
        }
    if not os.path.isdir(local_path):
        return {
            "ok": False,
            "error": f"Project local_path does not exist or is not a directory: {local_path}",
        }

    mcp_url = f"http://127.0.0.1:{local_port}/mcp?project={project_slug}"
    mcp_entry = f"agentshive-{project_slug}"
    # The bearer header is passed via --header. Shell-escape to avoid surprises
    # with project slugs containing special chars (slug validator already restricts
    # to [a-z0-9-] but defense-in-depth costs nothing).
    auth_header = f'Authorization: Bearer {bearer_key}'

    # Build a single cmd /K command that:
    #   1. cd to project dir
    #   2. claude mcp add ...
    #   3. claude --dangerously-skip-permissions
    # Each step is && so a failure stops the chain and the user sees the error.
    # We don't use shell=True — Popen + list args avoids quoting nightmares.
    claude_mcp_cmd = [
        cli_path, "mcp", "add", mcp_entry, mcp_url,
        "--header", auth_header,
    ]
    claude_run_cmd = [cli_path, "--dangerously-skip-permissions"]

    # cmd /K runs the command then leaves the prompt open. We construct one
    # string by joining the two commands with `&&` — Windows cmd treats the
    # whole string as one composite line.
    def _q(s: str) -> str:
        # cmd-style quoting — wrap anything with spaces in double quotes.
        return f'"{s}"' if " " in s or "\t" in s else s

    composite = (
        f'cd /d {_q(local_path)} && '
        + " ".join(_q(p) for p in claude_mcp_cmd)
        + " && "
        + " ".join(_q(p) for p in claude_run_cmd)
    )

    CREATE_NEW_CONSOLE = 0x00000010
    try:
        subprocess.Popen(
            ["cmd", "/K", composite],
            cwd=local_path,
            creationflags=CREATE_NEW_CONSOLE,
            close_fds=True,
        )
    except OSError as e:  # pragma: no cover
        return {"ok": False, "error": f"Could not spawn terminal: {e}"}

    return {
        "ok": True,
        "message": (
            f"Launching Claude Code in {local_path}. Look for a new terminal "
            f"window. The Coder is registering MCP entry '{mcp_entry}' then "
            f"starting Claude Code in skip-permissions mode."
        ),
        "mcp_entry": mcp_entry,
        "local_path": local_path,
    }


# --------------------------------------------------------------------- status helper


def claude_status() -> dict:
    """Snapshot of what's detected on this machine. Cheap — only filesystem
    + shutil.which probes, no subprocess calls. Dashboard calls on every render.
    """
    desktop = detect_claude_desktop()
    cli = detect_claude_code_cli()
    return {
        "desktop_install": (
            None if desktop is None else {
                "variant": desktop.variant,
                "exe_path": desktop.exe_path,
                "package_full_name": desktop.package_full_name,
            }
        ),
        "cli_install": cli,
    }
