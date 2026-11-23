#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_ROOT="$DIR/pkg_build"

rm -rf "$PKG_ROOT"
mkdir -p "$PKG_ROOT/DEBIAN"
mkdir -p "$PKG_ROOT/usr/bin"
mkdir -p "$PKG_ROOT/usr/share/pulo"
mkdir -p "$PKG_ROOT/usr/share/applications"
mkdir -p "$PKG_ROOT/usr/share/icons/hicolor/scalable/apps"
mkdir -p "$PKG_ROOT/usr/share/icons/hicolor/256x256/apps"

# Copy main executable and script
cp "$DIR/pulau.py" "$PKG_ROOT/usr/share/pulo/pulau.py"
chmod +x "$PKG_ROOT/usr/share/pulo/pulau.py"

# Launcher script in /usr/bin/pulo
cat << 'EOF' > "$PKG_ROOT/usr/bin/pulo"
#!/usr/bin/env bash
exec /usr/bin/python3 /usr/share/pulo/pulau.py "$@"
EOF
chmod +x "$PKG_ROOT/usr/bin/pulo"

# Desktop entry
cat << 'EOF' > "$PKG_ROOT/usr/share/applications/pulo.desktop"
[Desktop Entry]
Name=Pulo Notch
GenericName=Dynamic Island
Comment=MacBook-Style Dynamic Island for Linux Desktop
Exec=pulo
Icon=pulo
Terminal=false
Type=Application
Categories=Utility;DesktopSettings;GTK;
StartupNotify=false
EOF
chmod 644 "$PKG_ROOT/usr/share/applications/pulo.desktop"

# Icons
cp "$DIR/pulo.svg" "$PKG_ROOT/usr/share/icons/hicolor/scalable/apps/pulo.svg"
cp "$DIR/pulo.png" "$PKG_ROOT/usr/share/icons/hicolor/256x256/apps/pulo.png"
chmod 644 "$PKG_ROOT/usr/share/icons/hicolor/scalable/apps/pulo.svg"
chmod 644 "$PKG_ROOT/usr/share/icons/hicolor/256x256/apps/pulo.png"

# Control file
cat << 'EOF' > "$PKG_ROOT/DEBIAN/control"
Package: pulo
Version: 1.0.0
Section: utils
Priority: optional
Architecture: all
Depends: python3, python3-pyqt6, python3-gi, gir1.2-gtk-3.0, gir1.2-gdkpixbuf-2.0
Recommends: libgtk-layer-shell0, playerctl, brightnessctl, pulseaudio-utils | pipewire-bin, network-manager, bluez
Maintainer: Pulo Project <support@pulo.local>
Description: MacBook-Style Dynamic Island for Linux
 A modern, lightweight Dynamic Island for Linux desktop (Wayland & X11).
 Features live battery indicators, media player with real-time karaoke lyrics,
 prayer times & adzan alert, weather forecast, stock market (IHSG) and currency
 rates, health reminders (20-20-20 & stretch), LocalSend file sharing,
 and a quick System Control Center.
EOF

# Postinst hook (update icon cache)
cat << 'EOF' > "$PKG_ROOT/DEBIAN/postinst"
#!/bin/sh
set -e
if which update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
if which gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
exit 0
EOF
chmod 755 "$PKG_ROOT/DEBIAN/postinst"

# Build deb package
DEB_FILE="$DIR/pulo_1.0.0_all.deb"
dpkg-deb --build --root-owner-group "$PKG_ROOT" "$DEB_FILE"
rm -rf "$PKG_ROOT"

echo "Package successfully built at: $DEB_FILE"
