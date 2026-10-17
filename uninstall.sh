#!/bin/sh
# Remove GaugeDeck and everything it added for the current user.
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"

# Take pinned gauges off the dock first (needs the installed program; harmless if it is gone)
if [ -x "$HOME/.local/bin/gaugedeck" ]; then
    "$HOME/.local/bin/gaugedeck" --remove-launchers 2>/dev/null || true
fi

# Launchers and icons made for pinned gauges and the strip (also covers the program already being gone)
rm -f "$APPS"/io.github.cyoulater.GaugeDeck.*.desktop "$ICONS"/io.github.cyoulater.GaugeDeck.*.svg

rm -f "$HOME/.local/bin/gaugedeck" \
      "$APPS/gaugedeck.desktop" \
      "$APPS/io.github.cyoulater.GaugeDeck.desktop" \
      "$ICONS/io.github.cyoulater.GaugeDeck.svg" \
      "$ICONS/gaugedeck.svg"

# Settings, look, sensor choice and window layout (your own pictures are not touched)
rm -rf "$HOME/.config/gaugedeck"

update-desktop-database "$APPS" 2>/dev/null || true
gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
echo "GaugeDeck removed."
