# 🏝️ Pulo Notch - Dynamic Island Linux

Dynamic Island modern dan ringan bergaya MacBook Notch untuk desktop Linux (mendukung **Wayland** dan **X11**).

---

## ✨ Fitur Unggulan

1. **Desain Fleksibel (2 Bentuk Tampilan)**:
   - **MacBook Notch**: Menempel mulus di tepi atas layar.
   - **Floating Pill**: Kapsul melayang rounded yang modern.
2. **Mode Standby Interaktif**:
   - Jam digital (menampilkan detik saat kursor didekatkan).
   - Indikator baterai donat dengan animasi charger & persentase.
   - Perkiraan cuaca lokal langsung dari satelit.
   - Animasi mata interaktif (kedip alami, lirikan mengikuti kursor, ekspresi mengetik, ekspresi mengantuk saat idle, dan ekspresi marah saat disentuh).
   - **Indikator IHSG & Pasar Modal**: Menampilkan tren naik/turun IHSG secara live (`▲ +3.8%`).
   - **Live Karaoke Lyrics**: Lirik lagu berjalan saat mendengarkan musik di Spotify / YouTube Music / browser.
3. **Panel Cepat 7 Tab**:
   - **Notifikasi**: Pop-up toast auto-pause saat di-hover dan riwayat notifikasi lengkap.
   - **Musik & Media**: Kontrol pemutar musik, seek bar, volume, cover album, dan lirik sinkron.
   - **Jadwal Sholat & Adzan**: Waktu sholat otomatis untuk seluruh kota di Indonesia + hitung mundur dan notifikasi adzan.
   - **Prakiraan Cuaca**: Kondisi cuaca per jam & harian dengan ikon animasi.
   - **Pasar Saham & Keuangan**: Indeks IHSG real-time, grafik *sparkline* intraday, dan kurs nilai tukar Rupiah (USD, SGD, EUR).
   - **LocalSend**: Kirim berkas antar perangkat secara drag & drop.
   - **Control Center**: Pintasan cepat Wi-Fi, Bluetooth, Mode Jangan Ganggu (DND), Mute Mic, Screenshot layar, Lock screen, serta slider Volume & Kecerahan.
4. **Fitur Kesehatan**:
   - Pengingat istirahat mata aturan 20-20-20.
   - Pengingat berdiri & peregangan setelah duduk lama.

---

## 🚀 Cara Instalasi di Laptop Teman

### Opsi 1: Paket Debian / Ubuntu (.deb) - Paling Mudah
Cocok untuk: **Ubuntu, Linux Mint, Debian, Pop!_OS, Zorin OS, Elementary OS**.

1. Cukup buka terminal di folder ini, lalu jalankan:
   ```bash
   sudo apt install ./pulo_1.0.0_all.deb
   ```
   *(Sistem akan otomatis mengunduh seluruh dependensi yang diperlukan)*
2. Buka menu aplikasi dan cari **"Pulo Notch"**, atau jalankan perintah:
   ```bash
   pulo
   ```

---

### Opsi 2: Skrip Installer 1-Klik (install.sh)
Cocok untuk semua distro: **Ubuntu, Arch Linux, Manjaro, Fedora, openSUSE**.

1. Buka terminal di folder ini dan jalankan:
   ```bash
   ./install.sh
   ```
2. Skrip akan otomatis mendeteksi distro Anda, menginstal dependensi, memasang ikon aplikasi, dan menawarkan opsi autostart saat laptop menyala.

---

### Opsi 3: Menjalankan Langsung Tanpa Instalasi (Portabel)
Pastikan Python 3, PyQt6, dan GTK 3 sudah terpasang:
- **Ubuntu/Debian**:
  ```bash
  sudo apt install python3-pyqt6 python3-gi gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0 libgtk-layer-shell0
  ```
- **Arch/Manjaro**:
  ```bash
  sudo pacman -S python-pyqt6 python-gobject gtk3 gtk-layer-shell
  ```
- **Fedora**:
  ```bash
  sudo dnf install python3-qt6 python3-gobject gtk3 gtk-layer-shell
  ```

Lalu jalankan langsung:
```bash
python3 pulau.py
```

---

## ⌨️ Perintah CLI Pintas

| Perintah | Fungsi |
|---|---|
| `pulo` | Menjalankan Pulo Notch di background |
| `pulo --toggle` | Buka / tutup panel |
| `pulo --finance` | Langsung buka panel Keuangan (IHSG & Kurs) |
| `pulo --control` | Langsung buka panel Control Center |
| `pulo --settings` | Buka jendela Pusat Pengaturan |
| `pulo --shape pill` | Ganti bentuk menjadi Floating Pill |
| `pulo --shape notch` | Ganti bentuk menjadi MacBook Notch |
| `pulo --stop` | Menghentikan Pulo Notch di background |
| `pulo --restart` | Mulai ulang Pulo Notch |
| `pulo --status` | Lihat status proses Pulo Notch |

---

## 🗑️ Cara Uninstall
Cukup jalankan:
```bash
./uninstall.sh
```
Atau jika dipasang via paket `.deb`:
```bash
sudo apt remove pulo
```
