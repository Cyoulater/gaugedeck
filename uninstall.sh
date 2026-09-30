#!/bin/sh
rm -f "$HOME/.local/bin/gaugedeck" \
      "$HOME/.local/share/applications/gaugedeck.desktop" \
      "$HOME/.local/share/applications/io.github.cyoulater.GaugeDeck.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/io.github.cyoulater.GaugeDeck.svg" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/gaugedeck.svg"
rm -rf "$HOME/.config/gaugedeck"
echo "GaugeDeck removed."
