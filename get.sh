#!/bin/sh
# One-line GaugeDeck install for any Linux desktop:
#   curl -fsSL https://raw.githubusercontent.com/Cyoulater/gaugedeck/main/get.sh | sh
# Installs GTK 4 support with your package manager (asks for your password),
# then installs GaugeDeck for your user only. Optional title:
#   curl -fsSL https://raw.githubusercontent.com/Cyoulater/gaugedeck/main/get.sh | sh -s -- "MY RIG"
set -e
REPO=https://github.com/Cyoulater/gaugedeck

has() { command -v "$1" >/dev/null 2>&1; }
gtk_ok() {
    python3 -c "import gi; gi.require_version('Gtk','4.0'); from gi.repository import Gtk; gi.require_foreign('cairo')" 2>/dev/null
}

if ! gtk_ok; then
    echo "Installing GTK 4 and Python support (asks for your password)..."
    if has apt-get; then sudo apt-get install -y python3-gi python3-gi-cairo gir1.2-gtk-4.0
    elif has dnf; then sudo dnf install -y python3-gobject python3-cairo gobject-introspection gtk4
    elif has pacman; then sudo pacman -S --needed --noconfirm python-gobject python-cairo gtk4
    elif has zypper; then sudo zypper --non-interactive install python3-gobject python3-gobject-cairo typelib-1_0-Gtk-4_0
    else
        echo "Couldn't find apt, dnf, pacman or zypper. Install GTK 4 and Python GObject" >&2
        echo "(PyGObject with cairo) yourself, then run this again." >&2
        exit 1
    fi
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "Downloading GaugeDeck..."
if has curl; then GET="curl -fsSL -o"; else GET="wget -qO"; fi
# latest release; before the first packaged release, fall back to main
$GET "$TMP/g.tar.gz" "$REPO/releases/latest/download/gaugedeck.tar.gz" 2>/dev/null ||
    $GET "$TMP/g.tar.gz" "$REPO/archive/refs/heads/main.tar.gz"
tar -xzf "$TMP/g.tar.gz" -C "$TMP"
sh "$(dirname "$(find "$TMP" -name install.sh | head -n 1)")/install.sh" "$@"
