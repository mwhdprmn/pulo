#!/usr/bin/env bash
# =============================================================================
# Pulo Notch - One-Click Installer for Linux
# Supported Distros: Ubuntu, Debian, Mint, Pop!_OS, Fedora, Arch, Manjaro, openSUSE
# =============================================================================

set -e

BOLD="\033[1m"
GREEN="\033[32m"
CYAN="\033[36m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

echo -e "${BOLD}${CYAN}"
echo "  ╔════════════════════════════════════════════════════════════╗"
echo "  ║           🏝️   PULO NOTCH - DYNAMIC ISLAND LINUX           ║"
echo "  ║       MacBook-Style Notch Dynamic Island for Desktop       ║"
echo "  ╚════════════════════════════════════════════════════════════╝"
echo -e "${RESET}"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# -----------------------------------------------------------------------------
# 1. Deteksi Distro & Instal Dependensi Sistem
# -----------------------------------------------------------------------------
echo -e "${BOLD}Memeriksa dependensi sistem...${RESET}"

install_dependencies() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        DISTRO_ID="$ID"
        DISTRO_LIKE="$ID_LIKE"
    else
        DISTRO_ID="unknown"
        DISTRO_LIKE="unknown"
    fi

    echo -e "Distro terdeteksi: ${CYAN}${PRETTY_NAME:-$DISTRO_ID}${RESET}"

    if command -v apt-get >/dev/null 2>&1; then
        echo -e "${YELLOW}Menginstal dependensi via APT (membutuhkan sudo)...${RESET}"
        sudo apt-get update -qq
        sudo apt-get install -y -qq python3 python3-pyqt6 python3-gi gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0 libgtk-layer-shell0 playerctl brightnessctl pulseaudio-utils || true
    elif command -v pacman >/dev/null 2>&1; then
        echo -e "${YELLOW}Menginstal dependensi via Pacman (Arch/Manjaro)...${RESET}"
        sudo pacman -S --needed --noconfirm python python-pyqt6 python-gobject gtk3 gtk-layer-shell playerctl brightnessctl || true
    elif command -v dnf >/dev/null 2>&1; then
        echo -e "${YELLOW}Menginstal dependensi via DNF (Fedora)...${RESET}"
        sudo dnf install -y python3 python3-qt6 python3-gobject gtk3 gtk-layer-shell playerctl brightnessctl || true
    elif command -v zypper >/dev/null 2>&1; then
        echo -e "${YELLOW}Menginstal dependensi via Zypper (openSUSE)...${RESET}"
        sudo zypper install -y python3 python3-qt6 python3-gobject gtk3 gtk-layer-shell playerctl brightnessctl || true
    else
        echo -e "${YELLOW}Manajer paket tidak dikenal. Pastikan Python 3, PyQt6, PyGObject, dan GTK 3 sudah terpasang.${RESET}"
    fi
}

# Tanya apakah ingin menginstal dependensi
read -r -p "Instal dependensi sistem yang diperlukan sekarang? [Y/n] " prompt_dep
prompt_dep=${prompt_dep:-Y}
if [[ "$prompt_dep" =~ ^[Yy]$ ]]; then
    install_dependencies
fi

# -----------------------------------------------------------------------------
# 2. Pasang Berkas Pulo ke ~/.local/bin dan ~/.local/share
# -----------------------------------------------------------------------------
INSTALL_BIN="$HOME/.local/bin"
INSTALL_SHARE="$HOME/.local/share/pulo"
INSTALL_APPS="$HOME/.local/share/applications"
INSTALL_ICONS_SVG="$HOME/.local/share/icons/hicolor/scalable/apps"
INSTALL_ICONS_PNG="$HOME/.local/share/icons/hicolor/256x256/apps"
INSTALL_AUTOSTART="$HOME/.config/autostart"

mkdir -p "$INSTALL_BIN" "$INSTALL_SHARE" "$INSTALL_APPS" "$INSTALL_ICONS_SVG" "$INSTALL_ICONS_PNG" "$INSTALL_AUTOSTART"

echo -e "Menyalin berkas aplikasi..."
cp "$DIR/pulau.py" "$INSTALL_SHARE/pulau.py"
chmod +x "$INSTALL_SHARE/pulau.py"

# Wrapper executable pulo di ~/.local/bin/pulo
cat << 'EOF' > "$INSTALL_BIN/pulo"
#!/usr/bin/env bash
exec /usr/bin/env python3 "$HOME/.local/share/pulo/pulau.py" "$@"
EOF
chmod +x "$INSTALL_BIN/pulo"

# Icons
if [ -f "$DIR/pulo.svg" ]; then
    cp "$DIR/pulo.svg" "$INSTALL_ICONS_SVG/pulo.svg"
fi
if [ -f "$DIR/pulo.png" ]; then
    cp "$DIR/pulo.png" "$INSTALL_ICONS_PNG/pulo.png"
fi

# Desktop Entry
cat << EOF > "$INSTALL_APPS/pulo.desktop"
[Desktop Entry]
Name=Pulo Notch
GenericName=Dynamic Island
Comment=MacBook-Style Dynamic Island for Linux Desktop
Exec=$INSTALL_BIN/pulo
Icon=pulo
Terminal=false
Type=Application
Categories=Utility;DesktopSettings;GTK;
StartupNotify=false
EOF
chmod 644 "$INSTALL_APPS/pulo.desktop"

# Perbarui cache ikon & desktop jika ada
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$INSTALL_APPS" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
fi

# -----------------------------------------------------------------------------
# 3. Opsi Autostart Saat Login
# -----------------------------------------------------------------------------
read -r -p "Jalankan Pulo Notch otomatis saat laptop menyala / login? [Y/n] " prompt_auto
prompt_auto=${prompt_auto:-Y}
if [[ "$prompt_auto" =~ ^[Yy]$ ]]; then
    cp "$INSTALL_APPS/pulo.desktop" "$INSTALL_AUTOSTART/pulo.desktop"
    echo -e "${GREEN}✓ Autostart diaktifkan.${RESET}"
else
    rm -f "$INSTALL_AUTOSTART/pulo.desktop"
fi

# Pastikan ~/.local/bin ada di PATH
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo -e "${YELLOW}Catatan: Pastikan '$HOME/.local/bin' sudah ada di PATH shell Anda (.bashrc / .zshrc).${RESET}"
fi

echo -e "\n${BOLD}${GREEN}✓ Instalasi Pulo Notch Berhasil!${RESET}"
echo -e "Anda sekarang dapat menjalankan Pulo melalui:"
echo -e "  • Menu Aplikasi desktop: cari ${BOLD}Pulo Notch${RESET}"
echo -e "  • Perintah terminal: ${BOLD}pulo${RESET} atau ${BOLD}pulo --toggle${RESET}"
echo ""

# Jalankan Pulo sekarang jika diminta
read -r -p "Jalankan Pulo Notch sekarang? [Y/n] " prompt_run
prompt_run=${prompt_run:-Y}
if [[ "$prompt_run" =~ ^[Yy]$ ]]; then
    "$INSTALL_BIN/pulo" &
    echo -e "${GREEN}Pulo Notch telah dijalankan di background!${RESET}"
fi
