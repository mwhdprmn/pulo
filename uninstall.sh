#!/usr/bin/env bash
# =============================================================================
# Pulo Notch - Uninstaller
# =============================================================================

set -e

echo "Menghentikan proses Pulo Notch jika sedang berjalan..."
if command -v pulo >/dev/null 2>&1; then
    pulo --stop >/dev/null 2>&1 || true
fi

echo "Menghapus berkas Pulo Notch..."
rm -f "$HOME/.local/bin/pulo"
rm -rf "$HOME/.local/share/pulo"
rm -f "$HOME/.local/share/applications/pulo.desktop"
rm -f "$HOME/.config/autostart/pulo.desktop"
rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/pulo.svg"
rm -f "$HOME/.local/share/icons/hicolor/256x256/apps/pulo.png"

read -r -p "Hapus riwayat cache dan pengaturan lokal (~/.cache/pulo dan ~/.config/pulo)? [y/N] " del_cache
if [[ "$del_cache" =~ ^[Yy]$ ]]; then
    rm -rf "$HOME/.cache/pulo"
    rm -rf "$HOME/.config/pulo"
    echo "Cache dan konfigurasi telah dihapus."
fi

echo "Pulo Notch berhasil dicopot (uninstalled)."
