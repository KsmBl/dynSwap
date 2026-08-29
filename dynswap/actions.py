"""dynswap.actions — run the privileged helper and stream its progress.

The UIs never touch system state directly; they build a request here, pick an
escalation strategy (already-root / pkexec / sudo) and consume JSON Line events
as the helper works.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

INSTALLED_HELPER = "/usr/lib/dynswap/dynswap-helper"

EventHandler = Callable[[dict], None]


@dataclass
class Result:
    ok: bool
    message: str
    log: list[str]


def helper_program() -> list[str]:
    """The argv prefix that runs the helper, installed or straight from a checkout."""
    if Path(INSTALLED_HELPER).is_file() and os.access(INSTALLED_HELPER, os.X_OK):
        return [INSTALLED_HELPER]
    local = Path(__file__).resolve().parent.parent / "dynswap-helper"
    if local.is_file() and os.access(local, os.X_OK):
        return [str(local)]
    return [sys.executable, "-m", "dynswap.helper"]


def is_root() -> bool:
    return os.geteuid() == 0


def has_pkexec() -> bool:
    return shutil.which("pkexec") is not None


def has_sudo() -> bool:
    return shutil.which("sudo") is not None


def sudo_ticket_valid() -> bool:
    if not has_sudo():
        return False
    try:
        return subprocess.run(
            ["sudo", "-n", "true"], capture_output=True, timeout=5
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def sudo_authenticate(password: str) -> bool:
    """Check a password and refresh the sudo timestamp, without using a TTY.

    Lets a full-screen interface collect the password in its own dialog: the
    password goes down a pipe, so it never reaches the terminal or the process
    list, and a success leaves a ticket the following ``sudo -n`` calls reuse.
    """
    if not has_sudo():
        return False
    try:
        proc = subprocess.run(
            ["sudo", "-S", "-p", "", "-v"],
            input=password + "\n", capture_output=True, text=True, timeout=60,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def graphical_session() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


def escalation_prefix(prefer: str = "auto") -> list[str]:
    """argv prefix that gains root.  ``prefer`` is 'auto', 'pkexec' or 'sudo'."""
    if is_root():
        return []
    if prefer == "sudo" and has_sudo():
        return ["sudo", "-n"]
    if prefer == "pkexec" and has_pkexec():
        return ["pkexec"]
    graphical = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if graphical and has_pkexec():
        return ["pkexec"]
    if has_sudo():
        return ["sudo", "-n"]
    if has_pkexec():
        return ["pkexec"]
    return []


def _pkexec_env_wrap(argv: list[str]) -> list[str]:
    """pkexec scrubs the environment; re-inject PYTHONPATH for checkout runs."""
    if argv[0] != sys.executable:
        return argv
    root = str(Path(__file__).resolve().parent.parent)
    return ["/usr/bin/env", f"PYTHONPATH={root}", *argv]


def build_argv(request: list[str], prefer: str = "auto") -> list[str]:
    prefix = escalation_prefix(prefer)
    program = helper_program()
    if prefix and prefix[0] == "pkexec":
        program = _pkexec_env_wrap(program)
    return [*prefix, *program, *request]


def run_helper(
    request: list[str],
    on_event: EventHandler | None = None,
    prefer: str = "auto",
) -> Result:
    """Run one helper subcommand, forwarding every event to ``on_event``."""
    argv = build_argv(request, prefer)
    if not argv:
        return Result(False, "no way to gain root privileges (install polkit or sudo)", [])

    captured: list[str] = []

    def dispatch(event: dict) -> None:
        if event.get("t") == "log":
            captured.append(str(event.get("msg", "")))
        if on_event:
            on_event(event)

    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        return Result(False, f"could not start helper: {exc}", captured)

    # Read stderr on its own thread: stdout is consumed to completion below,
    # and a full stderr pipe would otherwise block the helper forever.
    stderr_lines: list[str] = []

    def drain_stderr() -> None:
        if proc.stderr is None:
            return
        for line in proc.stderr:
            stderr_lines.append(line)

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()

    final: Result | None = None
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            dispatch({"t": "log", "msg": line})
            continue
        dispatch(event)
        if event.get("t") == "done":
            final = Result(bool(event.get("ok")), str(event.get("msg", "")), captured)

    proc.wait()
    stderr_thread.join(timeout=5)
    stderr = "".join(stderr_lines)

    if final is not None:
        return final
    for noise in stderr.splitlines():
        if noise.strip():
            captured.append(noise.strip())
    if proc.returncode == 126:
        return Result(False, "authentication failed or was cancelled", captured)
    if proc.returncode == 127:
        return Result(False, "helper not found — is dynswap installed?", captured)
    if "password" in stderr.lower() or "sudo:" in stderr.lower():
        return Result(False, "authentication required", captured)
    tail = next((l.strip() for l in reversed(stderr.splitlines()) if l.strip()), "")
    return Result(False, tail or f"helper exited with status {proc.returncode}", captured)


# --------------------------------------------------------------------------- #
# request builders — keep argv construction in one place
# --------------------------------------------------------------------------- #

def swapfile_apply(path: str, size_mib: int, priority: int, persist: bool) -> list[str]:
    request = ["swapfile-apply", "--path", path, "--size-mib", str(int(size_mib)),
               "--priority", str(int(priority))]
    if persist:
        request.append("--persist")
    return request


def swapfile_remove(path: str, purge: bool) -> list[str]:
    request = ["swapfile-remove", "--path", path]
    if purge:
        request.append("--purge")
    return request


def swapfile_toggle(path: str, priority: int = -1) -> list[str]:
    return ["swapfile-toggle", "--path", path, "--priority", str(int(priority))]


def zram_apply(size_mib: int, algo: str, priority: int) -> list[str]:
    return ["zram-apply", "--size-mib", str(int(size_mib)), "--algo", algo,
            "--priority", str(int(priority))]


def zram_remove() -> list[str]:
    return ["zram-remove"]


def tuning_apply(values: dict[str, int], persist: bool) -> list[str]:
    request = ["tuning-apply"]
    for key, value in sorted(values.items()):
        request += ["--set", f"{key}={int(value)}"]
    if persist:
        request.append("--persist")
    return request
