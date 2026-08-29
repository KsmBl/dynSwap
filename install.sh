#!/usr/bin/env bash
#
# dynSwap installer.
#
#   ./install.sh              install to /usr
#   ./install.sh --uninstall  remove every installed file
#   ./install.sh --prefix=/usr/local
#
set -euo pipefail

APP_ID="de.synthelicz.dynSwap"
PREFIX="/usr"
ACTION="install"
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

for arg in "$@"; do
  case "$arg" in
    --uninstall|-u) ACTION="uninstall" ;;
    --prefix=*)     PREFIX="${arg#*=}" ;;
    --help|-h)
      sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

LIBDIR="$PREFIX/lib/dynswap"
BINDIR="$PREFIX/bin"
POLICY_DIR="$PREFIX/share/polkit-1/actions"
DESKTOP_DIR="$PREFIX/share/applications"
ICON_DIR="$PREFIX/share/icons/hicolor/scalable/apps"
HELPER="$LIBDIR/dynswap-helper"

# ---------------------------------------------------------------- output ---
if [ -t 1 ]; then
  B=$'\e[1m'; DIM=$'\e[2m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; RED=$'\e[31m'
  ACCENT=$'\e[35m'; R=$'\e[0m'
else
  B=""; DIM=""; GREEN=""; YELLOW=""; RED=""; ACCENT=""; R=""
fi
say()  { printf '  %s\n' "$*"; }
ok()   { printf '  %s✔%s %s\n' "$GREEN" "$R" "$*"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$R" "$*"; }
die()  { printf '  %s✘%s %s\n' "$RED" "$R" "$*" >&2; exit 1; }
head_() { printf '\n%s%s%s\n' "$B" "$*" "$R"; }

printf '\n%s  dynSwap%s %sswap control for Linux%s\n' "$ACCENT$B" "$R" "$DIM" "$R"
[ "$ACTION" = "install" ] && printf '  %sprefix: %s%s\n' "$DIM" "$PREFIX" "$R"

# ------------------------------------------------------------------ root ---
# A system prefix needs root.  A prefix the caller already owns (say
# ~/.local) is installed without it, minus the bits that only work system-wide.
prefix_writable() {
  local probe="$PREFIX"
  while [ ! -e "$probe" ] && [ "$probe" != "/" ]; do probe="$(dirname "$probe")"; done
  [ -w "$probe" ]
}

USER_INSTALL=0
if [ "$(id -u)" -ne 0 ]; then
  if prefix_writable; then
    USER_INSTALL=1
  elif command -v sudo >/dev/null 2>&1; then
    say "${DIM}re-running with sudo…${R}"
    exec sudo -- "$0" "$@"
  else
    die "this script must run as root to install into $PREFIX"
  fi
fi

# install(1) may only set ownership as root
OWN=(-o root -g root)
[ "$USER_INSTALL" -eq 1 ] && OWN=()

# ------------------------------------------------------------- uninstall ---
if [ "$ACTION" = "uninstall" ]; then
  head_ "Removing dynSwap"
  for path in \
    "$BINDIR/dynswap" \
    "$POLICY_DIR/$APP_ID.policy" \
    "$DESKTOP_DIR/$APP_ID.desktop" \
    "$ICON_DIR/$APP_ID.svg"
  do
    if [ -e "$path" ]; then rm -f "$path"; ok "removed $path"; fi
  done
  if [ -d "$LIBDIR" ]; then rm -rf "$LIBDIR"; ok "removed $LIBDIR"; fi

  command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    gtk-update-icon-cache -qtf "$PREFIX/share/icons/hicolor" 2>/dev/null || true
  command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database -q "$DESKTOP_DIR" 2>/dev/null || true

  head_ "Left in place"
  say "Your swap itself is untouched. To undo swap changes dynSwap made:"
  say "  ${DIM}swapoff /swapfile && rm /swapfile${R}   and drop its /etc/fstab line"
  say "  ${DIM}rm /etc/sysctl.d/99-dynswap.conf${R}"
  say "  ${DIM}systemctl disable --now dynswap-zram.service${R}"
  printf '\n'
  exit 0
fi

# ---------------------------------------------------------- dependencies ---
head_ "Checking dependencies"

command -v python3 >/dev/null 2>&1 || die "python3 is required"
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 9) else 0)')
[ "$PY_OK" = "1" ] || die "python3 3.9 or newer is required (found $(python3 -V 2>&1))"
ok "python3 $(python3 -c 'import platform; print(platform.python_version())')"

MISSING=""
for tool in mkswap swapon swapoff findmnt; do
  command -v "$tool" >/dev/null 2>&1 || MISSING="$MISSING $tool"
done
[ -z "$MISSING" ] || die "missing core tools:$MISSING (install util-linux)"
ok "util-linux tools present"

if command -v zramctl >/dev/null 2>&1; then
  ok "zramctl present — zram management available"
else
  warn "zramctl not found; the zram page will not be able to apply changes"
fi

if python3 - <<'PY' 2>/dev/null
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
PY
then
  ok "GTK 4 and libadwaita found — GUI available"
else
  warn "GTK 4 / libadwaita Python bindings missing — TUI only"
  case "$(. /etc/os-release 2>/dev/null && echo "${ID_LIKE:-$ID}")" in
    *arch*)  say "    ${DIM}sudo pacman -S python-gobject gtk4 libadwaita${R}" ;;
    *debian*|*ubuntu*) say "    ${DIM}sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1${R}" ;;
    *fedora*|*rhel*)   say "    ${DIM}sudo dnf install python3-gobject gtk4 libadwaita${R}" ;;
  esac
fi

if command -v pkexec >/dev/null 2>&1; then
  ok "pkexec present — the GUI can ask for a password graphically"
else
  warn "pkexec not found; dynSwap will fall back to sudo"
fi

# --------------------------------------------------------------- install ---
head_ "Installing to $PREFIX"

install -d -m 0755 "$LIBDIR" "$LIBDIR/dynswap" "$BINDIR" \
                   "$POLICY_DIR" "$DESKTOP_DIR" "$ICON_DIR"

for module in "$SOURCE_DIR"/dynswap/*.py; do
  install -m 0644 "${OWN[@]}" "$module" "$LIBDIR/dynswap/"
done
rm -rf "$LIBDIR/dynswap/__pycache__"
ok "python package  → $LIBDIR/dynswap"

install -m 0755 "${OWN[@]}" "$SOURCE_DIR/dynswap-helper" "$HELPER"
ok "privileged helper → $HELPER"

cat > "$BINDIR/dynswap" <<LAUNCHER
#!/bin/sh
# dynSwap launcher — installed by install.sh
PYTHONPATH="$LIBDIR\${PYTHONPATH:+:\$PYTHONPATH}"
export PYTHONPATH
exec python3 -m dynswap "\$@"
LAUNCHER
chmod 0755 "$BINDIR/dynswap"
ok "launcher        → $BINDIR/dynswap"

if [ "$USER_INSTALL" -eq 1 ]; then
  warn "skipping the polkit policy — it is only read from /usr/share/polkit-1"
  say "    ${DIM}dynSwap will ask for your password through sudo instead${R}"
else
  sed "s|@HELPER@|$HELPER|g" "$SOURCE_DIR/data/$APP_ID.policy.in" \
    > "$POLICY_DIR/$APP_ID.policy"
  chmod 0644 "$POLICY_DIR/$APP_ID.policy"
  ok "polkit policy   → $POLICY_DIR/$APP_ID.policy"
fi

install -m 0644 "$SOURCE_DIR/data/$APP_ID.desktop" "$DESKTOP_DIR/"
install -m 0644 "$SOURCE_DIR/data/$APP_ID.svg" "$ICON_DIR/"
ok "desktop entry and icon"

python3 -m compileall -q "$LIBDIR/dynswap" >/dev/null 2>&1 || true

command -v gtk-update-icon-cache >/dev/null 2>&1 && \
  gtk-update-icon-cache -qtf "$PREFIX/share/icons/hicolor" 2>/dev/null || true
command -v update-desktop-database >/dev/null 2>&1 && \
  update-desktop-database -q "$DESKTOP_DIR" 2>/dev/null || true

# ------------------------------------------------------------ verify ---
head_ "Verifying"
if "$BINDIR/dynswap" --version >/dev/null 2>&1; then
  ok "$("$BINDIR/dynswap" --version) responds"
else
  die "the installed launcher does not run"
fi
if "$HELPER" --help >/dev/null 2>&1; then
  ok "helper responds"
else
  die "the installed helper does not run"
fi

head_ "Done"
say "${B}dynswap${R}           open the GUI on a desktop, the TUI in a terminal"
say "${B}dynswap --tui${R}     force the terminal interface"
say "${B}dynswap --gui${R}     force the graphical interface"
say "${B}dynswap --status${R}  print the current configuration and exit"
printf '\n'
say "${DIM}Uninstall with: $SOURCE_DIR/install.sh --uninstall${R}"
printf '\n'
