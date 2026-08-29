"""dynswap.cli — argument parsing and frontend selection."""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__, core


def _graphical_session() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


def _gtk_available() -> bool:
    try:
        import gi  # noqa: F401
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        return True
    except (ImportError, ValueError):
        return False


def print_status() -> int:
    snap = core.memory_snapshot()
    swaps = core.read_swaps()
    print(f"RAM    {core.fmt_mib(snap.used_mib)} used of {core.fmt_mib(snap.total_mib)} "
          f"({snap.used_fraction * 100:.0f}%)")
    if swaps:
        print(f"Swap   {core.fmt_mib(snap.swap_used_mib)} used of "
              f"{core.fmt_mib(snap.swap_total_mib)}")
        for entry in swaps:
            kind = "zram" if entry.is_zram else entry.kind
            print(f"       {entry.name:<24} {core.fmt_mib(entry.size_mib):>10}  "
                  f"{kind:<10} priority {entry.priority}")
    else:
        print("Swap   none configured")

    state = core.swapfile_state()
    print(f"\nSwapfile  {state.path} — "
          f"{'active' if state.active else 'present' if state.exists else 'absent'}"
          f"{', in fstab' if state.in_fstab else ''}")
    print(f"          {state.fs_type}, {core.fmt_mib(state.free_mib)} free")

    zram = core.zram_state()
    device = zram.primary
    if device:
        print(f"zram      {device.path} — {core.fmt_mib(device.disksize_mib)}, "
              f"{device.algorithm}, ratio {device.ratio:.1f}×")
    else:
        print("zram      not configured")

    tuning = core.tuning_state()
    print("\nTunables  " + "  ".join(f"{k}={v}" for k, v in sorted(tuning.items())))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dynswap",
        description="Set the amount of swap space on this system.",
        epilog="With no options dynswap opens the GUI on a desktop session, "
               "and the TUI otherwise.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("-t", "--tui", action="store_true", help="force the terminal interface")
    mode.add_argument("-g", "--gui", action="store_true", help="force the graphical interface")
    mode.add_argument("-s", "--status", action="store_true",
                      help="print current swap configuration and exit")
    parser.add_argument("-V", "--version", action="version",
                        version=f"dynswap {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.status:
        return print_status()

    want_gui = args.gui or (not args.tui and _graphical_session() and _gtk_available())

    if want_gui:
        if not _gtk_available():
            print("GTK 4 / libadwaita Python bindings are missing.\n"
                  "On Arch: sudo pacman -S python-gobject gtk4 libadwaita\n"
                  "Falling back to the terminal interface.", file=sys.stderr)
        else:
            from .gui import run as run_gui
            return run_gui()

    if not sys.stdout.isatty():
        print("No terminal available for the TUI; use --status or --gui.", file=sys.stderr)
        return 2
    from .tui import run as run_tui
    return run_tui()
