"""dynswap.tui — a curses interface for reading and setting swap.

Stdlib only: no Textual, no Rich, nothing to install.  The drawing primitives
live in :class:`Screen`, the interactive widgets in the ``Control`` hierarchy,
and each tab is a :class:`Page` that turns its controls into a helper request.
"""
from __future__ import annotations

import curses
import getpass
import locale
import queue
import threading
import time
from dataclasses import dataclass, field

from . import actions, core

# --------------------------------------------------------------------------- #
# theme
# --------------------------------------------------------------------------- #

TITLE = "dynSwap"

# semantic name -> (256-colour index, 8-colour fallback)
PALETTE = {
    "text":    (252, curses.COLOR_WHITE),
    "bright":  (255, curses.COLOR_WHITE),
    "muted":   (245, curses.COLOR_WHITE),
    "dim":     (239, curses.COLOR_BLACK),
    "accent":  (141, curses.COLOR_MAGENTA),
    "accent2": (110, curses.COLOR_CYAN),
    "ok":      (114, curses.COLOR_GREEN),
    "warn":    (221, curses.COLOR_YELLOW),
    "danger":  (203, curses.COLOR_RED),
    "cache":   (66,  curses.COLOR_BLUE),
}

BOX = {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│"}
FILL, TRACK, KNOB = "█", "░", "●"

MARK_W = 3        # focus arrow gutter
LABEL_W = 14      # label column
READOUT_W = 15    # right-hand value column


def row_layout(x: int, width: int) -> tuple[int, int, int, int]:
    """(label_x, field_x, field_w, readout_x) for one settings row."""
    label_x = x + MARK_W
    field_x = label_x + LABEL_W
    readout_x = x + width - READOUT_W
    field_w = max(6, readout_x - field_x - 2)
    return label_x, field_x, field_w, readout_x


class Screen:
    """Thin drawing layer over a curses window: colours, boxes, bars, clipping."""

    def __init__(self, stdscr):
        self.win = stdscr
        self.pairs: dict[str, int] = {}
        self.rich = False
        self._init_colors()

    def _init_colors(self) -> None:
        try:
            curses.start_color()
            curses.use_default_colors()
        except curses.error:
            return
        self.rich = curses.COLORS >= 256
        for index, (name, (rich, basic)) in enumerate(PALETTE.items(), start=1):
            try:
                curses.init_pair(index, rich if self.rich else basic, -1)
                self.pairs[name] = index
            except curses.error:
                self.pairs[name] = 0

    def attr(self, name: str, *, bold: bool = False, reverse: bool = False) -> int:
        value = curses.color_pair(self.pairs.get(name, 0))
        if bold:
            value |= curses.A_BOLD
        if reverse:
            value |= curses.A_REVERSE
        return value

    @property
    def size(self) -> tuple[int, int]:
        height, width = self.win.getmaxyx()
        return height, width

    def write(self, y: int, x: int, text: str, color: str = "text", **kw) -> int:
        """Draw clipped text, returning the x just past what was written."""
        height, width = self.size
        if y < 0 or y >= height or x >= width:
            return x
        if x < 0:
            text, x = text[-x:], 0
        room = width - x
        if room <= 0:
            return x
        text = text[:room]
        try:
            self.win.addstr(y, x, text, self.attr(color, **kw))
        except curses.error:
            pass  # bottom-right cell always raises
        return x + len(text)

    def hline(self, y: int, x: int, width: int, color: str = "dim") -> None:
        self.write(y, x, BOX["h"] * max(0, width), color)

    def box(self, y: int, x: int, height: int, width: int,
            color: str = "dim", title: str = "", title_color: str = "accent") -> None:
        if height < 2 or width < 2:
            return
        self.write(y, x, BOX["tl"] + BOX["h"] * (width - 2) + BOX["tr"], color)
        for row in range(1, height - 1):
            self.write(y + row, x, BOX["v"], color)
            self.write(y + row, x + width - 1, BOX["v"], color)
        self.write(y + height - 1, x, BOX["bl"] + BOX["h"] * (width - 2) + BOX["br"], color)
        if title:
            self.write(y, x + 2, f" {title} ", title_color, bold=True)

    def bar(self, y: int, x: int, width: int, fraction: float,
            color: str, track: str = "dim", second: float = 0.0,
            second_color: str = "cache") -> None:
        """A horizontal gauge; ``second`` paints a dimmer band after the first."""
        width = max(0, width)
        fraction = min(max(fraction, 0.0), 1.0)
        second = min(max(second, 0.0), 1.0 - fraction)
        primary = int(round(fraction * width))
        secondary = int(round(second * width))
        rest = max(0, width - primary - secondary)
        cursor = self.write(y, x, FILL * primary, color)
        cursor = self.write(y, cursor, FILL * secondary, second_color)
        self.write(y, cursor, TRACK * rest, track)

    @staticmethod
    def load_color(fraction: float) -> str:
        if fraction < 0.60:
            return "ok"
        if fraction < 0.85:
            return "warn"
        return "danger"


# --------------------------------------------------------------------------- #
# controls
# --------------------------------------------------------------------------- #

class Control:
    focusable = True
    height = 1

    def render(self, ui: Screen, y: int, x: int, width: int, focused: bool) -> None:
        raise NotImplementedError

    def handle(self, key: int) -> bool:
        return False


class Static(Control):
    """A non-interactive line of key/value information."""

    focusable = False

    def __init__(self, label: str, value_fn, color: str = "muted"):
        self.label = label
        self.value_fn = value_fn
        self.color = color

    def render(self, ui: Screen, y: int, x: int, width: int, focused: bool) -> None:
        label_x, field_x, _, _ = row_layout(x, width)
        ui.write(y, label_x, self.label[:LABEL_W].ljust(LABEL_W), "dim")
        value = self.value_fn() if callable(self.value_fn) else str(self.value_fn)
        ui.write(y, field_x, str(value)[: max(0, x + width - field_x)], self.color)


class Spacer(Control):
    focusable = False

    def __init__(self, height: int = 1):
        self.height = height

    def render(self, ui: Screen, y: int, x: int, width: int, focused: bool) -> None:
        return


class Slider(Control):
    """A value on a track, adjusted with the arrow keys."""

    def __init__(self, label: str, value: int, minimum: int, maximum_fn,
                 step: int, big_step: int, fmt=str, presets=None, hint: str = ""):
        self.label = label
        self.value = value
        self.minimum = minimum
        self.maximum_fn = maximum_fn
        self.step = step
        self.big_step = big_step
        self.fmt = fmt
        self.presets = presets or []
        self.hint = hint

    @property
    def maximum(self) -> int:
        return max(self.minimum, int(self.maximum_fn() if callable(self.maximum_fn)
                                     else self.maximum_fn))

    def clamp(self) -> None:
        self.value = min(max(int(self.value), self.minimum), self.maximum)

    def set(self, value: int) -> None:
        self.value = value
        self.clamp()

    def handle(self, key: int) -> bool:
        self.clamp()
        if key in (curses.KEY_LEFT, ord("h")):
            self.set(self.value - self.step)
        elif key in (curses.KEY_RIGHT, ord("l")):
            self.set(self.value + self.step)
        elif key in (curses.KEY_SLEFT, ord("H")):
            self.set(self.value - self.big_step)
        elif key in (curses.KEY_SRIGHT, ord("L")):
            self.set(self.value + self.big_step)
        elif key == curses.KEY_HOME:
            self.set(self.minimum)
        elif key == curses.KEY_END:
            self.set(self.maximum)
        elif ord("1") <= key <= ord("9") and self.presets:
            index = key - ord("1")
            if index < len(self.presets):
                self.set(self.presets[index])
        else:
            return False
        return True

    def render(self, ui: Screen, y: int, x: int, width: int, focused: bool) -> None:
        self.clamp()
        label_x, track_x, track_w, readout_x = row_layout(x, width)
        ui.write(y, x, f" {'▸' if focused else ' '} ",
                 "accent" if focused else "dim", bold=focused)
        ui.write(y, label_x, self.label[:LABEL_W].ljust(LABEL_W),
                 "bright" if focused else "muted", bold=focused)

        span = max(1, self.maximum - self.minimum)
        fraction = (self.value - self.minimum) / span
        filled = int(round(fraction * track_w))
        ui.write(y, track_x, FILL * filled, "accent" if focused else "accent2")
        ui.write(y, track_x + filled, TRACK * (track_w - filled), "dim")
        if focused and track_w:
            knob = track_x + min(track_w - 1, max(0, filled - (1 if filled else 0)))
            ui.write(y, knob, KNOB, "bright", bold=True)

        ui.write(y, readout_x, self.fmt(self.value)[:READOUT_W],
                 "bright" if focused else "muted", bold=focused)


class Choice(Control):
    """Cycle through a list of options with the arrow keys."""

    def __init__(self, label: str, options_fn, index: int = 0, hint: str = ""):
        self.label = label
        self.options_fn = options_fn
        self.index = index
        self.hint = hint

    @property
    def options(self) -> list[str]:
        result = self.options_fn() if callable(self.options_fn) else self.options_fn
        return list(result) or ["—"]

    @property
    def value(self) -> str:
        options = self.options
        return options[min(self.index, len(options) - 1)]

    def select(self, name: str) -> None:
        options = self.options
        if name in options:
            self.index = options.index(name)

    def handle(self, key: int) -> bool:
        options = self.options
        if key in (curses.KEY_LEFT, ord("h")):
            self.index = (self.index - 1) % len(options)
        elif key in (curses.KEY_RIGHT, ord("l"), ord(" ")):
            self.index = (self.index + 1) % len(options)
        else:
            return False
        return True

    def render(self, ui: Screen, y: int, x: int, width: int, focused: bool) -> None:
        label_x, field_x, field_w, _ = row_layout(x, width)
        ui.write(y, x, f" {'▸' if focused else ' '} ",
                 "accent" if focused else "dim", bold=focused)
        ui.write(y, label_x, self.label[:LABEL_W].ljust(LABEL_W),
                 "bright" if focused else "muted", bold=focused)
        cursor, limit = field_x, field_x + field_w + READOUT_W - 2
        for name in self.options:
            chunk = f"[{name}]" if name == self.value else f" {name} "
            if cursor + len(chunk) > limit:
                ui.write(y, cursor, "›", "dim")
                break
            cursor = ui.write(y, cursor, chunk,
                              ("accent" if focused else "accent2")
                              if name == self.value else "dim",
                              bold=name == self.value)
            cursor += 1


class Toggle(Control):
    def __init__(self, label: str, value: bool, hint: str = ""):
        self.label = label
        self.value = value
        self.hint = hint

    def handle(self, key: int) -> bool:
        if key in (ord(" "), curses.KEY_LEFT, curses.KEY_RIGHT, ord("h"), ord("l")):
            self.value = not self.value
            return True
        return False

    def render(self, ui: Screen, y: int, x: int, width: int, focused: bool) -> None:
        label_x, field_x, _, _ = row_layout(x, width)
        ui.write(y, x, f" {'▸' if focused else ' '} ",
                 "accent" if focused else "dim", bold=focused)
        ui.write(y, label_x, self.label[:LABEL_W].ljust(LABEL_W),
                 "bright" if focused else "muted", bold=focused)
        color = "accent" if focused else ("ok" if self.value else "dim")
        cursor = ui.write(y, field_x, "◉ " if self.value else "○ ", color, bold=True)
        ui.write(y, cursor, "yes" if self.value else "no",
                 "bright" if focused else "muted")


class Text(Control):
    """A single-line editable field, edited in place."""

    def __init__(self, label: str, value: str, hint: str = ""):
        self.label = label
        self.value = value
        self.hint = hint
        self.editing = False
        self.buffer = ""

    def handle(self, key: int) -> bool:
        if not self.editing:
            if key in (ord("\n"), curses.KEY_ENTER, ord("e")):
                self.editing = True
                self.buffer = self.value
                return True
            return False
        if key in (27,):                       # Esc
            self.editing = False
        elif key in (ord("\n"), curses.KEY_ENTER):
            self.value = self.buffer.strip() or self.value
            self.editing = False
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            self.buffer = self.buffer[:-1]
        elif 32 <= key < 127:
            self.buffer += chr(key)
        return True

    def render(self, ui: Screen, y: int, x: int, width: int, focused: bool) -> None:
        label_x, field_x, field_w, _ = row_layout(x, width)
        ui.write(y, x, f" {'▸' if focused else ' '} ",
                 "accent" if focused else "dim", bold=focused)
        ui.write(y, label_x, self.label[:LABEL_W].ljust(LABEL_W),
                 "bright" if focused else "muted", bold=focused)
        if self.editing:
            shown = (self.buffer + "▏")[-field_w:]
            ui.write(y, field_x, shown, "bright", bold=True)
            ui.write(y, field_x + field_w + 2, "⏎ ok  esc cancel", "dim")
        else:
            ui.write(y, field_x, self.value[: field_w + READOUT_W],
                     "bright" if focused else "muted")


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #

@dataclass
class Action:
    key: str
    label: str
    build: object            # callable -> (request, confirm_text) or None
    danger: bool = False


class Page:
    name = "page"
    controls: list[Control] = []
    actions: list[Action] = []

    def __init__(self, app: "App"):
        self.app = app
        self.focus = 0
        self.build()

    def build(self) -> None:
        self.controls = []
        self.actions = []

    def refresh(self) -> None:
        """Re-read system state into the controls (only when not being edited)."""

    def focusables(self) -> list[int]:
        return [i for i, c in enumerate(self.controls) if c.focusable]

    def move_focus(self, delta: int) -> None:
        order = self.focusables()
        if not order:
            return
        current = self.focus if self.focus in order else order[0]
        self.focus = order[(order.index(current) + delta) % len(order)]

    @property
    def focused(self) -> Control | None:
        if 0 <= self.focus < len(self.controls) and self.controls[self.focus].focusable:
            return self.controls[self.focus]
        order = self.focusables()
        return self.controls[order[0]] if order else None

    def details(self) -> list[tuple[str, str]]:
        """Extra (colour, text) lines shown below the controls."""
        return []

    def render(self, ui: Screen, y: int, x: int, width: int, height: int) -> None:
        row = y
        for index, control in enumerate(self.controls):
            if row >= y + height - 2:
                break
            control.render(ui, row, x, width, index == self.focus)
            row += control.height

        lines = self.details()
        if lines:
            top = y + height - 1 - len(lines)
            if top > row:
                ui.hline(top - 1, x + MARK_W, width - MARK_W * 2, "dim")
                for offset, (color, text) in enumerate(lines):
                    ui.write(top + offset, x + MARK_W,
                             text[: width - MARK_W * 2], color)

        hint = getattr(self.focused, "hint", "")
        if hint:
            ui.write(y + height - 1, x + MARK_W, f"› {hint}", "dim")

    def handle(self, key: int) -> bool:
        control = self.focused
        return bool(control and control.handle(key))


class SwapfilePage(Page):
    name = "Swapfile"

    def build(self) -> None:
        state = core.swapfile_state()
        snap = core.memory_snapshot()
        default = state.size_mib or core.recommended_swapfile_mib(snap.total_mib)

        self.path = Text("Location", state.path, "⏎ edit")
        self.size = Slider(
            "Size", default, 0, lambda: max(1024, self.state.max_settable_mib),
            step=256, big_step=1024, fmt=lambda v: core.fmt_mib(v) if v else "off",
            presets=[1024, 2048, 4096, 8192, 16384],
            hint="1-5 presets · shift ±1 GiB",
        )
        self.priority = Slider(
            "Priority", state.priority if state.priority >= 0 else -1, -1, 100,
            step=1, big_step=10, fmt=lambda v: "auto" if v < 0 else str(v),
            hint="disk swap wants a low priority",
        )
        self.persist = Toggle("Persist", state.in_fstab or not state.exists,
                              "add an /etc/fstab entry")

        self.controls = [
            Static("Filesystem", lambda: f"{self.state.fs_type}  ·  "
                                         f"{core.fmt_mib(self.state.free_mib)} free"),
            Static("Status", self._status_text, "text"),
            Spacer(),
            self.path,
            self.size,
            self.priority,
            self.persist,
        ]
        self.focus = 4
        self.actions = [
            Action("⏎", "apply", self._apply),
            Action("t", "on/off", self._toggle),
            Action("d", "remove", self._remove, danger=True),
        ]

    @property
    def state(self) -> core.SwapfileState:
        return self.app.swapfile

    def _status_text(self) -> str:
        state = self.state
        if state.active:
            used = core.fmt_mib(state.used_mib)
            return f"active · {core.fmt_mib(state.active_size_mib)} · {used} in use"
        if state.exists:
            return f"present but inactive · {core.fmt_mib(state.size_mib)}"
        return "no swapfile at this location"

    def refresh(self) -> None:
        if not self.path.editing:
            state = self.state
            if state.path != self.path.value:
                return
            if not self.app.dirty:
                self.persist.value = state.in_fstab or not state.exists
                if state.exists:
                    self.size.set(state.size_mib)

    def details(self) -> list[tuple[str, str]]:
        state, target = self.state, self.size.value
        lines: list[tuple[str, str]] = []
        if not state.exists and target:
            lines.append(("accent2", f"Pending  create {state.path} at "
                                     f"{core.fmt_mib(target)}"))
        elif state.exists and target != state.size_mib:
            delta = target - state.size_mib
            verb = "grow" if delta > 0 else "shrink"
            lines.append(("accent2", f"Pending  {verb} by {core.fmt_mib(abs(delta))} "
                                     f"→ {core.fmt_mib(target)}"))
        else:
            lines.append(("dim", "Pending  nothing — the slider matches the disk"))
        after = state.free_mib - (target - (state.size_mib if state.exists else 0))
        color = "danger" if after < 0 else "dim"
        lines.append((color, f"Disk     {core.fmt_mib(max(after, 0))} would be left free"
                             f" on {state.fs_type}"
                             + ("  (not enough space)" if after < 0 else "")))
        return lines

    def _apply(self):
        path = self.path.value
        if self.size.value < 8:
            return None, "Size is 0 — use 'd' to remove the swapfile instead."
        confirm = (
            f"Set {path} to {core.fmt_mib(self.size.value)}"
            f"{' and add it to /etc/fstab' if self.persist.value else ''}?\n"
            "Existing swap at this path is deactivated and rebuilt."
        )
        return actions.swapfile_apply(path, self.size.value, self.priority.value,
                                      self.persist.value), confirm

    def _toggle(self):
        path = self.path.value
        verb = "Deactivate" if self.state.active else "Activate"
        return actions.swapfile_toggle(path, self.priority.value), f"{verb} {path}?"

    def _remove(self):
        path = self.path.value
        return (actions.swapfile_remove(path, purge=True),
                f"Deactivate {path}, delete the file and drop its /etc/fstab entry?")


class ZramPage(Page):
    name = "zram"

    def build(self) -> None:
        state = self.app.zram
        snap = core.memory_snapshot()
        current = (state.primary.disksize_mib if state.primary else 0)
        default = current or state.configured_size_mib or core.recommended_zram_mib(snap.total_mib)

        self.size = Slider(
            "Size", default, 0, lambda: max(1024, core.memory_snapshot().total_mib * 2),
            step=256, big_step=1024, fmt=lambda v: core.fmt_mib(v) if v else "off",
            presets=[1024, 2048, 4096, 8192, 16384],
            hint="half of RAM is a good start",
        )
        self.algo = Choice("Algorithm", lambda: self.app.zram.available_algos,
                           hint="zstd compresses best")
        self.algo.select(
            (state.primary.algorithm if state.primary else "") or state.configured_algo or "zstd")
        self.priority = Slider(
            "Priority", (state.primary.priority if state.primary else 0) or 100, 0, 200,
            step=5, big_step=25, fmt=str, hint="higher than disk swap",
        )

        self.controls = [
            Static("Backend", self._backend_text),
            Static("Status", self._status_text, "text"),
            Static("Compression", self._ratio_text),
            Spacer(),
            self.size,
            self.algo,
            self.priority,
        ]
        self.focus = 4
        self.actions = [
            Action("⏎", "apply", self._apply),
            Action("d", "remove", self._remove, danger=True),
        ]

    def _backend_text(self) -> str:
        state = self.app.zram
        if state.generator_available:
            return "systemd zram-generator"
        if state.unit_installed:
            return "dynswap systemd unit"
        return "dynswap systemd unit (will be created)"

    def _status_text(self) -> str:
        device = self.app.zram.primary
        if device and device.active_swap:
            return (f"{device.path} active · {core.fmt_mib(device.disksize_mib)} · "
                    f"{device.algorithm} · priority {device.priority}")
        if device:
            return f"{device.path} exists but is not swap"
        return "no zram device"

    def _ratio_text(self) -> str:
        device = self.app.zram.primary
        if not device or device.data_mib <= 0:
            return "—"
        return (f"{core.fmt_mib(device.data_mib)} stored in "
                f"{core.fmt_mib(device.total_mib)} of RAM  ·  {device.ratio:.1f}× ratio")

    def refresh(self) -> None:
        if self.app.dirty:
            return
        device = self.app.zram.primary
        if device and device.disksize_mib:
            self.size.set(device.disksize_mib)
            self.algo.select(device.algorithm)

    def details(self) -> list[tuple[str, str]]:
        state = self.app.zram
        device = state.primary
        lines: list[tuple[str, str]] = []
        target = self.size.value
        current = device.disksize_mib if device else 0
        if target != current or (device and device.algorithm != self.algo.value):
            lines.append(("accent2", f"Pending  {core.fmt_mib(target)} of {self.algo.value} "
                                     f"zram (currently {core.fmt_mib(current) if current else 'none'})"))
        else:
            lines.append(("dim", "Pending  nothing — matches the running device"))
        if device and device.data_mib > 0:
            saved = max(0.0, device.data_mib - device.total_mib)
            lines.append(("ok", f"Saving   {core.fmt_mib(saved)} of RAM through compression"))
        else:
            lines.append(("dim", "Saving   zram costs RAM only for what it actually holds"))
        return lines

    def _apply(self):
        if self.size.value < 8:
            return None, "Size is 0 — use 'd' to remove zram instead."
        return (actions.zram_apply(self.size.value, self.algo.value, self.priority.value),
                f"Configure zram at {core.fmt_mib(self.size.value)} using "
                f"{self.algo.value}?\nAny running zram swap is reset first.")

    def _remove(self):
        return actions.zram_remove(), "Stop zram swap and remove its configuration?"


class TuningPage(Page):
    name = "Tuning"

    def build(self) -> None:
        values = core.tuning_state()
        self.swappiness = Slider(
            "Swappiness", values.get("vm.swappiness", 60), 0, 200, 5, 25, str,
            presets=[0, 10, 60, 100, 180],
            hint="how eagerly the kernel swaps",
        )
        self.cache_pressure = Slider(
            "Cache press.", values.get("vm.vfs_cache_pressure", 100), 0, 1000, 10, 50, str,
            hint="lower keeps inode/dentry cache longer",
        )
        self.page_cluster = Slider(
            "Page cluster", values.get("vm.page-cluster", 3), 0, 8, 1, 1,
            lambda v: f"{v}  ({1 << v} pages)",
            hint="0 is best for zram",
        )
        self.persist = Toggle("Persist", core.tuning_persisted(),
                              "write /etc/sysctl.d/99-dynswap.conf")

        self.controls = [
            Static("Live values", self._live_text),
            Spacer(),
            self.swappiness,
            self.cache_pressure,
            self.page_cluster,
            self.persist,
            Spacer(),
            Static("Tip", self._tip_text, "accent2"),
        ]
        self.focus = 2
        self.actions = [
            Action("⏎", "apply", self._apply),
            Action("r", "reload", self._reload),
        ]

    def _live_text(self) -> str:
        values = core.tuning_state()
        return "  ".join(f"{k.split('.')[-1]}={v}" for k, v in sorted(values.items())
                         if k != "vm.watermark_boost_factor")

    def _tip_text(self) -> str:
        if self.swappiness.value <= 10:
            return "Very low swappiness delays swapping until memory is nearly gone."
        if self.swappiness.value >= 150:
            return "High swappiness suits zram, where swapping is cheap."
        return "60 is the kernel default; 100–180 pairs well with zram."

    def refresh(self) -> None:
        return

    def details(self) -> list[tuple[str, str]]:
        live = core.tuning_state()
        pending = {
            "vm.swappiness": self.swappiness.value,
            "vm.vfs_cache_pressure": self.cache_pressure.value,
            "vm.page-cluster": self.page_cluster.value,
        }
        changed = [f"{k.split('.')[-1]} {live.get(k)}→{v}"
                   for k, v in pending.items() if live.get(k) != v]
        lines = [("accent2", "Pending  " + ", ".join(changed))] if changed else \
                [("dim", "Pending  nothing — values match the running kernel")]
        lines.append(("dim", "Persist  " + (f"written to {core.SYSCTL_FILE}"
                                            if self.persist.value
                                            else "this boot only")))
        return lines

    def _reload(self):
        self.build()
        self.app.flash("reloaded live sysctl values", "ok")
        return None, None

    def _apply(self):
        values = {
            "vm.swappiness": self.swappiness.value,
            "vm.vfs_cache_pressure": self.cache_pressure.value,
            "vm.page-cluster": self.page_cluster.value,
        }
        summary = ", ".join(f"{k.split('.')[-1]}={v}" for k, v in values.items())
        return (actions.tuning_apply(values, self.persist.value),
                f"Apply {summary}"
                f"{' and persist it' if self.persist.value else ''}?")


class LogPage(Page):
    name = "Log"

    def build(self) -> None:
        self.controls = []
        self.actions = [Action("c", "clear", self._clear)]
        self.offset = 0

    def _clear(self):
        self.app.log.clear()
        self.app.flash("log cleared", "muted")
        return None, None

    def handle(self, key: int) -> bool:
        if key in (curses.KEY_UP, ord("k")):
            self.offset += 1
        elif key in (curses.KEY_DOWN, ord("j")):
            self.offset = max(0, self.offset - 1)
        else:
            return False
        return True

    def render(self, ui: Screen, y: int, x: int, width: int, height: int) -> None:
        lines = self.app.log
        if not lines:
            ui.write(y + 1, x + 3, "Nothing yet — applied changes are logged here.", "dim")
            return
        self.offset = min(self.offset, max(0, len(lines) - height))
        window = lines[max(0, len(lines) - height - self.offset):][:height]
        for row, line in enumerate(window):
            color = "dim" if line.startswith("  ") or line.startswith("$") else "muted"
            if line.startswith("✔"):
                color = "ok"
            elif line.startswith("✘"):
                color = "danger"
            elif line.startswith("▸"):
                color = "accent"
            ui.write(y + row, x + 2, line[: max(0, width - 4)], color)


# --------------------------------------------------------------------------- #
# application
# --------------------------------------------------------------------------- #

@dataclass
class Job:
    request: list[str]
    events: "queue.Queue[dict]" = field(default_factory=queue.Queue)
    thread: threading.Thread | None = None
    pct: int = 0
    message: str = "starting…"
    result: actions.Result | None = None
    started: float = field(default_factory=time.monotonic)


@dataclass
class PasswordPrompt:
    """A password being collected for a request that is waiting on it."""

    request: list[str]
    buffer: bytearray = field(default_factory=bytearray)
    error: str = ""
    attempts: int = 0
    checking: bool = False

    MAX_ATTEMPTS = 3

    @property
    def length(self) -> int:
        """Characters typed, not bytes — a password may be non-ASCII."""
        return len(bytes(self.buffer).decode("utf-8", "replace"))

    def take(self) -> str:
        password = bytes(self.buffer).decode("utf-8", "surrogateescape")
        self.buffer = bytearray()
        return password


class App:
    def __init__(self, screen: Screen):
        self.ui = screen
        self.pages: list[Page] = []
        self.log: list[str] = []
        self.flash_text = ""
        self.flash_color = "muted"
        self.flash_until = 0.0
        self.dirty = False
        self.job: Job | None = None
        self.auth: PasswordPrompt | None = None
        self.auth_events: "queue.Queue[bool]" = queue.Queue()
        self.username = getpass.getuser()
        self.confirm: tuple[str, list[str]] | None = None
        self.spinner = 0
        self.reload_state()
        self.pages = [SwapfilePage(self), ZramPage(self),
                      TuningPage(self), LogPage(self)]
        self.tab = 0

    # -- state ------------------------------------------------------------- #

    def reload_state(self) -> None:
        self.memory = core.memory_snapshot()
        self.swapfile = core.swapfile_state(self.swapfile_path)
        self.zram = core.zram_state()
        self.swaps = core.read_swaps()

    @property
    def swapfile_path(self) -> str:
        """The path the Swapfile page is pointed at (before it exists, ours)."""
        for page in self.pages:
            if isinstance(page, SwapfilePage):
                return page.path.value
        return core.DEFAULT_SWAPFILE

    @property
    def page(self) -> Page:
        return self.pages[self.tab]

    def flash(self, text: str, color: str = "muted", seconds: float = 4.0) -> None:
        self.flash_text = text
        self.flash_color = color
        self.flash_until = time.monotonic() + seconds

    # -- drawing ----------------------------------------------------------- #

    def draw(self) -> None:
        ui = self.ui
        ui.win.erase()
        height, width = ui.size
        if height < 20 or width < 64:
            ui.write(height // 2, max(0, (width - 34) // 2),
                     "Terminal too small — 64×20 minimum", "warn", bold=True)
            ui.win.noutrefresh()
            curses.doupdate()
            return

        inner = width - 4
        self._draw_header(2, inner)
        self._draw_gauges(5, 2, inner)
        tabs_y = 10
        self._draw_tabs(tabs_y, 2, inner)

        body_y = tabs_y + 2
        body_h = height - body_y - 3
        ui.box(body_y, 2, body_h, inner, "dim")
        self.page.render(ui, body_y + 1, 3, inner - 2, body_h - 2)

        self._draw_footer(height - 2, 2, inner)

        if self.job:
            self._draw_job()
        elif self.auth:
            self._draw_password()
        elif self.confirm:
            self._draw_confirm()

        ui.win.noutrefresh()
        curses.doupdate()

    def _draw_header(self, x: int, width: int) -> None:
        ui = self.ui
        cursor = ui.write(1, x, TITLE, "accent", bold=True)
        ui.write(1, cursor + 1, "swap control", "dim")

        total = sum(s.size_mib for s in self.swaps)
        if total:
            used = sum(s.used_mib for s in self.swaps)
            summary = f"{len(self.swaps)} device{'s' if len(self.swaps) != 1 else ''} · " \
                      f"{core.fmt_mib(total)} · {core.fmt_mib(used)} used"
            color = "ok"
        else:
            summary, color = "no swap configured", "warn"
        ui.write(1, x + width - len(summary), summary, color)
        ui.hline(2, x, width, "dim")

    def _draw_gauges(self, y: int, x: int, width: int) -> None:
        ui = self.ui
        snap = self.memory
        label_w, readout_w = 8, 24
        track_w = max(10, width - label_w - readout_w - 8)

        used_frac = snap.used_fraction
        cache_frac = (snap.cached_mib / snap.total_mib) if snap.total_mib else 0.0
        ui.write(y, x + 1, "RAM", "muted", bold=True)
        ui.write(y, x + label_w - 3, f"{used_frac * 100:3.0f}%",
                 ui.load_color(used_frac), bold=True)
        ui.bar(y, x + label_w + 2, track_w, used_frac, ui.load_color(used_frac),
               second=cache_frac)
        ui.write(y, x + label_w + track_w + 4,
                 f"{core.fmt_mib(snap.used_mib)} / {core.fmt_mib(snap.total_mib)}", "text")

        swap_frac = snap.swap_fraction
        ui.write(y + 1, x + 1, "SWAP", "muted", bold=True)
        if snap.swap_total_mib:
            ui.write(y + 1, x + label_w - 3, f"{swap_frac * 100:3.0f}%",
                     ui.load_color(swap_frac), bold=True)
            ui.bar(y + 1, x + label_w + 2, track_w, swap_frac, ui.load_color(swap_frac))
            ui.write(y + 1, x + label_w + track_w + 4,
                     f"{core.fmt_mib(snap.swap_used_mib)} / "
                     f"{core.fmt_mib(snap.swap_total_mib)}", "text")
        else:
            ui.write(y + 1, x + label_w - 3, "  —", "dim")
            ui.bar(y + 1, x + label_w + 2, track_w, 0.0, "dim")
            ui.write(y + 1, x + label_w + track_w + 4, "none", "dim")

        ui.write(y + 2, x + label_w + 2,
                 f"{FILL} used   {FILL} cache   {TRACK} free", "dim")

        row = y + 3
        for entry in self.swaps[:1]:
            kind = "zram" if entry.is_zram else entry.kind
            ui.write(row, x + label_w + 2,
                     f"{entry.name}  ({kind}, priority {entry.priority})", "dim")
        if len(self.swaps) > 1:
            ui.write(row, x + label_w + 2,
                     "  ".join(f"{s.name} {core.fmt_mib(s.size_mib)}" for s in self.swaps)
                     [: width - label_w - 4], "dim")

    def _draw_tabs(self, y: int, x: int, width: int) -> None:
        ui = self.ui
        ui.hline(y + 1, x, width, "dim")
        cursor = x + 1
        for index, page in enumerate(self.pages):
            label = f" {page.name} "
            if index == self.tab:
                ui.write(y, cursor, label, "accent", bold=True, reverse=True)
                ui.write(y + 1, cursor, "━" * len(label), "accent", bold=True)
            else:
                ui.write(y, cursor, label, "muted")
            cursor += len(label) + 1

    def _draw_footer(self, y: int, x: int, width: int) -> None:
        ui = self.ui
        ui.hline(y - 1, x, width, "dim")
        if time.monotonic() < self.flash_until and self.flash_text:
            ui.write(y, x + 1, self.flash_text[: width - 2], self.flash_color, bold=True)
            return
        hints: list[tuple[str, str, str]] = [
            ("↹", "tab", "accent2"), ("↑↓", "field", "accent2"),
            ("←→", "adjust", "accent2"),
        ]
        for action in self.page.actions:
            hints.append((action.key, action.label,
                          "danger" if action.danger else "accent2"))
        hints += [("R", "refresh", "accent2"), ("q", "quit", "accent2")]
        cursor = x + 1
        for key, label, color in hints:
            cursor = ui.write(y, cursor, key, color, bold=True)
            cursor = ui.write(y, cursor + 1, label, "dim")
            cursor += 2
        if not actions.is_root():
            note = "authenticates on apply"
            note_x = x + width - len(note)
            if note_x > cursor + 2:
                ui.write(y, note_x, note, "dim")

    def _center_box(self, height: int, width: int) -> tuple[int, int, int, int]:
        term_h, term_w = self.ui.size
        width = min(width, term_w - 4)
        height = min(height, term_h - 4)
        return (term_h - height) // 2, (term_w - width) // 2, height, width

    def _draw_confirm(self) -> None:
        assert self.confirm
        text, _ = self.confirm
        lines = [line for chunk in text.split("\n") for line in _wrap(chunk, 56)]
        y, x, height, width = self._center_box(len(lines) + 6, 62)
        ui = self.ui
        for row in range(height):
            ui.write(y + row, x, " " * width, "text", reverse=False)
        ui.box(y, x, height, width, "accent", "Confirm")
        for row, line in enumerate(lines):
            ui.write(y + 2 + row, x + 3, line, "text")
        ui.write(y + height - 2, x + 3, "⏎", "ok", bold=True)
        ui.write(y + height - 2, x + 5, "apply", "text")
        ui.write(y + height - 2, x + 13, "esc", "danger", bold=True)
        ui.write(y + height - 2, x + 17, "cancel", "text")

    def _draw_password(self) -> None:
        prompt = self.auth
        if prompt is None:
            return
        y, x, height, width = self._center_box(11, 62)
        ui = self.ui
        for row in range(height):
            ui.write(y + row, x, " " * width, "text")
        ui.box(y, x, height, width, "accent", "Authentication")

        ui.write(y + 2, x + 3, "Changing swap needs administrator rights.", "text")
        ui.write(y + 4, x + 3, f"Password for {self.username}", "muted")

        field_w = width - 6
        typed = min(prompt.length, field_w - 2)
        ui.write(y + 5, x + 4, "●" * typed, "bright", bold=True)
        if not prompt.checking:
            ui.write(y + 5, x + 4 + typed, "▏", "accent", bold=True)
        ui.hline(y + 6, x + 3, field_w, "dim")

        if prompt.checking:
            frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            ui.write(y + 8, x + 3, frames[self.spinner % len(frames)], "accent",
                     bold=True)
            ui.write(y + 8, x + 5, "Checking…", "muted")
        elif prompt.error:
            ui.write(y + 8, x + 3, prompt.error, "danger", bold=True)
        else:
            ui.write(y + 8, x + 3, "⏎", "ok", bold=True)
            ui.write(y + 8, x + 5, "authenticate", "text")
            ui.write(y + 8, x + 20, "esc", "danger", bold=True)
            ui.write(y + 8, x + 24, "cancel", "text")

    def _draw_job(self) -> None:
        assert self.job
        job = self.job
        y, x, height, width = self._center_box(9, 66)
        ui = self.ui
        for row in range(height):
            ui.write(y + row, x, " " * width, "text")
        ui.box(y, x, height, width, "accent", "Working")
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        ui.write(y + 2, x + 3, frames[self.spinner % len(frames)], "accent", bold=True)
        ui.write(y + 2, x + 5, job.message[: width - 8], "text")
        track = width - 6
        filled = int(track * min(job.pct, 100) / 100)
        ui.write(y + 4, x + 3, FILL * filled, "accent")
        ui.write(y + 4, x + 3 + filled, TRACK * (track - filled), "dim")
        ui.write(y + 5, x + 3, f"{job.pct:3d}%", "muted")
        elapsed = time.monotonic() - job.started
        ui.write(y + 5, x + width - 12, f"{elapsed:5.1f}s", "dim")
        tail = next((line for line in reversed(self.log) if line.startswith("  ")), "")
        ui.write(y + 6, x + 3, tail.strip()[: width - 6], "dim")

    # -- jobs -------------------------------------------------------------- #

    def start_job(self, request: list[str]) -> None:
        """Gain root if we need it, then run the request."""
        if actions.is_root():
            self._launch(request, "auto")
            return
        if actions.has_sudo():
            if actions.sudo_ticket_valid():
                self._launch(request, "sudo")
            else:
                self.auth = PasswordPrompt(request=request)
            return
        if actions.has_pkexec():
            # pkexec brings its own prompt. On a desktop that is a polkit
            # dialog, but on a plain console it is a text agent that would
            # draw straight over this screen — so hand the terminal back to
            # it and take the screen again afterwards.
            self._run_detached(request)
            return
        self.flash("no way to gain root privileges — install polkit or sudo",
                   "danger", 8.0)

    def _launch(self, request: list[str], prefer: str) -> None:
        job = Job(request=request)
        self.job = job
        self.log.append(f"▸ {' '.join(request)}")

        def worker() -> None:
            result = actions.run_helper(request, job.events.put, prefer=prefer)
            job.events.put({"t": "result", "result": result})

        job.thread = threading.Thread(target=worker, daemon=True)
        job.thread.start()

    def _run_detached(self, request: list[str]) -> None:
        """Leave curses so pkexec's own agent owns the terminal while it runs."""
        curses.def_prog_mode()
        curses.endwin()
        print("\n  dynSwap needs administrator rights to change swap.\n")
        self.log.append(f"▸ {' '.join(request)}")

        def echo(event: dict) -> None:
            if event.get("t") == "step":
                print(f"  {int(event.get('pct', 0)):3d}%  {event.get('msg', '')}")
            elif event.get("t") == "log":
                self.log.append("  " + str(event.get("msg", "")))

        result = actions.run_helper(request, echo, prefer="pkexec")

        curses.reset_prog_mode()
        curses.flushinp()
        self.ui.win.clearok(True)
        self.ui.win.refresh()
        self._settle(result)

    def _settle(self, result: actions.Result) -> None:
        """Record a finished request and re-read the system."""
        self.log.append(("✔ " if result.ok else "✘ ") + result.message)
        self.flash(result.message, "ok" if result.ok else "danger", 6.0)
        self.dirty = False
        self.reload_state()
        for page in self.pages:
            page.refresh()

    # -- password ---------------------------------------------------------- #

    def pump_auth(self) -> None:
        prompt = self.auth
        if prompt is None:
            return
        try:
            accepted = self.auth_events.get_nowait()
        except queue.Empty:
            return
        prompt.checking = False
        if accepted:
            request = prompt.request
            self.auth = None
            self.log.append("✔ authenticated")
            self._launch(request, "sudo")
            return
        prompt.attempts += 1
        left = PasswordPrompt.MAX_ATTEMPTS - prompt.attempts
        if left <= 0:
            self.auth = None
            self.flash("authentication failed", "danger", 6.0)
        else:
            prompt.error = (f"Wrong password — {left} "
                            f"attempt{'s' if left != 1 else ''} left.")

    def handle_password_key(self, key: int) -> bool:
        prompt = self.auth
        if prompt is None:
            return True
        if key == 27:                                    # Esc
            self.auth = None
            self.flash("authentication cancelled", "muted", 3.0)
            return True
        if prompt.checking:
            return True
        if key in (ord("\n"), curses.KEY_ENTER):
            if not prompt.buffer:
                prompt.error = "Type a password, or press esc to cancel."
                return True
            prompt.checking = True
            prompt.error = ""
            password = prompt.take()
            threading.Thread(target=self._verify, args=(password,),
                             daemon=True).start()
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            # Drop a whole character: continuation bytes are 0x80-0xBF.
            while prompt.buffer:
                if not 0x80 <= prompt.buffer.pop() < 0xC0:
                    break
        elif key == 21:                                  # Ctrl-U
            prompt.buffer = bytearray()
        elif 32 <= key <= 255 and len(prompt.buffer) < 512:
            prompt.buffer.append(key)
        return True

    def _verify(self, password: str) -> None:
        self.auth_events.put(actions.sudo_authenticate(password))

    def pump_job(self) -> None:
        job = self.job
        if not job:
            return
        while True:
            try:
                event = job.events.get_nowait()
            except queue.Empty:
                break
            kind = event.get("t")
            if kind == "step":
                job.message = str(event.get("msg", ""))
                job.pct = int(event.get("pct", job.pct))
            elif kind == "log":
                self.log.append("  " + str(event.get("msg", "")))
            elif kind == "result":
                job.result = event["result"]
        if job.result is not None and not (job.thread and job.thread.is_alive()):
            self.job = None
            self._settle(job.result)

    # -- input ------------------------------------------------------------- #

    def handle_key(self, key: int) -> bool:
        """Returns False to quit."""
        if self.job:
            return True
        if self.auth:
            return self.handle_password_key(key)
        if self.confirm:
            if key in (ord("\n"), curses.KEY_ENTER):
                request = self.confirm[1]
                self.confirm = None
                self.start_job(request)
            elif key in (27, ord("n"), ord("q")):
                self.confirm = None
                self.flash("cancelled", "muted", 2.0)
            return True

        editing = isinstance(self.page.focused, Text) and self.page.focused.editing
        if editing:
            self.page.handle(key)
            if not self.page.focused.editing:
                self.reload_state()
            return True

        if key in (ord("q"), 27):
            return False
        if key in (ord("\t"), ord("]")):
            self.tab = (self.tab + 1) % len(self.pages)
            return True
        if key in (curses.KEY_BTAB, ord("[")):
            self.tab = (self.tab - 1) % len(self.pages)
            return True
        if key in (curses.KEY_UP, ord("k")):
            self.page.move_focus(-1)
            return True
        if key in (curses.KEY_DOWN, ord("j")):
            self.page.move_focus(1)
            return True
        if key in (ord("R"),):
            self.dirty = False
            self.reload_state()
            for page in self.pages:
                page.refresh()
            self.flash("state reloaded", "ok", 2.0)
            return True

        if self.page.handle(key):
            self.dirty = True
            return True

        for action in self.page.actions:
            trigger = ord("\n") if action.key == "⏎" else ord(action.key)
            if key == trigger or (action.key == "⏎" and key == curses.KEY_ENTER):
                built = action.build()
                if not built:
                    return True
                request, confirm = built
                if request is None:
                    if confirm:
                        self.flash(confirm, "warn")
                    return True
                self.confirm = (confirm, request)
                return True
        return True

    # -- loop -------------------------------------------------------------- #

    def run(self) -> None:
        last_poll = 0.0
        while True:
            now = time.monotonic()
            if now - last_poll > 1.0 and not self.job:
                last_poll = now
                self.reload_state()
                for page in self.pages:
                    page.refresh()
            self.pump_job()
            self.pump_auth()
            self.spinner += 1
            self.draw()
            key = self.ui.win.getch()
            if key == -1:
                continue
            if key == curses.KEY_RESIZE:
                continue
            if not self.handle_key(key):
                return


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current or not lines:
        lines.append(current)
    return lines


def _main(stdscr) -> None:
    curses.curs_set(0)
    stdscr.timeout(120)
    stdscr.keypad(True)
    App(Screen(stdscr)).run()


def run() -> int:
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(_main)
    except KeyboardInterrupt:
        pass
    return 0
