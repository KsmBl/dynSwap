"""dynswap.core — read-only introspection of the system's swap configuration.

Nothing in this module mutates system state; every privileged operation lives in
``dynswap.helper`` so that the UI processes can stay unprivileged.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MIB = 1024 * 1024

DEFAULT_SWAPFILE = "/swapfile"
FSTAB = "/etc/fstab"
SYSCTL_FILE = "/etc/sysctl.d/99-dynswap.conf"
ZRAM_GENERATOR_CONF = "/etc/systemd/zram-generator.conf"
ZRAM_UNIT = "/etc/systemd/system/dynswap-zram.service"
ZRAM_UNIT_NAME = "dynswap-zram.service"

FALLBACK_ALGOS = ["zstd", "lz4", "lzo-rle", "lzo", "lz4hc", "deflate", "842"]

TUNABLES = {
    "vm.swappiness": (0, 200),
    "vm.vfs_cache_pressure": (0, 1000),
    "vm.page-cluster": (0, 8),
    "vm.watermark_boost_factor": (0, 30000),
}


# --------------------------------------------------------------------------- #
# formatting helpers
# --------------------------------------------------------------------------- #

def fmt_bytes(n: float) -> str:
    """Human-readable size, binary units, at most one decimal."""
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit in ("B", "KiB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def fmt_mib(mib: float) -> str:
    return fmt_bytes(mib * MIB)


def parse_size_to_mib(text: str) -> int | None:
    """Parse '4G', '512M', '2.5 GiB', '4096' (bare number == MiB)."""
    m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([kKmMgGtT]?)(?:i?[bB])?\s*", text or "")
    if not m:
        return None
    value = float(m.group(1))
    mult = {"": 1, "k": 1 / 1024, "m": 1, "g": 1024, "t": 1024 * 1024}[m.group(2).lower()]
    mib = value * mult
    return int(round(mib)) if mib >= 0 else None


# --------------------------------------------------------------------------- #
# memory / swap state
# --------------------------------------------------------------------------- #

@dataclass
class SwapEntry:
    name: str
    kind: str          # "file" | "partition"
    size_mib: int
    used_mib: int
    priority: int

    @property
    def is_zram(self) -> bool:
        return self.name.startswith("/dev/zram")


def read_swaps() -> list[SwapEntry]:
    entries: list[SwapEntry] = []
    try:
        lines = Path("/proc/swaps").read_text().splitlines()[1:]
    except OSError:
        return entries
    for line in lines:
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            entries.append(SwapEntry(
                name=parts[0],
                kind=parts[1],
                size_mib=int(parts[2]) // 1024,
                used_mib=int(parts[3]) // 1024,
                priority=int(parts[4]),
            ))
        except ValueError:
            continue
    return entries


def meminfo() -> dict[str, int]:
    """/proc/meminfo in KiB, keys lowercased with underscores."""
    out: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            value = rest.strip().split()
            if value and value[0].isdigit():
                out[key.strip().lower().replace("(", "_").replace(")", "")] = int(value[0])
    except OSError:
        pass
    return out


@dataclass
class MemorySnapshot:
    total_mib: int = 0
    available_mib: int = 0
    used_mib: int = 0
    cached_mib: int = 0
    swap_total_mib: int = 0
    swap_free_mib: int = 0

    @property
    def swap_used_mib(self) -> int:
        return max(0, self.swap_total_mib - self.swap_free_mib)

    @property
    def used_fraction(self) -> float:
        return self.used_mib / self.total_mib if self.total_mib else 0.0

    @property
    def swap_fraction(self) -> float:
        return self.swap_used_mib / self.swap_total_mib if self.swap_total_mib else 0.0


def memory_snapshot() -> MemorySnapshot:
    mi = meminfo()
    total = mi.get("memtotal", 0) // 1024
    available = mi.get("memavailable", 0) // 1024
    cached = (mi.get("cached", 0) + mi.get("sreclaimable", 0) - mi.get("shmem", 0)) // 1024
    return MemorySnapshot(
        total_mib=total,
        available_mib=available,
        used_mib=max(0, total - available),
        cached_mib=max(0, cached),
        swap_total_mib=mi.get("swaptotal", 0) // 1024,
        swap_free_mib=mi.get("swapfree", 0) // 1024,
    )


# --------------------------------------------------------------------------- #
# filesystem facts
# --------------------------------------------------------------------------- #

def filesystem_type(path: str) -> str:
    """Filesystem type of the mount that would contain ``path``."""
    probe = Path(path)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        out = subprocess.run(
            ["findmnt", "-no", "FSTYPE", "-T", str(probe)],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def disk_free_mib(path: str) -> int:
    probe = Path(path)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        st = os.statvfs(probe)
        return int(st.f_bavail * st.f_frsize) // MIB
    except OSError:
        return 0


def fstab_swap_entries() -> list[str]:
    """Device/file fields of every swap line in fstab."""
    found: list[str] = []
    try:
        for raw in Path(FSTAB).read_text().splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 3 and parts[2] == "swap":
                found.append(parts[0])
    except OSError:
        pass
    return found


# --------------------------------------------------------------------------- #
# swapfile
# --------------------------------------------------------------------------- #

@dataclass
class SwapfileState:
    path: str = DEFAULT_SWAPFILE
    exists: bool = False
    size_mib: int = 0
    active: bool = False
    active_size_mib: int = 0
    used_mib: int = 0
    priority: int = -2
    in_fstab: bool = False
    fs_type: str = "unknown"
    free_mib: int = 0
    permissions_ok: bool = True

    @property
    def max_settable_mib(self) -> int:
        """Largest size we could grow to: free space plus what we already own."""
        return self.free_mib + (self.size_mib if self.exists else 0)


def swapfile_state(path: str = DEFAULT_SWAPFILE) -> SwapfileState:
    st = SwapfileState(path=path)
    st.fs_type = filesystem_type(path)
    st.free_mib = disk_free_mib(path)
    st.in_fstab = path in fstab_swap_entries()
    try:
        stat = os.stat(path)
        st.exists = True
        st.size_mib = int(stat.st_size) // MIB
        st.permissions_ok = (stat.st_mode & 0o077) == 0
    except OSError:
        pass
    for entry in read_swaps():
        if entry.name == path:
            st.active = True
            st.active_size_mib = entry.size_mib
            st.used_mib = entry.used_mib
            st.priority = entry.priority
    return st


def discovered_swapfiles() -> list[str]:
    """Swapfiles that exist on this system besides the default path."""
    seen: list[str] = []
    for entry in read_swaps():
        if entry.kind == "file" and entry.name not in seen:
            seen.append(entry.name)
    for spec in fstab_swap_entries():
        if spec.startswith("/") and not spec.startswith("/dev/") and spec not in seen:
            seen.append(spec)
    if DEFAULT_SWAPFILE not in seen and Path(DEFAULT_SWAPFILE).exists():
        seen.append(DEFAULT_SWAPFILE)
    return seen


# --------------------------------------------------------------------------- #
# zram
# --------------------------------------------------------------------------- #

@dataclass
class ZramDevice:
    name: str                  # "zram0"
    disksize_mib: int = 0
    algorithm: str = "?"
    data_mib: float = 0.0      # uncompressed bytes stored
    compressed_mib: float = 0.0
    total_mib: float = 0.0     # memory actually consumed
    active_swap: bool = False
    priority: int = 0

    @property
    def path(self) -> str:
        return f"/dev/{self.name}"

    @property
    def ratio(self) -> float:
        return (self.data_mib / self.compressed_mib) if self.compressed_mib > 0 else 0.0


@dataclass
class ZramState:
    devices: list[ZramDevice] = field(default_factory=list)
    module_loaded: bool = False
    generator_available: bool = False
    generator_configured: bool = False
    configured_size_mib: int = 0
    configured_algo: str = ""
    unit_installed: bool = False
    available_algos: list[str] = field(default_factory=lambda: list(FALLBACK_ALGOS))

    @property
    def primary(self) -> ZramDevice | None:
        return self.devices[0] if self.devices else None


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def _parse_comp_algorithm(raw: str) -> tuple[str, list[str]]:
    """'lzo lz4 [zstd]' -> ('zstd', ['lzo', 'lz4', 'zstd'])."""
    tokens = raw.split()
    current = ""
    names: list[str] = []
    for tok in tokens:
        name = tok.strip("[]")
        names.append(name)
        if tok.startswith("["):
            current = name
    return current or (names[0] if names else ""), names


def zram_state() -> ZramState:
    st = ZramState()
    st.generator_available = _zram_generator_available()
    st.unit_installed = Path(ZRAM_UNIT).exists()

    swaps = {e.name: e for e in read_swaps()}
    for sysdir in sorted(Path("/sys/block").glob("zram*")):
        st.module_loaded = True
        dev = ZramDevice(name=sysdir.name)
        disksize = _read(str(sysdir / "disksize"))
        dev.disksize_mib = int(disksize) // MIB if disksize.isdigit() else 0
        algo_raw = _read(str(sysdir / "comp_algorithm"))
        if algo_raw:
            dev.algorithm, algos = _parse_comp_algorithm(algo_raw)
            if algos:
                st.available_algos = algos
        mm = _read(str(sysdir / "mm_stat")).split()
        if len(mm) >= 3:
            try:
                dev.data_mib = int(mm[0]) / MIB
                dev.compressed_mib = int(mm[1]) / MIB
                dev.total_mib = int(mm[2]) / MIB
            except ValueError:
                pass
        entry = swaps.get(dev.path)
        if entry:
            dev.active_swap = True
            dev.priority = entry.priority
        st.devices.append(dev)

    size, algo = _read_generator_conf()
    if size:
        st.generator_configured = True
        st.configured_size_mib = size
        st.configured_algo = algo
    elif st.unit_installed:
        size, algo = _read_unit_conf()
        st.configured_size_mib = size
        st.configured_algo = algo
    return st


def _zram_generator_available() -> bool:
    for candidate in (
        "/usr/lib/systemd/system-generators/zram-generator",
        "/lib/systemd/system-generators/zram-generator",
    ):
        if Path(candidate).exists():
            return True
    return False


def _read_generator_conf() -> tuple[int, str]:
    """Size in MiB and algorithm from zram-generator.conf, if it is a plain size."""
    try:
        text = Path(ZRAM_GENERATOR_CONF).read_text()
    except OSError:
        return 0, ""
    size_mib, algo = 0, ""
    in_zram0 = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.startswith("[") and line.endswith("]"):
            in_zram0 = line.strip("[]").strip().lower() in ("zram0", "zram-generator")
            continue
        if not in_zram0 or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().lower(), value.strip()
        if key in ("zram-size", "zram-fraction"):
            parsed = parse_size_to_mib(value)
            if parsed:
                size_mib = parsed
        elif key == "compression-algorithm":
            algo = value.split()[0] if value else ""
    return size_mib, algo


def _read_unit_conf() -> tuple[int, str]:
    """Recover size/algo from a dynswap-managed zram unit."""
    try:
        text = Path(ZRAM_UNIT).read_text()
    except OSError:
        return 0, ""
    size = re.search(r"# dynswap-size-mib=(\d+)", text)
    algo = re.search(r"# dynswap-algo=([\w.-]+)", text)
    return (int(size.group(1)) if size else 0), (algo.group(1) if algo else "")


# --------------------------------------------------------------------------- #
# sysctl tunables
# --------------------------------------------------------------------------- #

def sysctl_get(key: str) -> int | None:
    path = "/proc/sys/" + key.replace(".", "/")
    raw = _read(path)
    try:
        return int(raw.split()[0])
    except (ValueError, IndexError):
        return None


def tuning_state() -> dict[str, int]:
    return {key: (sysctl_get(key) or 0) for key in TUNABLES}


def tuning_persisted() -> bool:
    return Path(SYSCTL_FILE).exists()


# --------------------------------------------------------------------------- #
# recommendations
# --------------------------------------------------------------------------- #

def recommended_swapfile_mib(total_ram_mib: int) -> int:
    """A sane default: enough to be useful, never absurd on a big machine."""
    if total_ram_mib <= 2048:
        return max(1024, total_ram_mib * 2)
    if total_ram_mib <= 8192:
        return total_ram_mib
    return max(8192, total_ram_mib // 2)


def recommended_zram_mib(total_ram_mib: int) -> int:
    """Half of RAM, capped at 8 GiB — the common zram-generator default shape."""
    return min(total_ram_mib // 2, 8192)


def which(name: str) -> str | None:
    return shutil.which(name)
