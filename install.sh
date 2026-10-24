#!/bin/sh
# GaugeDeck installer - installs for the current user only (no system files touched)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Checking for GTK 4 and Python support..."
if ! python3 -c "import gi; gi.require_version('Gtk','4.0'); from gi.repository import Gtk; gi.require_foreign('cairo')" 2>/dev/null; then
    echo "Installing required packages (asks for your password)..."
    sudo apt install -y python3-gi python3-gi-cairo gir1.2-gtk-4.0
fi

mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
install -m 755 "$DIR/gaugedeck.py" "$HOME/.local/bin/gaugedeck"
install -m 644 "$DIR/io.github.cyoulater.GaugeDeck.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/io.github.cyoulater.GaugeDeck.svg"
sed "s|^Exec=.*|Exec=$HOME/.local/bin/gaugedeck|" "$DIR/io.github.cyoulater.GaugeDeck.desktop" > "$HOME/.local/share/applications/io.github.cyoulater.GaugeDeck.desktop"
# clean up the 1.0.0-1.0.2 file names if present
rm -f "$HOME/.local/share/applications/gaugedeck.desktop" "$HOME/.local/share/icons/hicolor/scalable/apps/gaugedeck.svg"

# optional: custom title, e.g.  ./install.sh "MY RIG"
# Only the title changes: any other settings already in the config file are kept.
if [ -n "$1" ]; then
    CFG_DIR="$HOME/.config/gaugedeck"
    mkdir -p "$CFG_DIR"
    if [ -f "$CFG_DIR/config" ]; then
        grep -v '^title=' "$CFG_DIR/config" > "$CFG_DIR/config.new" || true
        printf 'title=%s\n' "$1" >> "$CFG_DIR/config.new"
        mv "$CFG_DIR/config.new" "$CFG_DIR/config"
    else
        printf 'title=%s\nscale=0.6\n' "$1" > "$CFG_DIR/config"
    fi
fi

update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
echo "Done. Open 'GaugeDeck' from your app menu (Super key, then type GaugeDeck)."
