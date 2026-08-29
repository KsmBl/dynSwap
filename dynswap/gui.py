"""dynswap.gui — a GTK 4 / libadwaita interface for reading and setting swap.

Layout: a persistent overview card with custom-drawn memory meters, an
Adw.ViewStack of one page per backend, and a progress dialog that streams the
privileged helper's events while it works.
"""
from __future__ import annotations

import math
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, GObject, Gtk  # noqa: E402

from . import __version__, actions, core  # noqa: E402

APP_ID = "de.synthelicz.dynSwap"

CSS = """
.overview-card {
  background: linear-gradient(150deg,
              alpha(@accent_bg_color, .12), alpha(@accent_bg_color, .02));
  border: 1px solid alpha(@borders, .6);
  border-radius: 16px;
  padding: 16px 18px;
}
.meter-label { font-weight: 700; letter-spacing: .06em; font-size: .82em; }
.meter-value { font-variant-numeric: tabular-nums; }
.readout {
  font-variant-numeric: tabular-nums;
  font-weight: 700;
  min-width: 5.5em;
}
.hint { font-size: .85em; opacity: .62; }
.preset-bar button { min-width: 3.6em; }
.log-view { font-family: monospace; font-size: .85em; }
"""

# meter colours: (r, g, b) in 0..1
ACCENT = (0.55, 0.40, 0.85)
BLUE = (0.31, 0.56, 0.90)
GREEN = (0.35, 0.74, 0.47)
AMBER = (0.94, 0.74, 0.30)
RED = (0.88, 0.36, 0.36)
CACHE = (0.42, 0.52, 0.66)


def load_color(fraction: float) -> tuple[float, float, float]:
    if fraction < 0.60:
        return GREEN
    if fraction < 0.85:
        return AMBER
    return RED


def _rounded_rect(cr, x: float, y: float, width: float, height: float, radius: float) -> None:
    radius = min(radius, height / 2, width / 2)
    cr.new_sub_path()
    cr.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    cr.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    cr.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    cr.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    cr.close_path()


class Meter(Gtk.DrawingArea):
    """A rounded bar with a primary fill and an optional dimmer second band."""

    def __init__(self):
        super().__init__()
        self.fraction = 0.0
        self.second = 0.0
        self.color = GREEN
        self.set_content_height(12)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

    def update(self, fraction: float, second: float = 0.0,
               color: tuple[float, float, float] | None = None) -> None:
        self.fraction = min(max(fraction, 0.0), 1.0)
        self.second = min(max(second, 0.0), 1.0 - self.fraction)
        self.color = color or load_color(self.fraction)
        self.queue_draw()

    def _draw(self, area, cr, width, height) -> None:
        dark = Adw.StyleManager.get_default().get_dark()
        radius = height / 2

        _rounded_rect(cr, 0, 0, width, height, radius)
        if dark:
            cr.set_source_rgba(1, 1, 1, 0.10)
        else:
            cr.set_source_rgba(0, 0, 0, 0.09)
        cr.fill()

        if self.second > 0:
            span = width * (self.fraction + self.second)
            if span > 1:
                _rounded_rect(cr, 0, 0, span, height, radius)
                cr.set_source_rgba(*CACHE, 0.55)
                cr.fill()

        span = width * self.fraction
        if span > 1:
            _rounded_rect(cr, 0, 0, span, height, radius)
            red, green, blue = self.color
            cr.set_source_rgba(red, green, blue, 1.0)
            cr.fill()


class MeterRow(Gtk.Box):
    """One labelled meter line inside the overview card."""

    def __init__(self, label: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.name = Gtk.Label(label=label, xalign=0)
        self.name.add_css_class("meter-label")
        self.name.add_css_class("dim-label")
        self.value = Gtk.Label(xalign=1)
        self.value.add_css_class("meter-value")
        self.value.set_hexpand(True)
        header.append(self.name)
        header.append(self.value)
        self.meter = Meter()
        self.append(header)
        self.append(self.meter)


class ProgressDialog(Adw.Dialog):
    """Modal progress + live log while the helper runs."""

    def __init__(self, title: str):
        super().__init__()
        self.set_title(title)
        self.set_can_close(False)
        self.set_content_width(480)

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar(show_end_title_buttons=False,
                                       show_start_title_buttons=False))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                      margin_top=6, margin_bottom=18, margin_start=18, margin_end=18)
        self.status = Gtk.Label(label="Starting…", xalign=0, wrap=True)
        self.bar = Gtk.ProgressBar(show_text=False)
        self.buffer = Gtk.TextBuffer()
        text = Gtk.TextView(buffer=self.buffer, editable=False, cursor_visible=False,
                            monospace=True, left_margin=8, right_margin=8,
                            top_margin=6, bottom_margin=6)
        text.add_css_class("log-view")
        scroller = Gtk.ScrolledWindow(min_content_height=150, vexpand=True)
        scroller.set_child(text)
        scroller.add_css_class("card")
        expander = Gtk.Expander(label="Details")
        expander.set_child(scroller)

        box.append(self.status)
        box.append(self.bar)
        box.append(expander)
        view.set_content(box)
        self.set_child(view)

    def step(self, message: str, percent: int) -> None:
        self.status.set_label(message)
        self.bar.set_fraction(min(max(percent, 0), 100) / 100)

    def log(self, line: str) -> None:
        end = self.buffer.get_end_iter()
        self.buffer.insert(end, line.rstrip() + "\n")


# --------------------------------------------------------------------------- #
# reusable rows
# --------------------------------------------------------------------------- #

class ScaleRow(Adw.ActionRow):
    """A settings row whose control is a slider plus a right-aligned readout."""

    def __init__(self, title: str, subtitle: str, minimum: int, maximum: int,
                 step: int, value: int, formatter, on_change=None):
        super().__init__(title=title, subtitle=subtitle)
        self.formatter = formatter
        self.on_change = on_change
        self._guard = False

        self.adjustment = Gtk.Adjustment(lower=minimum, upper=maximum, value=value,
                                         step_increment=step, page_increment=step * 4)
        self.scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL,
                               adjustment=self.adjustment, draw_value=False,
                               hexpand=True, width_request=120)
        self.scale.set_valign(Gtk.Align.CENTER)
        self.scale.connect("value-changed", self._changed)

        self.readout = Gtk.Label(xalign=1)
        self.readout.add_css_class("readout")
        self.readout.set_valign(Gtk.Align.CENTER)

        self.add_suffix(self.scale)
        self.add_suffix(self.readout)
        self._refresh_readout()

    @property
    def value(self) -> int:
        return int(round(self.adjustment.get_value()))

    def set_value(self, value: int, *, silent: bool = False) -> None:
        self._guard = silent
        self.adjustment.set_value(value)
        self._guard = False
        self._refresh_readout()

    def set_upper(self, upper: int) -> None:
        self.adjustment.set_upper(max(upper, self.adjustment.get_lower() + 1))

    def add_marks(self, marks: list[int]) -> None:
        for mark in marks:
            if mark <= self.adjustment.get_upper():
                self.scale.add_mark(mark, Gtk.PositionType.BOTTOM, None)

    def _refresh_readout(self) -> None:
        self.readout.set_label(self.formatter(self.value))

    def _changed(self, _scale) -> None:
        self._refresh_readout()
        if self.on_change and not self._guard:
            self.on_change(self.value)


def preset_bar(labels: list[tuple[str, int]], callback) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0,
                  halign=Gtk.Align.CENTER, margin_top=6)
    box.add_css_class("linked")
    box.add_css_class("preset-bar")
    for label, value in labels:
        button = Gtk.Button(label=label)
        button.connect("clicked", lambda _b, v=value: callback(v))
        box.append(button)
    return box


def action_bar(buttons: list[tuple[str, str, object]]) -> tuple[Gtk.Box, list[Gtk.Button]]:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
                  halign=Gtk.Align.END, margin_top=12)
    widgets = []
    for label, style, callback in buttons:
        button = Gtk.Button(label=label)
        if style:
            button.add_css_class(style)
        button.connect("clicked", lambda _b, cb=callback: cb())
        box.append(button)
        widgets.append(button)
    return box, widgets


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #

class SwapfileGroup(Adw.PreferencesPage):
    title = "Swapfile"
    icon = "drive-harddisk-symbolic"

    def __init__(self, window: "Window"):
        super().__init__()
        self.window = window
        state = core.swapfile_state()
        snapshot = core.memory_snapshot()

        status = Adw.PreferencesGroup(title="Current state")
        self.row_status = Adw.ActionRow(title="Swapfile", subtitle="—")
        self.row_disk = Adw.ActionRow(title="Filesystem", subtitle="—")
        status.add(self.row_status)
        status.add(self.row_disk)

        settings = Adw.PreferencesGroup(
            title="Configuration",
            description="Changing the size rebuilds the swapfile: it is switched "
                        "off, reallocated and switched back on.")
        self.row_path = Adw.EntryRow(title="Location")
        self.row_path.set_text(state.path)
        self.row_path.connect("apply", lambda _r: self.window.reload())
        self.row_path.set_show_apply_button(True)

        default = state.size_mib or core.recommended_swapfile_mib(snapshot.total_mib)
        self.row_size = ScaleRow(
            "Size", "How much disk to hand to the kernel",
            0, max(1024, state.max_settable_mib), 256, default,
            lambda v: core.fmt_mib(v) if v else "Off",
            on_change=lambda _v: self.window.touch())
        self.row_size.add_marks([1024, 2048, 4096, 8192, 16384, 32768])

        self.row_priority = Adw.SpinRow(
            title="Priority",
            subtitle="Higher wins; −1 lets the kernel decide",
            adjustment=Gtk.Adjustment(lower=-1, upper=100, value=state.priority
                                      if state.priority >= 0 else -1,
                                      step_increment=1, page_increment=10))
        self.row_persist = Adw.SwitchRow(
            title="Enable at boot",
            subtitle="Adds an entry to /etc/fstab",
            active=state.in_fstab or not state.exists)

        settings.add(self.row_path)
        settings.add(self.row_size)
        settings.add(self.row_priority)
        settings.add(self.row_persist)

        wrapper = Adw.PreferencesGroup()
        wrapper.add(preset_bar(
            [("1 GiB", 1024), ("2 GiB", 2048), ("4 GiB", 4096),
             ("8 GiB", 8192), ("16 GiB", 16384)],
            lambda v: (self.row_size.set_value(v), self.window.touch())))
        self.pending = Gtk.Label(xalign=0.5, wrap=True, margin_top=10)
        self.pending.add_css_class("hint")
        wrapper.add(self.pending)
        bar, (self.button_remove, self.button_toggle, _) = action_bar([
            ("Remove swapfile", "destructive-action", self.remove),
            ("Turn off", "", self.toggle),
            ("Apply", "suggested-action", self.apply),
        ])
        wrapper.add(bar)

        self.add(status)
        self.add(settings)
        self.add(wrapper)

    # -- state ------------------------------------------------------------- #

    @property
    def path(self) -> str:
        return self.row_path.get_text().strip() or core.DEFAULT_SWAPFILE

    def refresh(self, dirty: bool) -> None:
        state = core.swapfile_state(self.path)
        if state.active:
            self.row_status.set_subtitle(
                f"Active · {core.fmt_mib(state.active_size_mib)} · "
                f"{core.fmt_mib(state.used_mib)} in use · priority {state.priority}")
        elif state.exists:
            self.row_status.set_subtitle(
                f"Present but switched off · {core.fmt_mib(state.size_mib)}")
        else:
            self.row_status.set_subtitle("No swapfile at this location")
        self.row_disk.set_subtitle(
            f"{state.fs_type} · {core.fmt_mib(state.free_mib)} free"
            + (" · listed in /etc/fstab" if state.in_fstab else ""))

        self.button_remove.set_sensitive(state.exists or state.in_fstab)
        self.button_toggle.set_sensitive(state.exists)
        self.button_toggle.set_label("Turn off" if state.active else "Turn on")

        self.row_size.set_upper(max(1024, state.max_settable_mib))
        if not dirty:
            self.row_persist.set_active(state.in_fstab or not state.exists)
            if state.exists:
                self.row_size.set_value(state.size_mib, silent=True)

        target = self.row_size.value
        after = state.free_mib - (target - (state.size_mib if state.exists else 0))
        if after < 0:
            self.pending.set_label(
                f"Not enough space — {core.fmt_mib(-after)} short on {state.fs_type}")
        elif not state.exists and target:
            self.pending.set_label(
                f"Will create {self.path} at {core.fmt_mib(target)}, "
                f"leaving {core.fmt_mib(after)} free")
        elif state.exists and target != state.size_mib:
            delta = target - state.size_mib
            self.pending.set_label(
                f"Will {'grow' if delta > 0 else 'shrink'} {self.path} by "
                f"{core.fmt_mib(abs(delta))}, leaving {core.fmt_mib(after)} free")
        else:
            self.pending.set_label("Matches what is on disk")

    # -- actions ----------------------------------------------------------- #

    def apply(self) -> None:
        if self.row_size.value < 8:
            self.window.toast("Size is zero — use Remove swapfile instead")
            return
        persist = self.row_persist.get_active()
        self.window.confirm(
            "Apply swapfile change?",
            f"{self.path} will be set to {core.fmt_mib(self.row_size.value)}"
            + (" and added to /etc/fstab." if persist else ".")
            + " Existing swap at this path is switched off and rebuilt.",
            "Apply",
            actions.swapfile_apply(self.path, self.row_size.value,
                                   int(self.row_priority.get_value()), persist))

    def toggle(self) -> None:
        active = core.swapfile_state(self.path).active
        self.window.run_request(
            actions.swapfile_toggle(self.path, int(self.row_priority.get_value())),
            "Switching swap off" if active else "Switching swap on")

    def remove(self) -> None:
        self.window.confirm(
            "Remove this swapfile?",
            f"{self.path} will be switched off, deleted from disk and removed "
            "from /etc/fstab.",
            "Remove", actions.swapfile_remove(self.path, purge=True),
            destructive=True)


class ZramGroup(Adw.PreferencesPage):
    title = "zram"
    icon = "media-flash-symbolic"

    def __init__(self, window: "Window"):
        super().__init__()
        self.window = window
        state = core.zram_state()
        snapshot = core.memory_snapshot()
        device = state.primary

        status = Adw.PreferencesGroup(
            title="Current state",
            description="zram is swap held in RAM and compressed on the fly — "
                        "much faster than disk, at the cost of some memory.")
        self.row_status = Adw.ActionRow(title="Device", subtitle="—")
        self.row_ratio = Adw.ActionRow(title="Compression", subtitle="—")
        self.row_backend = Adw.ActionRow(title="Managed by", subtitle="—")
        status.add(self.row_status)
        status.add(self.row_ratio)
        status.add(self.row_backend)

        settings = Adw.PreferencesGroup(title="Configuration")
        default = ((device.disksize_mib if device else 0) or state.configured_size_mib
                   or core.recommended_zram_mib(snapshot.total_mib))
        self.row_size = ScaleRow(
            "Size", "The uncompressed capacity the device advertises",
            0, max(1024, snapshot.total_mib * 2), 256, default,
            lambda v: core.fmt_mib(v) if v else "Off",
            on_change=lambda _v: self.window.touch())
        self.row_size.add_marks([1024, 2048, 4096, 8192, 16384])

        self.algos = list(state.available_algos)
        self.row_algo = Adw.ComboRow(
            title="Algorithm",
            subtitle="zstd compresses hardest; lz4 is fastest",
            model=Gtk.StringList.new(self.algos))
        current = (device.algorithm if device else "") or state.configured_algo or "zstd"
        if current in self.algos:
            self.row_algo.set_selected(self.algos.index(current))
        self.row_algo.connect("notify::selected", lambda *_: self.window.touch())

        self.row_priority = Adw.SpinRow(
            title="Priority",
            subtitle="Keep this above the swapfile so RAM is used first",
            adjustment=Gtk.Adjustment(
                lower=0, upper=200,
                value=(device.priority if device and device.priority else 100),
                step_increment=5, page_increment=25))

        settings.add(self.row_size)
        settings.add(self.row_algo)
        settings.add(self.row_priority)

        wrapper = Adw.PreferencesGroup()
        wrapper.add(preset_bar(
            [("¼ RAM", max(256, snapshot.total_mib // 4)),
             ("½ RAM", max(256, snapshot.total_mib // 2)),
             ("1× RAM", snapshot.total_mib),
             ("2 GiB", 2048), ("8 GiB", 8192)],
            lambda v: (self.row_size.set_value(v), self.window.touch())))
        self.pending = Gtk.Label(xalign=0.5, wrap=True, margin_top=10)
        self.pending.add_css_class("hint")
        wrapper.add(self.pending)
        bar, (self.button_remove, _) = action_bar([
            ("Remove zram", "destructive-action", self.remove),
            ("Apply", "suggested-action", self.apply),
        ])
        wrapper.add(bar)

        self.add(status)
        self.add(settings)
        self.add(wrapper)

    @property
    def algorithm(self) -> str:
        index = self.row_algo.get_selected()
        return self.algos[index] if 0 <= index < len(self.algos) else "zstd"

    def refresh(self, dirty: bool) -> None:
        state = core.zram_state()
        device = state.primary
        if device and device.active_swap:
            self.row_status.set_subtitle(
                f"{device.path} · {core.fmt_mib(device.disksize_mib)} · "
                f"{device.algorithm} · priority {device.priority}")
        elif device:
            self.row_status.set_subtitle(f"{device.path} exists but is not used as swap")
        else:
            self.row_status.set_subtitle("No zram device configured")

        if device and device.data_mib > 0:
            saved = max(0.0, device.data_mib - device.total_mib)
            self.row_ratio.set_subtitle(
                f"{core.fmt_mib(device.data_mib)} stored in "
                f"{core.fmt_mib(device.total_mib)} of RAM · {device.ratio:.1f}× · "
                f"{core.fmt_mib(saved)} saved")
        else:
            self.row_ratio.set_subtitle("Nothing swapped out yet")

        self.row_backend.set_subtitle(
            "systemd zram-generator" if state.generator_available
            else "dynswap systemd unit" if state.unit_installed
            else "dynswap systemd unit (created on apply)")
        self.button_remove.set_sensitive(
            bool(device) or state.unit_installed or state.generator_configured)

        if not dirty and device and device.disksize_mib:
            self.row_size.set_value(device.disksize_mib, silent=True)

        current = device.disksize_mib if device else 0
        if self.row_size.value != current or (device and device.algorithm != self.algorithm):
            self.pending.set_label(
                f"Will configure {core.fmt_mib(self.row_size.value)} of "
                f"{self.algorithm} zram")
        else:
            self.pending.set_label("Matches the running device")

    def apply(self) -> None:
        if self.row_size.value < 8:
            self.window.toast("Size is zero — use Remove zram instead")
            return
        self.window.confirm(
            "Apply zram change?",
            f"zram will be configured at {core.fmt_mib(self.row_size.value)} using "
            f"{self.algorithm}. Any running zram swap is reset first.",
            "Apply",
            actions.zram_apply(self.row_size.value, self.algorithm,
                               int(self.row_priority.get_value())))

    def remove(self) -> None:
        self.window.confirm(
            "Remove zram swap?",
            "The zram device will be switched off and its configuration deleted.",
            "Remove", actions.zram_remove(), destructive=True)


class TuningGroup(Adw.PreferencesPage):
    title = "Tuning"
    icon = "preferences-other-symbolic"

    def __init__(self, window: "Window"):
        super().__init__()
        self.window = window
        values = core.tuning_state()

        group = Adw.PreferencesGroup(
            title="Kernel tunables",
            description="These change how willingly the kernel moves pages to swap.")
        self.row_swappiness = ScaleRow(
            "Swappiness", "0 avoids swap until memory runs out; 100+ suits zram",
            0, 200, 5, values.get("vm.swappiness", 60), str,
            on_change=lambda _v: (self.window.touch(), self._update_hint()))
        self.row_swappiness.add_marks([0, 60, 100, 150, 200])
        self.row_cache = ScaleRow(
            "Cache pressure", "Lower keeps directory and inode caches around longer",
            0, 1000, 10, values.get("vm.vfs_cache_pressure", 100), str,
            on_change=lambda _v: self.window.touch())
        self.row_cache.add_marks([50, 100, 500])
        self.row_cluster = ScaleRow(
            "Page cluster", "Pages read per swap-in; 0 is best for zram",
            0, 8, 1, values.get("vm.page-cluster", 3),
            lambda v: f"{v} ({1 << v})",
            on_change=lambda _v: self.window.touch())
        self.row_persist = Adw.SwitchRow(
            title="Keep after reboot",
            subtitle=f"Writes {core.SYSCTL_FILE}",
            active=core.tuning_persisted())
        group.add(self.row_swappiness)
        group.add(self.row_cache)
        group.add(self.row_cluster)
        group.add(self.row_persist)

        wrapper = Adw.PreferencesGroup()
        self.hint = Gtk.Label(xalign=0.5, wrap=True, margin_top=4)
        self.hint.add_css_class("hint")
        wrapper.add(self.hint)
        wrapper.add(action_bar([
            ("Reload from kernel", "", self.reload),
            ("Apply", "suggested-action", self.apply),
        ])[0])

        self.add(group)
        self.add(wrapper)
        self._update_hint()

    def _update_hint(self) -> None:
        value = self.row_swappiness.value
        if value <= 10:
            text = "Very low swappiness delays swapping until memory is nearly gone."
        elif value >= 150:
            text = "High swappiness suits zram, where swapping is cheap."
        else:
            text = "60 is the kernel default; 100–180 pairs well with zram."
        self.hint.set_label(text)

    def refresh(self, dirty: bool) -> None:
        if dirty:
            return
        values = core.tuning_state()
        self.row_swappiness.set_value(values.get("vm.swappiness", 60), silent=True)
        self.row_cache.set_value(values.get("vm.vfs_cache_pressure", 100), silent=True)
        self.row_cluster.set_value(values.get("vm.page-cluster", 3), silent=True)
        self.row_persist.set_active(core.tuning_persisted())
        self._update_hint()

    def reload(self) -> None:
        self.window.dirty = False
        self.refresh(False)
        self.window.toast("Reloaded the running kernel values")

    def apply(self) -> None:
        values = {
            "vm.swappiness": self.row_swappiness.value,
            "vm.vfs_cache_pressure": self.row_cache.value,
            "vm.page-cluster": self.row_cluster.value,
        }
        summary = ", ".join(f"{k.split('.')[-1]} = {v}" for k, v in values.items())
        self.window.confirm(
            "Apply kernel tunables?",
            summary + ("\nThey will also be written to " + core.SYSCTL_FILE
                       if self.row_persist.get_active() else "\nThis boot only."),
            "Apply", actions.tuning_apply(values, self.row_persist.get_active()))


# --------------------------------------------------------------------------- #
# window
# --------------------------------------------------------------------------- #

class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="dynSwap")
        self.set_default_size(760, 800)
        self.set_size_request(360, 480)
        self.dirty = False
        self.busy = False

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()

        header = Adw.HeaderBar()
        self.switcher = Adw.ViewSwitcher(policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(self.switcher)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic",
                             tooltip_text="Reload system state")
        refresh.connect("clicked", lambda _b: (self.reload(force=True),
                                               self.toast("Reloaded")))
        header.pack_start(refresh)

        menu = Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text="Menu")
        popover = Gtk.Popover()
        menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                           margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        about = Gtk.Button(label="About dynSwap")
        about.add_css_class("flat")
        about.connect("clicked", lambda _b: (popover.popdown(), self.show_about()))
        menu_box.append(about)
        popover.set_child(menu_box)
        menu.set_popover(popover)
        header.pack_end(menu)
        view.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.append(self._build_overview())

        self.stack = Adw.ViewStack(vexpand=True)
        self.pages = [SwapfileGroup(self), ZramGroup(self), TuningGroup(self)]
        for page in self.pages:
            self.stack.add_titled_with_icon(page, page.title.lower(),
                                            page.title, page.icon)
        self.stack.connect("notify::visible-child", lambda *_: self.reload(force=True))
        self.switcher.set_stack(self.stack)
        content.append(self.stack)

        view.set_content(content)
        switcher_bar = Adw.ViewSwitcherBar(stack=self.stack, reveal=False)
        view.add_bottom_bar(switcher_bar)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        # Narrow window: drop the header switcher, reveal the one at the bottom.
        narrow = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 560sp"))
        narrow.add_setter(switcher_bar, "reveal", True)
        # A bare None will not marshal into the GValue this expects; an unset
        # Value of the right type is how you say "no widget".
        empty = GObject.Value(Gtk.Widget)
        narrow.add_setter(header, "title-widget", empty)
        self.add_breakpoint(narrow)

        self.reload(force=True)
        GLib.timeout_add_seconds(2, self._tick)

    def _build_overview(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        card.add_css_class("overview-card")
        self.meter_ram = MeterRow("MEMORY")
        self.meter_swap = MeterRow("SWAP")
        card.append(self.meter_ram)
        card.append(self.meter_swap)
        self.devices = Gtk.Label(xalign=0, wrap=True)
        self.devices.add_css_class("hint")
        card.append(self.devices)

        # Adw.PreferencesPage clamps its rows; match that width so the card and
        # the settings beneath it share one column.
        clamp = Adw.Clamp(maximum_size=600, tightening_threshold=400, child=card,
                          margin_top=18, margin_bottom=6,
                          margin_start=12, margin_end=12)
        return clamp

    # -- state ------------------------------------------------------------- #

    def touch(self) -> None:
        self.dirty = True

    def reload(self, force: bool = False) -> None:
        if force:
            self.dirty = False
        snapshot = core.memory_snapshot()
        swaps = core.read_swaps()

        self.meter_ram.value.set_label(
            f"{core.fmt_mib(snapshot.used_mib)} of {core.fmt_mib(snapshot.total_mib)} "
            f"· {snapshot.used_fraction * 100:.0f}%")
        self.meter_ram.meter.update(
            snapshot.used_fraction,
            snapshot.cached_mib / snapshot.total_mib if snapshot.total_mib else 0.0)

        if snapshot.swap_total_mib:
            self.meter_swap.value.set_label(
                f"{core.fmt_mib(snapshot.swap_used_mib)} of "
                f"{core.fmt_mib(snapshot.swap_total_mib)} "
                f"· {snapshot.swap_fraction * 100:.0f}%")
            self.meter_swap.meter.update(snapshot.swap_fraction)
        else:
            self.meter_swap.value.set_label("None configured")
            self.meter_swap.meter.update(0.0)

        if swaps:
            self.devices.set_label("  ·  ".join(
                f"{s.name} {core.fmt_mib(s.size_mib)} "
                f"({'zram' if s.is_zram else s.kind}, pri {s.priority})" for s in swaps))
        else:
            self.devices.set_label(
                "This system has no swap. Pick a size below and press Apply.")

        for page in self.pages:
            page.refresh(self.dirty)

    def _tick(self) -> bool:
        if not self.busy:
            self.reload()
        return GLib.SOURCE_CONTINUE

    # -- feedback ---------------------------------------------------------- #

    def toast(self, message: str, timeout: int = 4) -> None:
        toast = Adw.Toast(title=message, timeout=timeout)
        self.toasts.add_toast(toast)

    def confirm(self, heading: str, body: str, verb: str,
                request: list[str], destructive: bool = False) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("go", verb)
        dialog.set_response_appearance(
            "go", Adw.ResponseAppearance.DESTRUCTIVE if destructive
            else Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("go")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, response: (
            self.run_request(request, heading.rstrip("?")) if response == "go" else None))
        dialog.present(self)

    # -- privileged work --------------------------------------------------- #

    def run_request(self, request: list[str], title: str) -> None:
        if self.busy:
            return
        self.busy = True
        progress = ProgressDialog(title)
        progress.present(self)

        def on_event(event: dict) -> None:
            GLib.idle_add(handle, event)

        def handle(event: dict) -> bool:
            kind = event.get("t")
            if kind == "step":
                progress.step(str(event.get("msg", "")), int(event.get("pct", 0)))
            elif kind == "log":
                progress.log(str(event.get("msg", "")))
            return GLib.SOURCE_REMOVE

        def finish(result: actions.Result) -> bool:
            self.busy = False
            self.dirty = False
            progress.set_can_close(True)
            progress.force_close()
            self.reload(force=True)
            toast = Adw.Toast(title=result.message,
                              timeout=5 if result.ok else 10)
            self.toasts.add_toast(toast)
            return GLib.SOURCE_REMOVE

        def worker() -> None:
            result = actions.run_helper(request, on_event, prefer="pkexec")
            GLib.idle_add(finish, result)

        threading.Thread(target=worker, daemon=True).start()

    def show_about(self) -> None:
        about = Adw.AboutDialog(
            application_name="dynSwap",
            application_icon=APP_ID,
            version=__version__,
            developer_name="dynSwap",
            comments="Set the amount of swap space on this system — swapfile, "
                     "zram and the kernel tunables that govern them.",
            license_type=Gtk.License.MIT_X11,
        )
        about.present(self)


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.window: Window | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self) -> None:
        if self.window is None:
            self.window = Window(self)
        self.window.present()


def run() -> int:
    return Application().run([])
