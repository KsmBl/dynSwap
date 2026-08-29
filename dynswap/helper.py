"""dynswap.helper — the only component that mutates system state.

Runs as root (via pkexec or sudo).  Every argument is validated here rather
than in the UI, so the helper stays safe no matter who invokes it.  Progress is
reported as JSON Lines on stdout:

    {"t": "step", "msg": "...", "pct": 40}
    {"t": "log",  "msg": "..."}
    {"t": "done", "ok": true, "msg": "..."}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import core
from .core import (
    FSTAB, MIB, SYSCTL_FILE, TUNABLES, ZRAM_GENERATOR_CONF, ZRAM_UNIT,
    ZRAM_UNIT_NAME,
)

MIN_SWAPFILE_MIB = 8
MAX_SWAPFILE_MIB = 1024 * 1024          # 1 TiB
MAX_ZRAM_MIB = 256 * 1024               # 256 GiB
FORBIDDEN_PREFIXES = ("/dev/", "/proc/", "/sys/", "/run/", "/tmp/")
SWAP_MAGIC = b"SWAPSPACE2"
ALGO_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")


class HelperError(Exception):
    """A validation or execution failure with a message meant for the user."""


# --------------------------------------------------------------------------- #
# event stream
# --------------------------------------------------------------------------- #

def emit(kind: str, **payload) -> None:
    sys.stdout.write(json.dumps({"t": kind, **payload}) + "\n")
    sys.stdout.flush()


def step(msg: str, pct: int) -> None:
    emit("step", msg=msg, pct=pct)


def log(msg: str) -> None:
    emit("log", msg=msg)


def run(cmd: list[str], *, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    if not quiet:
        log("$ " + " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    for stream in (proc.stdout, proc.stderr):
        for line in (stream or "").splitlines():
            if line.strip():
                log("  " + line.strip())
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise HelperError(f"{cmd[0]} failed: {detail[-1] if detail else proc.returncode}")
    return proc


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

def validate_swapfile_path(raw: str) -> str:
    path = os.path.normpath(raw)
    if not path.startswith("/") or path in ("/", ""):
        raise HelperError(f"swapfile path must be absolute: {raw!r}")
    if any(c in path for c in "\n\r\0"):
        raise HelperError("swapfile path contains control characters")
    if path.startswith(FORBIDDEN_PREFIXES):
        raise HelperError(f"refusing to place a swapfile under {path.split('/')[1]!r}")
    parent = Path(path).parent
    if not parent.is_dir():
        raise HelperError(f"directory does not exist: {parent}")
    if Path(path).is_dir():
        raise HelperError(f"{path} is a directory")
    if Path(path).is_symlink():
        raise HelperError(f"{path} is a symlink; refusing to follow it")
    return path


def has_swap_signature(path: str) -> bool:
    try:
        page = os.sysconf("SC_PAGE_SIZE")
        with open(path, "rb") as fh:
            fh.seek(max(0, page - len(SWAP_MAGIC)))
            return fh.read(len(SWAP_MAGIC)) == SWAP_MAGIC
    except (OSError, ValueError):
        return False


def assert_safe_to_overwrite(path: str) -> None:
    """Never clobber a file that isn't already ours."""
    if not Path(path).exists():
        return
    if not Path(path).is_file():
        raise HelperError(f"{path} exists and is not a regular file")
    known = (
        any(e.name == path for e in core.read_swaps())
        or path in core.fstab_swap_entries()
        or has_swap_signature(path)
    )
    if not known:
        raise HelperError(
            f"{path} already exists and is not a swap file — refusing to overwrite it. "
            "Choose another path or delete it yourself first."
        )


def validate_size(mib: int, *, maximum: int, label: str) -> int:
    if mib < MIN_SWAPFILE_MIB or mib > maximum:
        raise HelperError(
            f"{label} size must be between {MIN_SWAPFILE_MIB} MiB and {core.fmt_mib(maximum)}"
        )
    return int(mib)


def validate_priority(prio: int) -> int:
    if not -1 <= prio <= 32767:
        raise HelperError("priority must be between -1 and 32767")
    return int(prio)


def validate_algo(algo: str) -> str:
    if not ALGO_RE.match(algo or ""):
        raise HelperError(f"invalid compression algorithm: {algo!r}")
    return algo


# --------------------------------------------------------------------------- #
# fstab
# --------------------------------------------------------------------------- #

def _write_atomic(path: str, text: str, mode: int = 0o644) -> None:
    tmp = f"{path}.dynswap.tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def fstab_remove(spec: str) -> bool:
    """Drop every swap line referring to ``spec``.  Returns True if changed."""
    try:
        original = Path(FSTAB).read_text()
    except OSError:
        return False
    kept, removed = [], False
    for raw in original.splitlines(keepends=True):
        line = raw.split("#", 1)[0].strip()
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "swap" and parts[0] == spec:
            removed = True
            continue
        kept.append(raw)
    if removed:
        shutil.copy2(FSTAB, FSTAB + ".dynswap.bak")
        _write_atomic(FSTAB, "".join(kept))
        log(f"removed {spec} from {FSTAB} (backup: {FSTAB}.dynswap.bak)")
    return removed


def fstab_add(spec: str, priority: int) -> None:
    fstab_remove(spec)
    options = "defaults" if priority < 0 else f"defaults,pri={priority}"
    line = f"{spec}\tnone\tswap\t{options}\t0\t0\n"
    try:
        text = Path(FSTAB).read_text()
    except OSError:
        text = ""
    if text and not text.endswith("\n"):
        text += "\n"
    if "# dynswap" not in text:
        text += "\n# dynswap-managed swap\n"
    _write_atomic(FSTAB, text + line)
    log(f"added to {FSTAB}: {line.strip()}")


# --------------------------------------------------------------------------- #
# swapfile operations
# --------------------------------------------------------------------------- #

def swapoff(target: str) -> None:
    if not any(e.name == target for e in core.read_swaps()):
        return
    snap = core.memory_snapshot()
    in_use = next((e.used_mib for e in core.read_swaps() if e.name == target), 0)
    if in_use > snap.available_mib:
        raise HelperError(
            f"cannot deactivate {target}: {core.fmt_mib(in_use)} is swapped out but only "
            f"{core.fmt_mib(snap.available_mib)} of RAM is available. Free some memory first."
        )
    step(f"Deactivating {target}…", 15)
    run(["swapoff", target])


def allocate_file(path: str, size_mib: int, fs_type: str) -> None:
    """Create a fully-allocated, hole-free file of ``size_mib``."""
    size_bytes = size_mib * MIB
    if Path(path).exists():
        os.unlink(path)

    if fs_type == "btrfs" and shutil.which("btrfs"):
        step(f"Allocating {core.fmt_mib(size_mib)} on btrfs…", 35)
        try:
            run(["btrfs", "filesystem", "mkswapfile", "-s", f"{size_mib}M", path])
        except BaseException:
            Path(path).unlink(missing_ok=True)
            raise
        return

    if fs_type in ("ext2", "ext3", "ext4") and shutil.which("fallocate"):
        step(f"Allocating {core.fmt_mib(size_mib)}…", 35)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            run(["fallocate", "-l", str(size_bytes), path])
        except BaseException:
            Path(path).unlink(missing_ok=True)
            raise
        return

    # xfs/f2fs/unknown: fallocate can leave unwritten extents that swapon
    # rejects, so write the file out for real and report progress as we go.
    step(f"Writing {core.fmt_mib(size_mib)} (this filesystem needs a full write)…", 20)
    chunk = b"\0" * (4 * MIB)
    written = 0
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb", buffering=0) as fh:
            last = 0.0
            while written < size_bytes:
                n = min(len(chunk), size_bytes - written)
                fh.write(chunk[:n])
                written += n
                now = time.monotonic()
                if now - last > 0.25:
                    last = now
                    frac = written / size_bytes
                    step(f"Writing {core.fmt_bytes(written)} of {core.fmt_mib(size_mib)}…",
                         20 + int(frac * 30))
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise


def cmd_swapfile_apply(args) -> str:
    path = validate_swapfile_path(args.path)
    size_mib = validate_size(args.size_mib, maximum=MAX_SWAPFILE_MIB, label="swapfile")
    priority = validate_priority(args.priority)
    assert_safe_to_overwrite(path)

    state = core.swapfile_state(path)
    headroom = state.max_settable_mib
    if size_mib > headroom:
        raise HelperError(
            f"not enough free space: {core.fmt_mib(size_mib)} requested, "
            f"{core.fmt_mib(headroom)} available on {state.fs_type}"
        )

    step("Checking current state…", 5)
    swapoff(path)
    allocate_file(path, size_mib, state.fs_type)

    step("Setting permissions…", 55)
    os.chmod(path, 0o600)
    os.chown(path, 0, 0)

    step("Writing swap signature…", 65)
    run(["mkswap", "--label", "dynswap", path])

    step("Activating swap…", 80)
    swapon_cmd = ["swapon"]
    if priority >= 0:
        swapon_cmd += ["--priority", str(priority)]
    swapon_cmd.append(path)
    try:
        run(swapon_cmd)
    except HelperError:
        Path(path).unlink(missing_ok=True)
        raise

    if args.persist:
        step("Persisting to /etc/fstab…", 92)
        fstab_add(path, priority)
    else:
        fstab_remove(path)

    step("Done", 100)
    return f"{path} is active at {core.fmt_mib(size_mib)}"


def cmd_swapfile_remove(args) -> str:
    path = validate_swapfile_path(args.path)
    step("Deactivating…", 20)
    swapoff(path)
    step("Updating /etc/fstab…", 55)
    fstab_remove(path)
    if args.purge and Path(path).is_file():
        step("Deleting file…", 80)
        os.unlink(path)
        log(f"deleted {path}")
    step("Done", 100)
    return f"{path} removed" if args.purge else f"{path} deactivated (file kept)"


def cmd_swapfile_toggle(args) -> str:
    path = validate_swapfile_path(args.path)
    active = any(e.name == path for e in core.read_swaps())
    if active:
        swapoff(path)
        step("Done", 100)
        return f"{path} deactivated"
    if not Path(path).is_file():
        raise HelperError(f"{path} does not exist")
    step("Activating…", 50)
    cmd = ["swapon"]
    if args.priority >= 0:
        cmd += ["--priority", str(validate_priority(args.priority))]
    run(cmd + [path])
    step("Done", 100)
    return f"{path} activated"


# --------------------------------------------------------------------------- #
# zram operations
# --------------------------------------------------------------------------- #

# Everything here goes through sysfs rather than `zramctl`: as of util-linux
# 2.42 `zramctl --reset` hot-removes the device instead of just clearing it,
# which would delete /dev/zram0 out from under the very next command.
# Reading /sys/class/zram-control/hot_add creates a device and prints its id.
ZRAM_UNIT_TEMPLATE = """\
# Generated by dynswap — edit through the dynswap UI, not by hand.
# dynswap-size-mib={size_mib}
# dynswap-algo={algo}
[Unit]
Description=dynswap compressed swap on /dev/zram0
Documentation=https://docs.kernel.org/admin-guide/blockdev/zram.html
After=systemd-udevd.service
Wants=systemd-udevd.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=-/bin/sh -c 'modprobe zram'
ExecStartPre=-/bin/sh -c 'swapoff /dev/zram0'
ExecStartPre=-/bin/sh -c 'test -e /sys/block/zram0/reset && echo 1 > /sys/block/zram0/reset'
ExecStartPre=/bin/sh -c 'test -e /sys/block/zram0 || cat /sys/class/zram-control/hot_add > /dev/null'
ExecStartPre=-/bin/sh -c 'udevadm settle --timeout=10'
ExecStartPre=/bin/sh -c 'echo {algo} > /sys/block/zram0/comp_algorithm'
ExecStartPre=/bin/sh -c 'echo {size_bytes} > /sys/block/zram0/disksize'
ExecStart=/bin/sh -c 'mkswap -U clear /dev/zram0'
ExecStart=/bin/sh -c 'swapon --priority {priority} /dev/zram0'
ExecStop=-/bin/sh -c 'swapoff /dev/zram0'
ExecStop=-/bin/sh -c 'test -e /sys/block/zram0/reset && echo 1 > /sys/block/zram0/reset'

[Install]
WantedBy=multi-user.target
"""

GENERATOR_CONF_TEMPLATE = """\
# Generated by dynswap — edit through the dynswap UI, not by hand.
[zram0]
zram-size = {size_mib}
compression-algorithm = {algo}
swap-priority = {priority}
fs-type = swap
"""


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run(["systemctl", *args], check=check)


def zram_teardown_runtime(device: str = "/dev/zram0") -> None:
    """Free a zram device but keep it in place.

    Deliberately avoids `zramctl --reset`, which deletes the device outright on
    current util-linux; the sysfs reset clears it and leaves it usable.
    """
    if any(e.name == device for e in core.read_swaps()):
        swapoff(device)
    reset = Path(f"/sys/block/{Path(device).name}/reset")
    if reset.exists():
        try:
            reset.write_text("1\n")
            log(f"reset {device}")
        except OSError as exc:
            log(f"could not reset {device}: {exc}")


def zram_hot_remove(name: str = "zram0") -> None:
    """Delete a zram device so nothing is left listed at all."""
    control = Path("/sys/class/zram-control/hot_remove")
    if not control.exists() or not Path(f"/sys/block/{name}").exists():
        return
    index = name[len("zram"):]
    if not index.isdigit():
        return
    try:
        control.write_text(f"{index}\n")
        log(f"removed /dev/{name}")
    except OSError as exc:
        log(f"could not remove /dev/{name}: {exc}")


def unit_diagnostics(unit: str) -> str:
    """The unit's last words, so a failure explains itself in the UI."""
    try:
        proc = subprocess.run(
            ["journalctl", "-u", unit, "-n", "20", "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    for line in lines:
        log("  " + line)
    # systemd's own chatter says a unit failed but never why; the useful line is
    # whatever the process itself printed. systemd prefixes its messages with
    # the unit name, so strip that before deciding what is chatter.
    noise = (
        "Starting ", "Started ", "Stopping ", "Stopped ", "Consumed ",
        "Failed to start", "Failed with result", "Main process exited",
        "Control process exited", "Deactivated successfully",
        "Scheduled restart", "Triggering OnFailure",
    )
    prefix = unit + ": "
    cleaned = [line[len(prefix):] if line.startswith(prefix) else line
               for line in lines]
    detail = [line for line in cleaned if not line.startswith(noise)]
    if detail:
        return detail[-1]
    return cleaned[-1] if cleaned else ""


def cmd_zram_apply(args) -> str:
    size_mib = validate_size(args.size_mib, maximum=MAX_ZRAM_MIB, label="zram")
    algo = validate_algo(args.algo)
    priority = validate_priority(args.priority)

    snap = core.memory_snapshot()
    if size_mib > snap.total_mib * 4:
        raise HelperError(
            f"{core.fmt_mib(size_mib)} of zram on {core.fmt_mib(snap.total_mib)} of RAM is "
            "far past anything useful; keep it under 4× RAM."
        )

    state = core.zram_state()
    if state.available_algos and algo not in state.available_algos:
        raise HelperError(
            f"kernel does not offer {algo!r}; available: {', '.join(state.available_algos)}"
        )

    use_generator = state.generator_available and not args.force_unit
    step("Preparing…", 10)

    if use_generator:
        step("Writing zram-generator configuration…", 30)
        _write_atomic(ZRAM_GENERATOR_CONF, GENERATOR_CONF_TEMPLATE.format(
            size_mib=size_mib, algo=algo, priority=max(priority, 0)))
        step("Reloading systemd…", 50)
        systemctl("daemon-reload")
        step("Restarting zram device…", 70)
        zram_teardown_runtime()
        unit = "systemd-zram-setup@zram0.service"
        systemctl("restart", unit, check=False)
        backend = "zram-generator"
    else:
        step("Stopping any running zram device…", 25)
        # Disable before rewriting: systemctl reads the old [Install] section to
        # know which symlinks to clean up.
        systemctl("stop", ZRAM_UNIT_NAME, check=False)
        systemctl("disable", ZRAM_UNIT_NAME, check=False)
        zram_teardown_runtime()

        step("Installing systemd unit…", 45)
        _write_atomic(ZRAM_UNIT, ZRAM_UNIT_TEMPLATE.format(
            size_mib=size_mib, size_bytes=size_mib * MIB, algo=algo,
            priority=max(priority, 0)))
        systemctl("daemon-reload")

        step("Starting zram device…", 70)
        systemctl("enable", ZRAM_UNIT_NAME)
        systemctl("start", ZRAM_UNIT_NAME, check=False)
        unit = ZRAM_UNIT_NAME
        backend = "dynswap unit"

    time.sleep(0.4)
    active = [e for e in core.read_swaps() if e.name.startswith("/dev/zram")]
    if not active:
        detail = unit_diagnostics(unit)
        raise HelperError(
            f"zram did not come up: {detail}" if detail
            else "zram did not come up — see the log for details")
    step("Done", 100)
    return f"zram active at {core.fmt_mib(active[0].size_mib)} using {algo} ({backend})"


def cmd_zram_remove(args) -> str:
    step("Stopping zram…", 20)
    systemctl("stop", ZRAM_UNIT_NAME, check=False)
    systemctl("stop", "systemd-zram-setup@zram0.service", check=False)
    for entry in core.read_swaps():
        if entry.name.startswith("/dev/zram"):
            zram_teardown_runtime(entry.name)
    for sysdir in sorted(Path("/sys/block").glob("zram*")):
        zram_teardown_runtime(f"/dev/{sysdir.name}")
        zram_hot_remove(sysdir.name)
    step("Removing configuration…", 60)
    if Path(ZRAM_UNIT).exists():
        systemctl("disable", ZRAM_UNIT_NAME, check=False)
        Path(ZRAM_UNIT).unlink()
        log(f"removed {ZRAM_UNIT}")
    if Path(ZRAM_GENERATOR_CONF).exists():
        header = Path(ZRAM_GENERATOR_CONF).read_text(errors="replace")
        if "Generated by dynswap" in header:
            Path(ZRAM_GENERATOR_CONF).unlink()
            log(f"removed {ZRAM_GENERATOR_CONF}")
        else:
            log(f"left {ZRAM_GENERATOR_CONF} alone — it was not written by dynswap")
    systemctl("daemon-reload", check=False)
    step("Done", 100)
    return "zram swap removed"


# --------------------------------------------------------------------------- #
# sysctl tuning
# --------------------------------------------------------------------------- #

def cmd_tuning_apply(args) -> str:
    values: dict[str, int] = {}
    for item in args.set or []:
        key, _, raw = item.partition("=")
        key = key.strip()
        if key not in TUNABLES:
            raise HelperError(f"unknown tunable: {key!r}")
        try:
            value = int(raw)
        except ValueError:
            raise HelperError(f"{key} needs an integer, got {raw!r}") from None
        low, high = TUNABLES[key]
        if not low <= value <= high:
            raise HelperError(f"{key} must be between {low} and {high}")
        values[key] = value
    if not values:
        raise HelperError("nothing to apply")

    total = len(values)
    for index, (key, value) in enumerate(sorted(values.items()), start=1):
        step(f"Setting {key} = {value}…", int(index / total * 70))
        target = Path("/proc/sys/" + key.replace(".", "/"))
        if not target.exists():
            log(f"skipped {key}: not supported by this kernel")
            continue
        target.write_text(f"{value}\n")

    if args.persist:
        step("Persisting to /etc/sysctl.d…", 85)
        body = "# Generated by dynswap — edit through the dynswap UI, not by hand.\n"
        body += "".join(f"{k} = {v}\n" for k, v in sorted(values.items()))
        Path(SYSCTL_FILE).parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(SYSCTL_FILE, body)
        log(f"wrote {SYSCTL_FILE}")
    elif Path(SYSCTL_FILE).exists():
        Path(SYSCTL_FILE).unlink()
        log(f"removed {SYSCTL_FILE}")

    step("Done", 100)
    return f"applied {total} setting{'s' if total != 1 else ''}"


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dynswap-helper",
        description="Privileged swap operations for dynswap. Not meant to be run by hand.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("swapfile-apply", help="create or resize a swapfile")
    p.add_argument("--path", default=core.DEFAULT_SWAPFILE)
    p.add_argument("--size-mib", type=int, required=True)
    p.add_argument("--priority", type=int, default=-1)
    p.add_argument("--persist", action="store_true")
    p.set_defaults(func=cmd_swapfile_apply)

    p = sub.add_parser("swapfile-remove", help="deactivate and optionally delete a swapfile")
    p.add_argument("--path", default=core.DEFAULT_SWAPFILE)
    p.add_argument("--purge", action="store_true")
    p.set_defaults(func=cmd_swapfile_remove)

    p = sub.add_parser("swapfile-toggle", help="swapon/swapoff an existing swapfile")
    p.add_argument("--path", default=core.DEFAULT_SWAPFILE)
    p.add_argument("--priority", type=int, default=-1)
    p.set_defaults(func=cmd_swapfile_toggle)

    p = sub.add_parser("zram-apply", help="configure the compressed zram swap device")
    p.add_argument("--size-mib", type=int, required=True)
    p.add_argument("--algo", default="zstd")
    p.add_argument("--priority", type=int, default=100)
    p.add_argument("--force-unit", action="store_true",
                   help="use dynswap's own systemd unit even if zram-generator exists")
    p.set_defaults(func=cmd_zram_apply)

    p = sub.add_parser("zram-remove", help="tear down zram swap and its configuration")
    p.set_defaults(func=cmd_zram_remove)

    p = sub.add_parser("tuning-apply", help="set vm.* sysctl tunables")
    p.add_argument("--set", action="append", metavar="KEY=VALUE")
    p.add_argument("--persist", action="store_true")
    p.set_defaults(func=cmd_tuning_apply)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if os.geteuid() != 0:
        emit("done", ok=False, msg="dynswap-helper must run as root")
        return 1
    try:
        message = args.func(args)
    except HelperError as exc:
        emit("done", ok=False, msg=str(exc))
        return 1
    except KeyboardInterrupt:
        emit("done", ok=False, msg="interrupted")
        return 130
    except Exception as exc:  # noqa: BLE001 - surface anything unexpected to the UI
        emit("done", ok=False, msg=f"{type(exc).__name__}: {exc}")
        return 1
    emit("done", ok=True, msg=message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
