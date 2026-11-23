#!/usr/bin/env python3
"""
Pulo - MacBook-Style Notch Dynamic Island for Linux (Wayland / X11)
Renders a seamless borderless top-edge Notch (height = 28px matching taskbar) with concave top ears,
smooth 60fps spring morphing, real-time clock, battery/charger/full indicator, D-Bus notifications,
and separate Notifikasi / Pengaturan tabs.
"""

import array
import datetime
import fcntl
import glob
import html
import json
import math
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import ctypes
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    import cairo
    HAS_CAIRO = True
except ImportError:
    HAS_CAIRO = False

_libgdk = ctypes.CDLL("libgdk-3.so.0")
_libgtk = ctypes.CDLL("libgtk-3.so.0")
_libcairo = ctypes.CDLL("libcairo.so.2")

_libgdk.gdk_cairo_surface_create_from_pixbuf.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
_libgdk.gdk_cairo_surface_create_from_pixbuf.restype = ctypes.c_void_p

_libgtk.gtk_image_set_from_surface.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_libgtk.gtk_image_set_from_surface.restype = None

_libcairo.cairo_surface_destroy.argtypes = [ctypes.c_void_p]
_libcairo.cairo_surface_destroy.restype = None

_libcairo.cairo_image_surface_create_for_data.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
_libcairo.cairo_image_surface_create_for_data.restype = ctypes.c_void_p

_libcairo.cairo_surface_set_device_scale.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]
_libcairo.cairo_surface_set_device_scale.restype = None

os.environ.setdefault("QT_NO_GLIB", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk

GLib.set_prgname("pulo")
GLib.set_application_name("Pulo")
Gdk.set_program_class("pulo")

import gc

try:
    threading.stack_size(524288)  # 512 KB per thread instead of default 8 MB
except Exception:
    pass

try:
    _libc = ctypes.CDLL(None)
    # PR_SET_NAME = 15
    _libc.prctl(15, b"pulo\0", 0, 0, 0)
except Exception:
    pass

try:
    _libc_trim = ctypes.CDLL("libc.so.6")
    _malloc_trim = _libc_trim.malloc_trim
    _malloc_trim.argtypes = [ctypes.c_size_t]
    _malloc_trim.restype = ctypes.c_int
except Exception:
    _malloc_trim = None


def trim_memory():
    """Forces garbage collection and releases freed heap memory back to the OS."""
    try:
        gc.collect(1)
        if _malloc_trim is not None:
            _malloc_trim(0)
    except Exception:
        pass

try:
    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import GtkLayerShell

    HAS_LAYER_SHELL = GtkLayerShell.is_supported()
except Exception:
    GtkLayerShell = None
    HAS_LAYER_SHELL = False

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QIcon,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

_qt_app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
_qt_app.setApplicationName("Pulo")
_qt_app.setApplicationDisplayName("Pulo")
_qt_app.setDesktopFileName("pulo")


# =============================================================================
# Data Models & Hardware Telemetry
# =============================================================================
@dataclass
class BatteryState:
    capacity: int = 100
    status: str = "Full"  # Charging, Discharging, Full, Not charging
    ac_online: bool = True
    present: bool = True
    voltage_v: float = 0.0
    model: str = "Baterai"
    error: Optional[str] = None

    @property
    def is_full(self) -> bool:
        return self.status == "Full" or (self.ac_online and self.capacity >= 99)

    @property
    def is_charging(self) -> bool:
        return self.status == "Charging"

    @property
    def is_plugged_idle(self) -> bool:
        return self.ac_online and not self.is_charging and not self.is_full

    @property
    def is_low(self) -> bool:
        return not self.ac_online and self.capacity <= 20

    @property
    def short_badge(self) -> str:
        if self.is_full:
            return "PENUH"
        if self.is_charging:
            return "MENGISI"
        if self.is_plugged_idle:
            return "DI CHARGER"
        if self.is_low:
            return "BATERAI LEMAH"
        return "BATERAI"

    @property
    def detail_label(self) -> str:
        if self.error:
            return "Sensor daya tidak terbaca"
        if self.is_full:
            return "Baterai Penuh • Terhubung Charger"
        if self.is_charging:
            return "Sedang Mengisi Daya • Charger Aktif"
        if self.is_plugged_idle:
            return f"Terhubung Charger • Daya Terjaga ({self.capacity}%)"
        if self.is_low:
            return "Daya Baterai Lemah • Segera Isi Daya"
        return "Menggunakan Daya Baterai"


@dataclass
class NotificationItem:
    id: int
    app_name: str
    summary: str
    body: str
    timestamp: str
    urgency: int = 1
    created_at: float = field(default_factory=time.time)
    app_icon: str = ""
    desktop_entry: str = ""
    resolved_icon: Optional[Any] = None
    _elided_cache: Optional[Tuple] = None


@dataclass
class HudItem:
    kind: str  # "volume", "mute", "mic", "brightness", "bluetooth"
    value: int = 0
    is_muted: bool = False
    extra_text: str = ""
    started_at: float = field(default_factory=time.time)
    duration: float = 2.2


class PowerReader:
    """Reads real battery & AC adapter status from Linux /sys/class/power_supply."""

    @staticmethod
    def read() -> BatteryState:
        base = "/sys/class/power_supply"
        state = BatteryState()
        try:
            if not os.path.isdir(base):
                state.error = "No power_supply"
                return state

            entries = os.listdir(base)
            bat_dir = None
            ac_online = False

            for name in sorted(entries):
                p = os.path.join(base, name)
                ptype = PowerReader._read_str(os.path.join(p, "type"))
                if ptype == "Mains":
                    if PowerReader._read_str(os.path.join(p, "online")) == "1":
                        ac_online = True
                elif ptype == "Battery" and name.startswith("BAT") and bat_dir is None:
                    bat_dir = p

            if bat_dir is None:
                for name in sorted(entries):
                    p = os.path.join(base, name)
                    if PowerReader._read_str(os.path.join(p, "type")) == "Battery":
                        bat_dir = p
                        break

            state.ac_online = ac_online
            if bat_dir is None:
                state.present = False
                state.capacity = 100
                state.status = "Full"
                state.model = "AC Desktop"
                return state

            cap_str = PowerReader._read_str(os.path.join(bat_dir, "capacity"))
            status_str = PowerReader._read_str(os.path.join(bat_dir, "status"))
            volt_str = PowerReader._read_str(os.path.join(bat_dir, "voltage_now"))
            mfg = PowerReader._read_str(os.path.join(bat_dir, "manufacturer"))
            mdl = PowerReader._read_str(os.path.join(bat_dir, "model_name"))

            state.capacity = max(0, min(100, int(cap_str))) if cap_str.isdigit() else 0
            state.status = status_str or ("Charging" if ac_online else "Discharging")
            if volt_str and volt_str.isdigit():
                state.voltage_v = int(volt_str) / 1_000_000.0
            label_parts = [x for x in (mfg, mdl) if x]
            state.model = " ".join(label_parts) if label_parts else os.path.basename(bat_dir)
            return state
        except Exception as exc:
            state.error = str(exc)
            return state

    @staticmethod
    def _read_str(path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return ""


class DBusNotificationListener(threading.Thread):
    """Monitors desktop notifications via dbus-monitor without hijacking the notification daemon."""

    TAG_RE = re.compile(r"<[^>]+>")

    def __init__(self, on_notify_callback):
        super().__init__(daemon=True)
        self.on_notify_callback = on_notify_callback
        self._stop_event = threading.Event()
        self._proc: Optional[subprocess.Popen] = None

    def stop(self):
        self._stop_event.set()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass

    @classmethod
    def clean_text(cls, text: str) -> str:
        unescaped = html.unescape(text or "")
        cleaned = cls.TAG_RE.sub("", unescaped)
        return " ".join(cleaned.split()).strip()

    def run(self):
        cmd = [
            "dbus-monitor",
            "interface='org.freedesktop.Notifications',member='Notify'",
        ]
        while not self._stop_event.is_set():
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
                in_notify = False
                strings: List[str] = []
                array_depth = 0
                hints_dict: Dict[str, str] = {}
                pending_hint_key: Optional[str] = None

                for raw_line in self._proc.stdout:
                    if self._stop_event.is_set():
                        break
                    line = raw_line.strip()
                    if "member=Notify" in line and "interface=org.freedesktop.Notifications" in line:
                        in_notify = True
                        strings = []
                        array_depth = 0
                        hints_dict = {}
                        pending_hint_key = None
                        continue

                    if not in_notify:
                        continue

                    if line.startswith("array ["):
                        array_depth += 1
                        continue
                    if line == "]" and array_depth > 0:
                        array_depth -= 1
                        continue

                    if array_depth == 0 and line.startswith('string "'):
                        val = line[len('string "') :]
                        if val.endswith('"'):
                            val = val[:-1]
                        strings.append(val)
                        continue

                    if array_depth > 0:
                        if line.startswith('string "'):
                            key_cand = line[len('string "') :].rstrip('"')
                            if key_cand in ("image-path", "image_path", "desktop-entry", "app-icon"):
                                pending_hint_key = key_cand
                        elif pending_hint_key and 'string "' in line:
                            val = line.split('string "', 1)[1].rstrip('"')
                            hints_dict[pending_hint_key] = val
                            pending_hint_key = None

                    if len(strings) >= 4 and (line.startswith("int32 ") or array_depth == 0):
                        app_name = self.clean_text(strings[0]) or "Sistem"
                        raw_app_icon = strings[1] if len(strings) > 1 else ""
                        summary = self.clean_text(strings[2])
                        body = self.clean_text(strings[3])
                        final_icon = (
                            raw_app_icon
                            or hints_dict.get("image-path")
                            or hints_dict.get("image_path")
                            or hints_dict.get("desktop-entry")
                            or hints_dict.get("app-icon")
                            or ""
                        )
                        desktop_entry = hints_dict.get("desktop-entry") or ""
                        if summary or body:
                            GLib.idle_add(
                                self.on_notify_callback,
                                app_name,
                                summary or app_name,
                                body,
                                final_icon,
                                desktop_entry,
                            )
                        in_notify = False
            except Exception:
                time.sleep(2.0)


class AudioWatcher(threading.Thread):
    """Monitors PipeWire/PulseAudio sink & source events in real-time via pactl subscribe."""

    def __init__(self, callback):
        super().__init__(daemon=True)
        self.callback = callback
        self._stop = threading.Event()
        self._proc = None

    def stop(self):
        self._stop.set()
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass

    def run(self):
        prev_sink_muted = None
        prev_vol = None
        prev_src_muted = None

        def poll_audio(force=False):
            nonlocal prev_sink_muted, prev_vol, prev_src_muted
            try:
                s_mute = subprocess.check_output(
                    ["pactl", "get-sink-mute", "@DEFAULT_SINK@"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
                sink_muted = "yes" in s_mute.lower()

                s_vol = subprocess.check_output(
                    ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                )
                m = re.search(r"(\d+)%", s_vol)
                vol = int(m.group(1)) if m else 0

                src_mute = subprocess.check_output(
                    ["pactl", "get-source-mute", "@DEFAULT_SOURCE@"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
                src_muted = "yes" in src_mute.lower()

                if force:
                    prev_sink_muted = sink_muted
                    prev_vol = vol
                    prev_src_muted = src_muted
                    return

                if prev_sink_muted is not None and sink_muted != prev_sink_muted:
                    prev_sink_muted = sink_muted
                    GLib.idle_add(self.callback, "mute", vol, sink_muted)
                elif prev_vol is not None and abs(vol - prev_vol) >= 1:
                    prev_vol = vol
                    if not sink_muted:
                        GLib.idle_add(self.callback, "volume", vol, False)

                if prev_src_muted is not None and src_muted != prev_src_muted:
                    prev_src_muted = src_muted
                    GLib.idle_add(self.callback, "mic", 0, src_muted)

                prev_sink_muted = sink_muted
                prev_vol = vol
                prev_src_muted = src_muted
            except Exception:
                pass

        poll_audio(force=True)

        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(
                    ["pactl", "subscribe"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
                for line in self._proc.stdout:
                    if self._stop.is_set():
                        break
                    if "sink" in line or "source" in line:
                        poll_audio(force=False)
            except Exception:
                time.sleep(2.0)


class DBusOsdListener(threading.Thread):
    """Monitors KDE Plasma OSD D-Bus calls and signals for volume, mic, and brightness changes."""

    def __init__(self, callback):
        super().__init__(daemon=True)
        self.callback = callback
        self._stop = threading.Event()
        self._proc = None

    def stop(self):
        self._stop.set()
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass

    def run(self):
        cmd = [
            "dbus-monitor",
            "interface='org.kde.osdService'",
        ]
        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
                for raw in self._proc.stdout:
                    if self._stop.is_set():
                        break
                    line = raw.strip()
                    if (
                        "member=brightnessChanged" in line
                        or "member=screenBrightnessChanged" in line
                    ):
                        for nxt in self._proc.stdout:
                            nxt = nxt.strip()
                            if nxt.startswith("int32 "):
                                val = int(nxt.split()[1])
                                GLib.idle_add(self.callback, "brightness", val, False)
                                break
                    elif "member=volumeChanged" in line:
                        for nxt in self._proc.stdout:
                            nxt = nxt.strip()
                            if nxt.startswith("int32 "):
                                val = int(nxt.split()[1])
                                GLib.idle_add(self.callback, "volume", val, False)
                                break
                    elif "member=microphoneVolumeChanged" in line:
                        for nxt in self._proc.stdout:
                            nxt = nxt.strip()
                            if nxt.startswith("int32 "):
                                val = int(nxt.split()[1])
                                GLib.idle_add(self.callback, "mic", val, val == 0)
                                break
                    elif "member=showText" in line or "member=osdText" in line:
                        strings = []
                        for nxt in self._proc.stdout:
                            nxt = nxt.strip()
                            if nxt.startswith('string "'):
                                s = nxt[len('string "') : -1]
                                strings.append(s)
                                if len(strings) >= 2:
                                    break
                        if len(strings) >= 1:
                            icon = strings[0]
                            text = strings[1] if len(strings) > 1 else ""
                            if "muted" in icon or "bisu" in text.lower():
                                if "mic" in icon:
                                    GLib.idle_add(self.callback, "mic", 0, True)
                                else:
                                    GLib.idle_add(self.callback, "mute", 0, True)
                            elif "mic" in icon:
                                GLib.idle_add(self.callback, "mic", 0, False)
            except Exception:
                time.sleep(2.0)


class BacklightWatcher(threading.Thread):
    """Monitors display brightness directly from /sys/class/backlight."""

    def __init__(self, callback):
        super().__init__(daemon=True)
        self.callback = callback
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        prev_pct = None
        base = "/sys/class/backlight"
        device = None
        try:
            if os.path.isdir(base):
                entries = os.listdir(base)
                if entries:
                    device = os.path.join(base, sorted(entries)[0])
        except Exception:
            pass

        if not device:
            return

        b_file = os.path.join(device, "brightness")
        m_file = os.path.join(device, "max_brightness")

        while not self._stop.is_set():
            try:
                if os.path.isfile(b_file) and os.path.isfile(m_file):
                    with open(b_file, "r") as f:
                        cur = int(f.read().strip())
                    with open(m_file, "r") as f:
                        mx = int(f.read().strip())
                    pct = int(round(cur * 100.0 / mx)) if mx > 0 else 0
                    if prev_pct is not None and abs(pct - prev_pct) >= 1:
                        GLib.idle_add(self.callback, "brightness", pct, False)
                    prev_pct = pct
            except Exception:
                pass
            time.sleep(0.2)


class BluetoothWatcher:
    """Monitors BlueZ D-Bus signals for device connections & disconnections."""

    def __init__(self, on_event_callback):
        self.on_event_callback = on_event_callback
        self._bus = None
        self._sub_id = None
        self._dev_names: Dict[str, str] = {}

    def start(self):
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            if self._bus is not None:
                self._sub_id = self._bus.signal_subscribe(
                    "org.bluez",
                    "org.freedesktop.DBus.Properties",
                    "PropertiesChanged",
                    None,
                    None,
                    Gio.DBusSignalFlags.NONE,
                    self._on_prop_changed,
                    None,
                )
                self._prepopulate_devices()
        except Exception:
            pass

    def stop(self):
        if self._bus is not None and self._sub_id is not None:
            try:
                self._bus.signal_unsubscribe(self._sub_id)
            except Exception:
                pass
            self._sub_id = None

    def _prepopulate_devices(self):
        try:
            reply = self._bus.call_sync(
                "org.bluez",
                "/",
                "org.freedesktop.DBus.ObjectManager",
                "GetManagedObjects",
                None,
                GLib.VariantType("(a{oa{sa{sv}}})"),
                Gio.DBusCallFlags.NONE,
                800,
                None,
            )
            objs = reply.unpack()[0]
            for path, ifaces in objs.items():
                if "org.bluez.Device1" in ifaces:
                    d = ifaces["org.bluez.Device1"]
                    name = d.get("Alias") or d.get("Name")
                    if name:
                        self._dev_names[path] = str(name)
        except Exception:
            pass

    def _on_prop_changed(self, _conn, _sender, object_path, _iface, _signal, parameters, _data):
        try:
            target_iface, changed, _invalidated = parameters.unpack()
            if target_iface == "org.bluez.Device1":
                if "Alias" in changed:
                    self._dev_names[object_path] = str(changed["Alias"])
                elif "Name" in changed:
                    self._dev_names[object_path] = str(changed["Name"])

                if "Connected" in changed:
                    is_connected = bool(changed["Connected"])
                    name = self._dev_names.get(object_path)
                    if not name:
                        try:
                            res = self._bus.call_sync(
                                "org.bluez",
                                object_path,
                                "org.freedesktop.DBus.Properties",
                                "Get",
                                GLib.Variant("(ss)", ("org.bluez.Device1", "Alias")),
                                GLib.VariantType("(v)"),
                                Gio.DBusCallFlags.NONE,
                                500,
                                None,
                            )
                            name = str(res.unpack()[0])
                            self._dev_names[object_path] = name
                        except Exception:
                            name = "Perangkat Bluetooth"

                    GLib.idle_add(self.on_event_callback, name, is_connected)
        except Exception:
            pass


class TypingWatcher(threading.Thread):
    """Detects keyboard typing via /proc/interrupts (i8042 and xhci_hcd when mouse idle)."""

    def __init__(self, on_typing_callback, is_cursor_moving_func=None):
        super().__init__(daemon=True)
        self.on_typing_callback = on_typing_callback
        self.is_cursor_moving_func = is_cursor_moving_func
        self._stop_event = threading.Event()
        self._prev_i8042 = 0
        self._prev_xhci = 0

    def _read_irqs(self) -> Tuple[int, int]:
        i8042_cnt = 0
        xhci_cnt = 0
        try:
            with open("/proc/interrupts", "r") as f:
                for line in f:
                    if "i8042" in line:
                        parts = line.split()
                        i8042_cnt += sum(int(x) for x in parts[1:-2] if x.isdigit())
                    elif "xhci_hcd" in line:
                        parts = line.split()
                        xhci_cnt += sum(int(x) for x in parts[1:-2] if x.isdigit())
        except Exception:
            pass
        return i8042_cnt, xhci_cnt

    def run(self):
        self._prev_i8042, self._prev_xhci = self._read_irqs()
        while not self._stop_event.is_set():
            time.sleep(0.08)
            cur_i8042, cur_xhci = self._read_irqs()
            delta_i = cur_i8042 - self._prev_i8042
            delta_x = cur_xhci - self._prev_xhci
            self._prev_i8042, self._prev_xhci = cur_i8042, cur_xhci

            if delta_i > 0:
                GLib.idle_add(self.on_typing_callback)
            elif delta_x >= 10:
                if self.is_cursor_moving_func and not self.is_cursor_moving_func():
                    GLib.idle_add(self.on_typing_callback)

    def stop(self):
        self._stop_event.set()


class PuloCursorTracker:
    """Tracks global mouse cursor position and active fullscreen window state on Wayland via KWin scripting DBus bridge."""

    def __init__(self, callback, fullscreen_callback=None):
        self.callback = callback
        self.fullscreen_callback = fullscreen_callback
        self.script_path = None
        self.is_running = False
        self.bus = None
        self._init_service()

    def _init_service(self):
        try:
            import dbus
            import dbus.service
            import dbus.mainloop.glib

            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            self.bus = dbus.SessionBus()

            cb = self.callback
            fs_cb = self.fullscreen_callback

            class _CursorService(dbus.service.Object):
                def __init__(outer_self, b):
                    bus_name = dbus.service.BusName("org.pulo.Notch", b)
                    super().__init__(bus_name, "/org/pulo/Notch")

                @dbus.service.method("org.pulo.Notch", in_signature="ii")
                def UpdateCursor(outer_self, x, y):
                    if cb:
                        cb(int(x), int(y))

                @dbus.service.method("org.pulo.Notch", in_signature="i")
                def SetFullscreen(outer_self, is_fs):
                    if fs_cb:
                        GLib.idle_add(fs_cb, bool(is_fs))

            self.svc = _CursorService(self.bus)
            self._load_kwin_script()
            self.is_running = True
        except Exception:
            pass

    def _load_kwin_script(self):
        try:
            kwin_script = self.bus.get_object("org.kde.KWin", "/Scripting")
            try:
                kwin_script.unloadScript("pulo_cursor_service")
            except Exception:
                pass

            js_code = (
                "var lastT = 0;\n"
                "var lastFs = -1;\n"
                "workspace.cursorPosChanged.connect(function() {\n"
                "    var now = Date.now();\n"
                "    if (now - lastT >= 35) {\n"
                "        lastT = now;\n"
                "        callDBus('org.pulo.Notch', '/org/pulo/Notch', 'org.pulo.Notch', 'UpdateCursor', "
                "Math.round(workspace.cursorPos.x), Math.round(workspace.cursorPos.y));\n"
                "    }\n"
                "});\n"
                "function checkFS() {\n"
                "    var isFs = 0;\n"
                "    try {\n"
                "        var act = (typeof workspace.activeWindow !== 'undefined') ? workspace.activeWindow : workspace.activeClient;\n"
                "        if (act && act.fullScreen && !act.minimized) {\n"
                "            isFs = 1;\n"
                "        } else {\n"
                "            var wins = (typeof workspace.windowList === 'function') ? workspace.windowList() : (typeof workspace.clientList === 'function' ? workspace.clientList() : []);\n"
                "            for (var i = 0; i < wins.length; i++) {\n"
                "                var w = wins[i];\n"
                "                if (w && w.fullScreen && !w.minimized && w.onCurrentDesktop) {\n"
                "                    isFs = 1;\n"
                "                    break;\n"
                "                }\n"
                "            }\n"
                "        }\n"
                "    } catch (e) {}\n"
                "    if (isFs !== lastFs) {\n"
                "        lastFs = isFs;\n"
                "        callDBus('org.pulo.Notch', '/org/pulo/Notch', 'org.pulo.Notch', 'SetFullscreen', isFs);\n"
                "    }\n"
                "}\n"
                "function hookWin(w) {\n"
                "    if (!w || w._puloFsHooked) return;\n"
                "    w._puloFsHooked = true;\n"
                "    try { if (w.fullScreenChanged) w.fullScreenChanged.connect(checkFS); } catch (e) {}\n"
                "    try { if (w.minimizedChanged) w.minimizedChanged.connect(checkFS); } catch (e) {}\n"
                "    try { if (w.desktopChanged) w.desktopChanged.connect(checkFS); } catch (e) {}\n"
                "}\n"
                "try { workspace.windowActivated.connect(function(w) { hookWin(w); checkFS(); }); } catch (e) {}\n"
                "try { workspace.clientActivated.connect(function(w) { hookWin(w); checkFS(); }); } catch (e) {}\n"
                "try { workspace.windowAdded.connect(function(w) { hookWin(w); checkFS(); }); } catch (e) {}\n"
                "try { workspace.windowRemoved.connect(function() { checkFS(); }); } catch (e) {}\n"
                "try { workspace.currentDesktopChanged.connect(function() { checkFS(); }); } catch (e) {}\n"
                "try {\n"
                "    var initWins = (typeof workspace.windowList === 'function') ? workspace.windowList() : (typeof workspace.clientList === 'function' ? workspace.clientList() : []);\n"
                "    for (var j = 0; j < initWins.length; j++) hookWin(initWins[j]);\n"
                "} catch (e) {}\n"
                "checkFS();\n"
                "callDBus('org.pulo.Notch', '/org/pulo/Notch', 'org.pulo.Notch', 'UpdateCursor', "
                "Math.round(workspace.cursorPos.x), Math.round(workspace.cursorPos.y));\n"
            )
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(js_code)
                self.script_path = f.name

            script_id = kwin_script.loadScript(self.script_path, "pulo_cursor_service")
            obj = self.bus.get_object("org.kde.KWin", f"/Scripting/Script{script_id}")
            obj.run()
        except Exception:
            pass

    def stop(self):
        self.is_running = False
        try:
            if self.bus:
                kwin_script = self.bus.get_object("org.kde.KWin", "/Scripting")
                kwin_script.unloadScript("pulo_cursor_service")
        except Exception:
            pass
        if self.script_path and os.path.exists(self.script_path):
            try:
                os.remove(self.script_path)
            except Exception:
                pass


class MprisWatcher:
    """Tracks active MPRIS media players and playback status via D-Bus."""

    def __init__(self, callback):
        self.callback = callback
        self.bus: Optional[Gio.DBusConnection] = None
        self.active_player: Optional[str] = None
        self.playback_status: str = "Stopped"
        self.title: str = ""
        self.artist: str = ""
        self.album: str = ""
        self.art_url: str = ""
        self.url: str = ""
        self.track_id: str = ""
        self.length_us: int = 0
        self.position_us: int = 0
        self.last_pos_update: float = 0.0
        self.player_name: str = ""
        self._cover_cache: Dict[str, Optional[QImage]] = {}

        # Synced and plain lyrics engine
        self.lyrics_synced: List[Tuple[float, str]] = []
        self.lyrics_plain: List[str] = []
        self.lyrics_loading: bool = False
        self._lyrics_cache: Dict[str, Tuple[List[Tuple[float, str]], List[str]]] = {}
        self._last_lyric_query: str = ""

    @property
    def is_playing(self) -> bool:
        return self.playback_status == "Playing"

    @property
    def effective_length_us(self) -> int:
        if self.length_us > 0:
            return self.length_us
        if self.lyrics_synced:
            # Estimate song length from final synced lyric timestamp + 15 seconds
            last_sec = self.lyrics_synced[-1][0]
            return int((last_sec + 15.0) * 1_000_000)
        return 0

    @property
    def progress_ratio(self) -> float:
        tot_us = self.effective_length_us
        cur_sec = self.current_position_sec
        if tot_us <= 0:
            if cur_sec <= 0:
                return 0.0
            return max(0.0, min(1.0, cur_sec / 210.0))
        tot_sec = tot_us / 1_000_000.0
        return max(0.0, min(1.0, cur_sec / float(tot_sec)))

    @property
    def current_position_sec(self) -> float:
        cur_sec = self.position_us / 1_000_000.0
        if self.is_playing and self.last_pos_update > 0:
            cur_sec += time.time() - self.last_pos_update
        tot_us = self.effective_length_us
        if tot_us > 0:
            tot_sec = tot_us / 1_000_000.0
            cur_sec = max(0.0, min(tot_sec, cur_sec))
        return max(0.0, cur_sec)

    @staticmethod
    def _format_time_sec(sec: int, force_hours: bool = False) -> str:
        sec = max(0, int(sec))
        hrs = sec // 3600
        mins = (sec % 3600) // 60
        secs = sec % 60
        if hrs > 0 or force_hours:
            return f"{hrs}:{mins:02d}:{secs:02d}"
        return f"{mins:02d}:{secs:02d}"

    @property
    def time_strings(self) -> Tuple[str, str]:
        cur_sec = int(self.current_position_sec)
        tot_us = self.effective_length_us
        if tot_us <= 0:
            s_cur = self._format_time_sec(cur_sec, force_hours=False)
            return (s_cur, "--:--")
        tot_sec = int(tot_us / 1_000_000)
        cur_sec = max(0, min(tot_sec, cur_sec))
        has_hrs = tot_sec >= 3600
        s_cur = self._format_time_sec(cur_sec, force_hours=has_hrs)
        s_tot = self._format_time_sec(tot_sec, force_hours=has_hrs)
        return (s_cur, s_tot)

    @property
    def remaining_time_str(self) -> str:
        cur_sec = int(self.current_position_sec)
        tot_us = self.effective_length_us
        if tot_us <= 0:
            return "--:--"
        tot_sec = int(tot_us / 1_000_000)
        cur_sec = max(0, min(tot_sec, cur_sec))
        rem_sec = max(0, tot_sec - cur_sec)
        has_hrs = tot_sec >= 3600
        return f"-{self._format_time_sec(rem_sec, force_hours=has_hrs)}"

    def get_current_lyric(self) -> Tuple[str, str, int]:
        """Returns (current_line, next_line, current_index)."""
        if not self.lyrics_synced:
            if self.lyrics_plain:
                return (self.lyrics_plain[0] if self.lyrics_plain else "", "", 0)
            return ("", "", -1)

        cur_sec = self.current_position_sec
        idx = -1
        for i, (t, txt) in enumerate(self.lyrics_synced):
            if t <= cur_sec:
                idx = i
            else:
                break

        cur_txt = ""
        next_txt = ""
        if idx >= 0 and idx < len(self.lyrics_synced):
            cur_txt = self.lyrics_synced[idx][1]
            for j in range(idx + 1, len(self.lyrics_synced)):
                if self.lyrics_synced[j][1]:
                    next_txt = self.lyrics_synced[j][1]
                    break
            if not cur_txt:
                cur_txt = "♫ Musik..."
        elif self.lyrics_synced:
            cur_txt = "♫ Musik pengantar..."
            next_txt = self.lyrics_synced[0][1]

        return (cur_txt, next_txt, idx)

    @property
    def clean_title(self) -> str:
        return self._clean_track_title(self.title)

    @property
    def clean_artist(self) -> str:
        return self._clean_artist_name(self.artist)

    @staticmethod
    def _clean_track_title(title: str) -> str:
        cleaned = re.sub(
            r"[\(\[\{].*?(?:official|video|audio|lyrics|lirik|mv|hd|4k|ft\.|feat\.|remix|clip).*?[\)\]\}]",
            "",
            title,
            flags=re.IGNORECASE,
        ).strip()
        cleaned = re.split(r"\s+[|/]\s+", cleaned)[0].strip()
        cleaned = re.sub(r"\s+[-–—]\s*$", "", cleaned).strip()
        return cleaned.strip("\"'") or title

    @staticmethod
    def _clean_artist_name(artist: str) -> str:
        a = artist.strip()
        a = re.sub(r"(?i)\bvevo\b", "", a).strip()
        a = re.sub(r"(?i)\s*-\s*topic$", "", a).strip()
        a = re.sub(r"(?i)\b(official|channel|tv|records|entertainment)\b", "", a).strip()
        if a.lower().endswith("music") and len(a) > 8:
            a = a[:-5].strip()
        return a

    def _build_lyric_candidates(self) -> List[str]:
        clean_t = self.clean_title
        clean_a = self.clean_artist
        candidates = []
        for sep in [" - ", " – ", " — "]:
            if sep in clean_t:
                parts = clean_t.split(sep, 1)
                p1, p2 = parts[0].strip(), parts[1].strip()
                candidates.append(f"{p1} {p2}")
                candidates.append(clean_t)
                candidates.append(p2)
                if clean_a:
                    candidates.append(f"{clean_a} {p2}")
                break
        if clean_a and clean_t:
            candidates.append(f"{clean_a} {clean_t}")
        if clean_t:
            candidates.append(clean_t)

        seen = set()
        uniq = []
        for c in candidates:
            k = c.lower().strip()
            if k and k not in seen:
                seen.add(k)
                uniq.append(c.strip())
        return uniq

    def fetch_lyrics(self, force: bool = False):
        if not self.title:
            self.lyrics_synced = []
            self.lyrics_plain = []
            self.lyrics_loading = False
            return

        candidates = self._build_lyric_candidates()
        cache_key = f"{self.artist.strip()}||{self.title.strip()}"
        if not candidates:
            return

        if not force and cache_key == self._last_lyric_query:
            return

        self._last_lyric_query = cache_key

        if cache_key in self._lyrics_cache:
            cached_synced, cached_plain = self._lyrics_cache[cache_key]
            self.lyrics_synced = cached_synced
            self.lyrics_plain = cached_plain
            self.lyrics_loading = False
            if self.callback:
                GLib.idle_add(self.callback)
            return

        self.lyrics_loading = True
        self.lyrics_synced = []
        self.lyrics_plain = []
        if self.callback:
            GLib.idle_add(self.callback)

        def _worker():
            synced = []
            plain = []
            found_len_us = 0
            for cand in candidates:
                try:
                    url = f"https://lrclib.net/api/search?q={urllib.parse.quote(cand)}"
                    req = urllib.request.Request(url, headers={"User-Agent": "Pulo-Notch/1.0"})
                    with urllib.request.urlopen(req, timeout=3.5) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        if isinstance(data, list) and data:
                            best_item = None
                            for it in data:
                                if it.get("syncedLyrics"):
                                    best_item = it
                                    break
                            if best_item is None:
                                best_item = data[0]

                            dur = best_item.get("duration")
                            if dur and found_len_us <= 0:
                                try:
                                    found_len_us = int(float(dur) * 1_000_000)
                                except Exception:
                                    pass

                            raw_synced = best_item.get("syncedLyrics")
                            raw_plain = best_item.get("plainLyrics")
                            if raw_synced:
                                for line in raw_synced.splitlines():
                                    line_s = line.strip()
                                    if not line_s:
                                        continue
                                    m = re.match(r"^\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)$", line_s)
                                    if m:
                                        t = int(m.group(1)) * 60 + float(m.group(2))
                                        txt = m.group(3).strip()
                                        synced.append((t, txt))
                            if raw_plain:
                                plain = [l.strip() for l in raw_plain.splitlines() if l.strip()]

                            if synced:
                                break
                except Exception:
                    pass

            # Fallback duration query for YouTube videos if duration still unknown
            if found_len_us <= 0 and self.length_us <= 0 and self.url:
                if "youtube.com" in self.url or "youtu.be" in self.url:
                    try:
                        yt_req = urllib.request.Request(
                            self.url,
                            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
                        )
                        with urllib.request.urlopen(yt_req, timeout=3.0) as yt_resp:
                            html = yt_resp.read().decode("utf-8", errors="ignore")
                            m = re.search(r'\"approxDurationMs\":\"(\d+)\"', html)
                            if m:
                                found_len_us = int(m.group(1)) * 1000
                            else:
                                m2 = re.search(r'\"lengthSeconds\":\"(\d+)\"', html)
                                if m2:
                                    found_len_us = int(m2.group(1)) * 1_000_000
                    except Exception:
                        pass

            self._lyrics_cache[cache_key] = (synced, plain)
            if cache_key == self._last_lyric_query:
                self.lyrics_synced = synced
                self.lyrics_plain = plain
                self.lyrics_loading = False
                if found_len_us > 0 and self.length_us <= 0:
                    self.length_us = found_len_us
                elif self.length_us <= 0 and synced:
                    self.length_us = int((synced[-1][0] + 15.0) * 1_000_000)
                if self.callback:
                    GLib.idle_add(self.callback)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def start(self):
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            if self.bus is not None:
                self.bus.signal_subscribe(
                    None,
                    "org.freedesktop.DBus.Properties",
                    "PropertiesChanged",
                    "/org/mpris/MediaPlayer2",
                    None,
                    Gio.DBusSignalFlags.NONE,
                    self._on_prop_changed,
                    None,
                )
                self.bus.signal_subscribe(
                    "org.freedesktop.DBus",
                    "org.freedesktop.DBus",
                    "NameOwnerChanged",
                    "/org/freedesktop/DBus",
                    None,
                    Gio.DBusSignalFlags.NONE,
                    self._on_name_owner_changed,
                    None,
                )
                self.bus.signal_subscribe(
                    None,
                    "org.mpris.MediaPlayer2.Player",
                    "Seeked",
                    "/org/mpris/MediaPlayer2",
                    None,
                    Gio.DBusSignalFlags.NONE,
                    self._on_seeked,
                    None,
                )
                self._find_player()
        except Exception:
            pass

    def stop(self):
        pass

    def _find_player(self):
        if not self.bus:
            return
        try:
            reply = self.bus.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "ListNames",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                800,
                None,
            )
            names = reply.unpack()[0]
            players = [n for n in names if n.startswith("org.mpris.MediaPlayer2.")]
            if players:
                spot = [p for p in players if "spotify" in p.lower()]
                self.active_player = spot[0] if spot else players[0]
                self._update_all()
            else:
                self.active_player = None
                self.playback_status = "Stopped"
                self.title = ""
                self.artist = ""
                self.player_name = ""
                self.lyrics_synced = []
                self.lyrics_plain = []
                self.lyrics_loading = False
        except Exception:
            pass

    def query_position(self) -> int:
        if not self.active_player or not self.bus:
            return self.position_us
        try:
            res = self.bus.call_sync(
                self.active_player,
                "/org/mpris/MediaPlayer2",
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", ("org.mpris.MediaPlayer2.Player", "Position")),
                GLib.VariantType("(v)"),
                Gio.DBusCallFlags.NONE,
                150,
                None,
            )
            val = int(res.unpack()[0])
            if val > 0:
                self.position_us = val
                self.last_pos_update = time.time()
            elif val == 0 and not self.is_playing:
                self.position_us = 0
                self.last_pos_update = time.time()
            return self.position_us
        except Exception:
            return self.position_us

    def seek_to_ratio(self, ratio: float):
        if not self.active_player or not self.bus:
            return
        ratio = max(0.0, min(1.0, float(ratio)))
        tot_us = self.effective_length_us
        if tot_us <= 0:
            return
        target_pos_us = int(ratio * tot_us)

        # 1. Try SetPosition(track_id, target_pos_us)
        if self.track_id:
            try:
                self.bus.call_sync(
                    self.active_player,
                    "/org/mpris/MediaPlayer2",
                    "org.mpris.MediaPlayer2.Player",
                    "SetPosition",
                    GLib.Variant("(ox)", (self.track_id, target_pos_us)),
                    None,
                    Gio.DBusCallFlags.NONE,
                    300,
                    None,
                )
                self.position_us = target_pos_us
                self.last_pos_update = time.time()
                if self.callback:
                    GLib.idle_add(self.callback)
                return
            except Exception:
                pass

        # 2. Fallback to Seek(offset_us)
        try:
            cur_us = int(self.current_position_sec * 1_000_000)
            offset_us = int(target_pos_us - cur_us)
            self.bus.call_sync(
                self.active_player,
                "/org/mpris/MediaPlayer2",
                "org.mpris.MediaPlayer2.Player",
                "Seek",
                GLib.Variant("(x)", (offset_us,)),
                None,
                Gio.DBusCallFlags.NONE,
                300,
                None,
            )
            self.position_us = target_pos_us
            self.last_pos_update = time.time()
            if self.callback:
                GLib.idle_add(self.callback)
        except Exception:
            pass

    def _update_all(self):
        if not self.active_player or not self.bus:
            return
        try:
            res = self.bus.call_sync(
                self.active_player,
                "/org/mpris/MediaPlayer2",
                "org.freedesktop.DBus.Properties",
                "GetAll",
                GLib.Variant("(s)", ("org.mpris.MediaPlayer2.Player",)),
                GLib.VariantType("(a{sv})"),
                Gio.DBusCallFlags.NONE,
                800,
                None,
            )
            props = res.unpack()[0]
            new_status = str(props.get("PlaybackStatus", "Stopped"))
            old_status = self.playback_status
            self.playback_status = new_status
            if new_status == "Playing" and old_status != "Playing":
                self.last_pos_update = time.time()
            elif new_status != "Playing" and old_status == "Playing":
                cur_sec = self.current_position_sec
                self.position_us = int(cur_sec * 1_000_000)
                self.last_pos_update = 0.0

            meta = props.get("Metadata", {})
            old_title = self.title
            new_title = str(meta.get("xesam:title", ""))
            self.title = new_title
            self.track_id = str(meta.get("mpris:trackid", ""))
            artists = meta.get("xesam:artist", [])
            self.artist = ", ".join(artists) if isinstance(artists, (list, tuple)) else str(artists)
            self.album = str(meta.get("xesam:album", ""))
            self.art_url = str(meta.get("mpris:artUrl", ""))
            self.url = str(meta.get("xesam:url", ""))
            raw_l = meta.get("mpris:length")
            if raw_l is not None:
                try:
                    self.length_us = int(raw_l)
                except Exception:
                    pass

            if new_title != old_title:
                self.position_us = 0
                self.last_pos_update = time.time() if self.is_playing else 0.0

            raw_pos = int(props.get("Position", 0))
            if raw_pos > 0:
                self.position_us = raw_pos
                self.last_pos_update = time.time()
            elif self.is_playing and self.last_pos_update <= 0:
                self.last_pos_update = time.time()

            self.query_position()
            raw = self.active_player.replace("org.mpris.MediaPlayer2.", "")
            self.player_name = raw.split(".")[0].capitalize()
            if self.title and (self.title != old_title or not self.lyrics_synced):
                self.fetch_lyrics()
        except Exception:
            pass

    def _on_prop_changed(self, _conn, sender, _path, _iface, _signal, params, _data):
        try:
            target_iface, changed, _ = params.unpack()
            if target_iface == "org.mpris.MediaPlayer2.Player":
                self.active_player = sender
                if "PlaybackStatus" in changed:
                    old_st = self.playback_status
                    new_st = str(changed["PlaybackStatus"])
                    self.playback_status = new_st
                    if new_st == "Playing" and old_st != "Playing":
                        self.last_pos_update = time.time()
                    elif new_st != "Playing" and old_st == "Playing":
                        cur_sec = self.current_position_sec
                        self.position_us = int(cur_sec * 1_000_000)
                        self.last_pos_update = 0.0
                    self.query_position()

                if "Metadata" in changed:
                    meta = changed["Metadata"]
                    old_title = self.title
                    new_title = str(meta.get("xesam:title", ""))
                    self.title = new_title
                    self.track_id = str(meta.get("mpris:trackid", ""))
                    artists = meta.get("xesam:artist", [])
                    self.artist = (
                        ", ".join(artists) if isinstance(artists, (list, tuple)) else str(artists)
                    )
                    self.album = str(meta.get("xesam:album", ""))
                    self.art_url = str(meta.get("mpris:artUrl", ""))
                    self.url = str(meta.get("xesam:url", ""))
                    raw_l = meta.get("mpris:length")
                    if raw_l is not None:
                        try:
                            self.length_us = int(raw_l)
                        except Exception:
                            pass
                    if new_title != old_title:
                        self.position_us = 0
                        self.last_pos_update = time.time() if self.is_playing else 0.0
                    self.query_position()
                    if self.title and (self.title != old_title or not self.lyrics_synced):
                        self.fetch_lyrics()

                if "Position" in changed:
                    self.position_us = int(changed["Position"])
                    self.last_pos_update = time.time()
                raw = sender.replace("org.mpris.MediaPlayer2.", "")
                self.player_name = raw.split(".")[0].capitalize()
                if self.callback:
                    GLib.idle_add(self.callback)
        except Exception:
            pass

    def _on_seeked(self, _conn, _sender, _path, _iface, _signal, params, _data):
        try:
            val = params.unpack()[0]
            self.position_us = int(val)
            self.last_pos_update = time.time()
            if self.callback:
                GLib.idle_add(self.callback)
        except Exception:
            pass

    def _on_name_owner_changed(self, _conn, _sender, _path, _iface, _signal, params, _data):
        try:
            name, _old_owner, new_owner = params.unpack()
            if name.startswith("org.mpris.MediaPlayer2."):
                if new_owner:
                    self.active_player = name
                    self._update_all()
                elif name == self.active_player:
                    self._find_player()
                if self.callback:
                    GLib.idle_add(self.callback)
        except Exception:
            pass

    def play_pause(self):
        if self.active_player and self.bus:
            try:
                # Optimistic toggle so UI updates immediately
                if self.is_playing:
                    cur_sec = self.current_position_sec
                    self.position_us = int(cur_sec * 1_000_000)
                    self.last_pos_update = 0.0
                    self.playback_status = "Paused"
                else:
                    self.last_pos_update = time.time()
                    self.playback_status = "Playing"

                self.bus.call_sync(
                    self.active_player,
                    "/org/mpris/MediaPlayer2",
                    "org.mpris.MediaPlayer2.Player",
                    "PlayPause",
                    None,
                    None,
                    Gio.DBusCallFlags.NONE,
                    500,
                    None,
                )
            except Exception:
                pass

    def next(self):
        if self.active_player and self.bus:
            try:
                self.position_us = 0
                self.last_pos_update = time.time() if self.is_playing else 0.0
                self.bus.call_sync(
                    self.active_player,
                    "/org/mpris/MediaPlayer2",
                    "org.mpris.MediaPlayer2.Player",
                    "Next",
                    None,
                    None,
                    Gio.DBusCallFlags.NONE,
                    500,
                    None,
                )
            except Exception:
                pass

    def previous(self):
        if self.active_player and self.bus:
            try:
                self.position_us = 0
                self.last_pos_update = time.time() if self.is_playing else 0.0
                self.bus.call_sync(
                    self.active_player,
                    "/org/mpris/MediaPlayer2",
                    "org.mpris.MediaPlayer2.Player",
                    "Previous",
                    None,
                    None,
                    Gio.DBusCallFlags.NONE,
                    500,
                    None,
                )
            except Exception:
                pass

    def get_cover_image(self) -> Optional[QImage]:
        if not self.art_url:
            return None

        if self.art_url in self._cover_cache:
            return self._cover_cache[self.art_url]
        path = self.art_url
        if path.startswith("file://"):
            path = path[7:]
        if os.path.isfile(path):
            try:
                img = QImage(path)
                if not img.isNull():
                    if img.width() > 160 or img.height() > 160:
                        img = img.scaled(160, 160, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                    if len(self._cover_cache) > 8:
                        self._cover_cache.clear()
                    self._cover_cache[self.art_url] = img
                    return img
            except Exception:
                pass
        if len(self._cover_cache) > 8:
            self._cover_cache.clear()
        self._cover_cache[self.art_url] = None
        return None



class AudioSpectrumAnalyzer:
    """Real-time 4-band audio spectrum analyzer using parec @DEFAULT_MONITOR@ + Goertzel DFT."""

    SAMPLE_RATE = 16000
    CHUNK_SAMPLES = 256
    CHUNK_BYTES = CHUNK_SAMPLES * 2  # s16le = 2 bytes/sample
    BIN_FREQS = (90.0, 180.0, 380.0, 700.0, 1300.0, 2200.0, 3800.0, 5600.0)

    def __init__(self):
        self.bands: List[float] = [0.0, 0.0, 0.0, 0.0]
        self._coeffs = [2.0 * math.cos(2.0 * math.pi * f / self.SAMPLE_RATE) for f in self.BIN_FREQS]
        self._active = False
        self._stop_event = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._has_parec = shutil.which("parec") is not None

    def set_active(self, active: bool):
        if not self._has_parec:
            return
        if active == self._active:
            return
        self._active = active
        if active:
            self._start()
        else:
            self._stop_stream()

    def stop(self):
        self._active = False
        self._stop_stream()

    def _start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()

    def _stop_stream(self):
        self._stop_event.set()
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        self.bands = [0.0, 0.0, 0.0, 0.0]

    def _worker(self):
        cmd = [
            "parec",
            "--client-name=PuloSpectrum",
            "--raw",
            f"--rate={self.SAMPLE_RATE}",
            "--channels=1",
            "--format=s16le",
            "--latency-msec=20",
            "-d",
            "@DEFAULT_MONITOR@",
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=self.CHUNK_BYTES,
            )
            with self._lock:
                if self._stop_event.is_set():
                    proc.terminate()
                    return
                self._proc = proc
        except Exception:
            return

        running_peak = 0.03
        smooth = [0.0, 0.0, 0.0, 0.0]
        coeffs = self._coeffs
        n_norm = float(self.CHUNK_SAMPLES) * 16384.0
        band_weights = (0.95, 1.15, 1.35, 1.65)  # Compensate natural music spectral roll-off

        try:
            stdout = proc.stdout
            while not self._stop_event.is_set() and stdout is not None:
                data = stdout.read(self.CHUNK_BYTES)
                if not data or len(data) < self.CHUNK_BYTES:
                    break
                samples = array.array("h")
                samples.frombytes(data)

                bins = []
                for c in coeffs:
                    s1 = 0.0
                    s2 = 0.0
                    for x in samples:
                        s0 = x + c * s1 - s2
                        s2 = s1
                        s1 = s0
                    pwr = s2 * s2 + s1 * s1 - c * s1 * s2
                    bins.append(math.sqrt(max(0.0, pwr)) / n_norm)

                raw_bands = [
                    max(bins[0], bins[1]) * band_weights[0],
                    max(bins[2], bins[3]) * band_weights[1],
                    max(bins[4], bins[5]) * band_weights[2],
                    max(bins[6], bins[7]) * band_weights[3],
                ]
                cur_max = max(raw_bands)
                running_peak = max(0.015, running_peak * 0.992, cur_max)

                for i in range(4):
                    norm = min(1.0, raw_bands[i] / running_peak)
                    shaped = norm ** 0.72
                    alpha = 0.68 if shaped > smooth[i] else 0.26
                    smooth[i] += (shaped - smooth[i]) * alpha

                self.bands = list(smooth)
        except Exception:
            pass
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=0.3)
            except Exception:
                pass
            with self._lock:
                if self._proc is proc:
                    self._proc = None


class PrivacyMonitor(threading.Thread):
    """Detects active Webcam (/dev/video*) and Microphone (PipeWire/PulseAudio) usage."""

    APP_NAME_MAP = {
        "chrome": "Chrome",
        "chromium": "Chromium",
        "brave": "Brave",
        "firefox": "Firefox",
        "firefox-bin": "Firefox",
        "obs": "OBS Studio",
        "zoom": "Zoom",
        "discord": "Discord",
        "teams": "Teams",
        "slack": "Slack",
        "telegram-deskto": "Telegram",
        "skypeforlinux": "Skype",
        "cheese": "Cheese",
        "guvcview": "Guvcview",
        "vlc": "VLC",
        "electron": "Electron App",
        "wireplumber": "Portal Kamera",
        "pipewire": "Portal Kamera",
    }

    def __init__(self, callback=None):
        super().__init__(daemon=True)
        self.callback = callback
        self.cam_active: bool = False
        self.mic_active: bool = False
        self.cam_apps: List[str] = []
        self.mic_apps: List[str] = []
        self._stop_event = threading.Event()
        self._uid = os.getuid()
        self._has_pactl = shutil.which("pactl") is not None

    def stop(self):
        self._stop_event.set()

    def _friendly_name(self, raw: str) -> str:
        low = raw.strip().lower()
        if low in self.APP_NAME_MAP:
            return self.APP_NAME_MAP[low]
        if raw.strip():
            return raw.strip().capitalize() if low == raw.strip() else raw.strip()
        return "Aplikasi"

    def _check_camera(self) -> List[str]:
        if not glob.glob("/dev/video*"):
            return []
        apps: List[str] = []
        seen = set()
        try:
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                pdir = f"/proc/{pid}"
                try:
                    if os.stat(pdir).st_uid != self._uid:
                        continue
                    fddir = f"{pdir}/fd"
                    for fd in os.listdir(fddir):
                        try:
                            tgt = os.readlink(f"{fddir}/{fd}")
                            if tgt.startswith("/dev/video"):
                                with open(f"{pdir}/comm", "r", encoding="utf-8", errors="ignore") as f:
                                    comm = f.read().strip()
                                name = self._friendly_name(comm)
                                if name not in seen:
                                    seen.add(name)
                                    apps.append(name)
                                break
                        except OSError:
                            pass
                except OSError:
                    pass
        except Exception:
            pass
        return apps

    def _check_mic(self) -> List[str]:
        if not self._has_pactl:
            return []
        monitor_ids = set()
        try:
            out_src = subprocess.check_output(
                ["pactl", "list", "sources", "short"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.5,
            )
            for line in out_src.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2 and ".monitor" in parts[1]:
                    monitor_ids.add(parts[0].strip())
        except Exception:
            pass

        apps: List[str] = []
        seen = set()
        try:
            out_so = subprocess.check_output(
                ["pactl", "list", "source-outputs"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.5,
            )
            cur_source = ""
            cur_corked = False
            cur_app = ""
            cur_bin = ""
            cur_media = ""

            def _flush():
                if not cur_source or cur_corked:
                    return
                if cur_source in monitor_ids:
                    return
                if cur_app in ("PuloSpectrum", "pulau", "pavucontrol") or cur_media == "Peak detect":
                    return
                label = self._friendly_name(cur_app or cur_bin or "Mikrofon")
                if label not in seen:
                    seen.add(label)
                    apps.append(label)

            for line in out_so.splitlines():
                s = line.strip()
                if s.startswith("Source Output #"):
                    _flush()
                    cur_source = ""
                    cur_corked = False
                    cur_app = ""
                    cur_bin = ""
                    cur_media = ""
                elif s.startswith("Source:"):
                    cur_source = s.split(":", 1)[1].strip()
                elif s.startswith("Corked:"):
                    cur_corked = ("yes" in s.lower())
                elif s.startswith("application.name ="):
                    cur_app = s.split("=", 1)[1].strip().strip('"')
                elif s.startswith("application.process.binary ="):
                    cur_bin = s.split("=", 1)[1].strip().strip('"')
                elif s.startswith("media.name ="):
                    cur_media = s.split("=", 1)[1].strip().strip('"')
            _flush()
        except Exception:
            pass
        return apps

    def run(self):
        while not self._stop_event.is_set():
            c_apps = self._check_camera()
            m_apps = self._check_mic()
            c_act = len(c_apps) > 0
            m_act = len(m_apps) > 0
            if (
                c_act != self.cam_active
                or m_act != self.mic_active
                or c_apps != self.cam_apps
                or m_apps != self.mic_apps
            ):
                self.cam_active = c_act
                self.mic_active = m_act
                self.cam_apps = c_apps
                self.mic_apps = m_apps
                if self.callback:
                    GLib.idle_add(self.callback)
            self._stop_event.wait(1.5)


# =============================================================================
# Prayer Times Manager (Jadwal Sholat & Kemenag / Astronomical Engine)
# =============================================================================
@dataclass
class PrayerAlert:
    name: str
    time_str: str
    minutes_left: int
    is_adzan: bool

    @property
    def prayer_name(self) -> str:
        return self.name


class PrayerManager:
    """Manages Indonesian prayer times (Jadwal Sholat), Kemenag online API with offline solar calculations."""

    CITIES = [
        {"name": "Banyumas", "id": "1402", "lat": -7.5167, "lon": 109.2833, "tz": 7},
        {"name": "Jakarta", "id": "1301", "lat": -6.2088, "lon": 106.8456, "tz": 7},
        {"name": "Bandung", "id": "1204", "lat": -6.9175, "lon": 107.6191, "tz": 7},
        {"name": "Surabaya", "id": "1638", "lat": -7.2575, "lon": 112.7521, "tz": 7},
        {"name": "Semarang", "id": "1433", "lat": -6.9667, "lon": 110.4167, "tz": 7},
        {"name": "Yogyakarta", "id": "1405", "lat": -7.7956, "lon": 110.3695, "tz": 7},
        {"name": "Medan", "id": "0228", "lat": 3.5952, "lon": 98.6722, "tz": 7},
        {"name": "Palembang", "id": "0409", "lat": -2.9761, "lon": 104.7754, "tz": 7},
        {"name": "Makassar", "id": "2306", "lat": -5.1477, "lon": 119.4327, "tz": 8},
        {"name": "Denpasar", "id": "1701", "lat": -8.6705, "lon": 115.2126, "tz": 8},
        {"name": "Balikpapan", "id": "2002", "lat": -1.2379, "lon": 116.8529, "tz": 8},
        {"name": "Banjarmasin", "id": "1902", "lat": -3.3167, "lon": 114.5900, "tz": 8},
        {"name": "Banda Aceh", "id": "0101", "lat": 5.5483, "lon": 95.3238, "tz": 7},
        {"name": "Padang", "id": "0301", "lat": -0.9492, "lon": 100.3543, "tz": 7},
        {"name": "Pekanbaru", "id": "0401", "lat": 0.5071, "lon": 101.4478, "tz": 7},
        {"name": "Samarinda", "id": "2001", "lat": -0.5022, "lon": 117.1536, "tz": 8},
        {"name": "Pontianak", "id": "1801", "lat": -0.0263, "lon": 109.3425, "tz": 7},
        {"name": "Manado", "id": "2101", "lat": 1.4748, "lon": 124.8421, "tz": 8},
        {"name": "Mataram", "id": "1708", "lat": -8.5833, "lon": 116.1167, "tz": 8},
        {"name": "Kupang", "id": "1710", "lat": -10.1772, "lon": 123.6070, "tz": 8},
        {"name": "Ambon", "id": "2401", "lat": -3.6547, "lon": 128.1906, "tz": 9},
        {"name": "Jayapura", "id": "2601", "lat": -2.5916, "lon": 140.6690, "tz": 9},
    ]

    def __init__(self, callback=None, on_city_change=None):
        self.callback = callback
        self.on_city_change = on_city_change
        self.city_idx = 0
        self.adzan_vibration_sec = 10
        self.today_times: Dict[str, Tuple[str, int]] = {}
        self.simulated_alert: Optional[PrayerAlert] = None
        self._load_config()
        self.recalculate()
        self.fetch_online()

    @property
    def current_city(self) -> Dict:
        return self.CITIES[self.city_idx % len(self.CITIES)]

    def _config_path(self) -> str:
        d_new = os.path.expanduser("~/.config/pulo")
        d_old = os.path.expanduser("~/.config/pulau")
        if not os.path.isdir(d_new) and os.path.isdir(d_old):
            try:
                import shutil
                shutil.copytree(d_old, d_new)
            except Exception:
                pass
        os.makedirs(d_new, exist_ok=True)
        return os.path.join(d_new, "settings.json")

    def _load_config(self):
        try:
            p = self._config_path()
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    c_name = cfg.get("prayer_city", "Jakarta")
                    self.adzan_vibration_sec = int(cfg.get("adzan_vibration_sec", 10))
                    for i, c in enumerate(self.CITIES):
                        if c["name"].lower() == c_name.lower():
                            self.city_idx = i
                            break
        except Exception:
            pass

    def _save_config(self):
        try:
            p = self._config_path()
            cfg = {}
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                except Exception:
                    pass
            cfg["prayer_city"] = self.current_city["name"]
            cfg["adzan_vibration_sec"] = self.adzan_vibration_sec
            with open(p, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass

    def cycle_city(self) -> str:
        self.city_idx = (self.city_idx + 1) % len(self.CITIES)
        self._save_config()
        self.recalculate()
        self.fetch_online()
        if self.on_city_change:
            self.on_city_change()
        if self.callback:
            GLib.idle_add(self.callback)
        return self.current_city["name"]

    def set_city_by_name(self, name: str) -> bool:
        for i, c in enumerate(self.CITIES):
            if c["name"].lower() == name.lower():
                self.city_idx = i
                self._save_config()
                self.recalculate()
                self.fetch_online()
                if self.on_city_change:
                    self.on_city_change()
                if self.callback:
                    GLib.idle_add(self.callback)
                return True
        return False

    def recalculate(self):
        city = self.current_city
        dt = datetime.date.today()
        y, m, d = dt.year, dt.month, dt.day
        if m <= 2:
            y -= 1
            m += 12
        a = math.floor(y / 100)
        b = 2 - a + math.floor(a / 4)
        jd = math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d + b - 1524.5
        d_days = jd - 2451545.0
        g = (357.529 + 0.98560028 * d_days) % 360
        q = (280.459 + 0.98564736 * d_days) % 360
        l = (q + 1.915 * math.sin(math.radians(g)) + 0.020 * math.sin(math.radians(2 * g))) % 360
        e = 23.439 - 0.00000036 * d_days
        ra = math.degrees(
            math.atan2(math.cos(math.radians(e)) * math.sin(math.radians(l)), math.cos(math.radians(l)))
        ) / 15.0
        ra = (ra + 24) % 24
        dec = math.degrees(math.asin(math.sin(math.radians(e)) * math.sin(math.radians(l))))

        eq_t = (q / 15.0) - ra
        while eq_t > 12:
            eq_t -= 24
        while eq_t < -12:
            eq_t += 24
        noon = 12.0 + city["tz"] - (city["lon"] / 15.0) - eq_t

        def get_ha(alt_deg):
            cos_t = (
                math.sin(math.radians(alt_deg))
                - math.sin(math.radians(city["lat"])) * math.sin(math.radians(dec))
            ) / (math.cos(math.radians(city["lat"])) * math.cos(math.radians(dec)))
            if cos_t > 1.0 or cos_t < -1.0:
                return None

            return math.degrees(math.acos(cos_t)) / 15.0

        fajr_ha = get_ha(-20.0)
        fajr = (noon - fajr_ha + (2.0 / 60.0)) if fajr_ha else 4.0
        dhuhr = noon + (2.0 / 60.0)

        alt_asr = math.degrees(math.atan(1.0 / (1.0 + math.tan(math.radians(abs(city["lat"] - dec))))))
        asr_ha = get_ha(alt_asr)
        asr = (noon + asr_ha + (2.0 / 60.0)) if asr_ha else 15.0

        mag_ha = get_ha(-0.833)
        maghrib = (noon + mag_ha + (2.0 / 60.0)) if mag_ha else 18.0

        isha_ha = get_ha(-18.0)
        isha = (noon + isha_ha + (2.0 / 60.0)) if isha_ha else 19.0

        def to_hm(val: float) -> Tuple[str, int]:
            h = int(val)
            m = int(round((val - h) * 60))
            if m >= 60:
                h += 1
                m = 0
            h = h % 24
            return f"{h:02d}:{m:02d}", h * 3600 + m * 60

        self.today_times = {
            "Subuh": to_hm(fajr),
            "Dzuhur": to_hm(dhuhr),
            "Ashar": to_hm(asr),
            "Maghrib": to_hm(maghrib),
            "Isya": to_hm(isha),
        }

    def fetch_online(self):
        city = self.current_city
        dt = datetime.date.today()

        def _worker():
            try:
                url = f"https://api.myquran.com/v2/sholat/jadwal/{city['id']}/{dt.year}/{dt.month:02d}/{dt.day:02d}"
                req = urllib.request.Request(url, headers={"User-Agent": "Pulo-Notch/1.0"})
                with urllib.request.urlopen(req, timeout=3.2) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    if data.get("status") and "data" in data and "jadwal" in data["data"]:
                        j = data["data"]["jadwal"]

                        def parse_hm(s: str) -> Tuple[str, int]:
                            parts = s.split(":")
                            h, m = int(parts[0]), int(parts[1])
                            return f"{h:02d}:{m:02d}", h * 3600 + m * 60

                        self.today_times = {
                            "Subuh": parse_hm(j.get("subuh", self.today_times["Subuh"][0])),
                            "Dzuhur": parse_hm(j.get("dzuhur", self.today_times["Dzuhur"][0])),
                            "Ashar": parse_hm(j.get("ashar", self.today_times["Ashar"][0])),
                            "Maghrib": parse_hm(j.get("maghrib", self.today_times["Maghrib"][0])),
                            "Isya": parse_hm(j.get("isya", self.today_times["Isya"][0])),
                        }
                        if self.callback:
                            GLib.idle_add(self.callback)
            except Exception:
                pass

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def get_next_prayer(self, now: Optional[datetime.datetime] = None) -> Tuple[str, str, int]:
        if now is None:
            now = datetime.datetime.now()
        cur_sec = now.hour * 3600 + now.minute * 60 + now.second
        for name in ["Subuh", "Dzuhur", "Ashar", "Maghrib", "Isya"]:
            t_str, p_sec = self.today_times.get(name, ("--:--", 0))
            if p_sec > cur_sec:
                return name, t_str, p_sec - cur_sec
        # After Isya -> Next is Subuh tomorrow
        sub_str, sub_sec = self.today_times.get("Subuh", ("04:06", 14760))
        rem = (86400 - cur_sec) + sub_sec
        return "Subuh", sub_str, rem

    def check_alert(self, now: Optional[datetime.datetime] = None) -> Optional[PrayerAlert]:
        if self.simulated_alert:
            return self.simulated_alert

        if now is None:
            now = datetime.datetime.now()
        cur_sec = now.hour * 3600 + now.minute * 60 + now.second

        # Alert range:
        # 1. 5 minutes before adzan (0 < diff <= 300) -> yellow/amber warning
        # 2. At adzan time for dynamic vibration seconds (-adzan_vibration_sec <= diff <= 0) -> green vibrating crescent moon
        # 3. After vibration seconds expire (diff < -adzan_vibration_sec) -> completely disappears
        for name in ["Subuh", "Dzuhur", "Ashar", "Maghrib", "Isya"]:
            t_str, p_sec = self.today_times.get(name, ("", -9999))
            diff = p_sec - cur_sec
            if -self.adzan_vibration_sec <= diff <= 300:
                mins_left = max(0, int(math.ceil(diff / 60.0)))
                is_adzan = diff <= 0
                return PrayerAlert(
                    name=name,
                    time_str=t_str,
                    minutes_left=mins_left,
                    is_adzan=is_adzan,
                )
        return None


# =============================================================================
# Weather Manager (Open-Meteo API)
# =============================================================================
@dataclass
class WeatherData:
    temperature: float = 0.0
    apparent_temperature: float = 0.0
    humidity: int = 0
    wind_speed: float = 0.0
    precipitation: float = 0.0
    weather_code: int = 0
    condition_text: str = "Cerah"
    temp_max: float = 0.0
    temp_min: float = 0.0
    hourly: List[Tuple[str, float, int, str]] = field(default_factory=list)
    daily: List[Tuple[str, float, float, int, str]] = field(default_factory=list)
    city_name: str = "Banyumas"
    last_updated: float = 0.0
    is_loading: bool = False


WMO_WEATHER_CODES = {
    0: ("Cerah", "sun"),
    1: ("Cerah Berawan", "sun_cloud"),
    2: ("Sebagian Berawan", "sun_cloud"),
    3: ("Mendung", "cloud"),
    45: ("Berkabut", "fog"),
    48: ("Berkabut Tebal", "fog"),
    51: ("Gerimis Ringan", "drizzle"),
    53: ("Gerimis", "drizzle"),
    55: ("Gerimis Lebat", "drizzle"),
    61: ("Hujan Ringan", "rain"),
    63: ("Hujan Sedang", "rain"),
    65: ("Hujan Lebat", "heavy_rain"),
    71: ("Salju Ringan", "snow"),
    73: ("Salju", "snow"),
    75: ("Salju Lebat", "snow"),
    80: ("Hujan Ringan", "rain"),
    81: ("Hujan Lebat", "heavy_rain"),
    82: ("Hujan Sangat Deras", "heavy_rain"),
    95: ("Badai Petir", "thunder"),
    96: ("Badai Petir & Es", "thunder"),
    99: ("Badai Petir Hebat", "thunder"),
}


def interpret_weather_code(code: int) -> Tuple[str, str]:
    return WMO_WEATHER_CODES.get(code, ("Berawan", "cloud"))


class WeatherManager:
    """Manages weather data from Open-Meteo API with offline caching and background auto-refresh."""

    def __init__(self, prayer_manager: Optional[PrayerManager] = None, callback=None):
        self.prayer_manager = prayer_manager
        self.callback = callback
        self.data: Optional[WeatherData] = None
        self._load_cache()
        self.fetch_online()

    def _cache_path(self) -> str:
        d = os.path.expanduser("~/.cache/pulo")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "weather_cache.json")

    def _load_cache(self):
        try:
            p = self._cache_path()
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    c = json.load(f)
                    self.data = WeatherData(
                        temperature=float(c.get("temperature", 27.0)),
                        apparent_temperature=float(c.get("apparent_temperature", 28.0)),
                        humidity=int(c.get("humidity", 75)),
                        wind_speed=float(c.get("wind_speed", 10.0)),
                        precipitation=float(c.get("precipitation", 0.0)),
                        weather_code=int(c.get("weather_code", 1)),
                        condition_text=c.get("condition_text", "Cerah Berawan"),
                        temp_max=float(c.get("temp_max", 31.0)),
                        temp_min=float(c.get("temp_min", 24.0)),
                        hourly=[tuple(x) for x in c.get("hourly", [])],
                        daily=[tuple(x) for x in c.get("daily", [])],
                        city_name=c.get("city_name", "Banyumas"),
                        last_updated=float(c.get("last_updated", 0.0)),
                    )
        except Exception:
            pass

    def _save_cache(self):
        if not self.data:
            return
        try:
            p = self._cache_path()
            with open(p, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "temperature": self.data.temperature,
                        "apparent_temperature": self.data.apparent_temperature,
                        "humidity": self.data.humidity,
                        "wind_speed": self.data.wind_speed,
                        "precipitation": self.data.precipitation,
                        "weather_code": self.data.weather_code,
                        "condition_text": self.data.condition_text,
                        "temp_max": self.data.temp_max,
                        "temp_min": self.data.temp_min,
                        "hourly": self.data.hourly,
                        "daily": self.data.daily,
                        "city_name": self.data.city_name,
                        "last_updated": self.data.last_updated,
                    },
                    f,
                    indent=2,
                )
        except Exception:
            pass

    def get_city_coords(self) -> Tuple[str, float, float]:
        if self.prayer_manager:
            c = self.prayer_manager.current_city
            return c["name"], c["lat"], c["lon"]
        return "Banyumas", -7.5167, 109.2833

    def fetch_online(self):
        def _worker():
            city_name, lat, lon = self.get_city_coords()
            url = (
                f"https://api.open-meteo.com/v1/forecast?"
                f"latitude={lat:.4f}&longitude={lon:.4f}&"
                f"current=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m&"
                f"hourly=temperature_2m,weather_code&"
                f"daily=weather_code,temperature_2m_max,temperature_2m_min&"
                f"timezone=auto&forecast_days=3"
            )
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Pulo-Linux/1.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    raw = resp.read().decode("utf-8")
                    res = json.loads(raw)
                    curr = res.get("current", {})
                    code = int(curr.get("weather_code", 0))
                    cond_txt, _ = interpret_weather_code(code)

                    # Hourly forecast (next 5 hours starting from current hour)
                    h_times = res.get("hourly", {}).get("time", [])
                    h_temps = res.get("hourly", {}).get("temperature_2m", [])
                    h_codes = res.get("hourly", {}).get("weather_code", [])

                    now_str = datetime.datetime.now().strftime("%Y-%m-%dT%H:00")
                    cur_idx = 0
                    for idx, ht in enumerate(h_times):
                        if ht >= now_str:
                            cur_idx = idx
                            break

                    hourly_list = []
                    for k in range(cur_idx, min(cur_idx + 6, len(h_times))):
                        t_part = h_times[k].split("T")[-1]
                        h_t = float(h_temps[k]) if k < len(h_temps) else 0.0
                        h_c = int(h_codes[k]) if k < len(h_codes) else 0
                        h_desc, _ = interpret_weather_code(h_c)
                        label = "Sekarang" if k == cur_idx else t_part
                        hourly_list.append((label, h_t, h_c, h_desc))

                    # Daily forecast
                    d_times = res.get("daily", {}).get("time", [])
                    d_maxs = res.get("daily", {}).get("temperature_2m_max", [])
                    d_mins = res.get("daily", {}).get("temperature_2m_min", [])
                    d_codes = res.get("daily", {}).get("weather_code", [])
                    day_labels = ["Hari Ini", "Besok", "Lusa"]
                    daily_list = []
                    for d_i in range(min(3, len(d_times))):
                        lbl = day_labels[d_i] if d_i < len(day_labels) else d_times[d_i]
                        d_max = float(d_maxs[d_i]) if d_i < len(d_maxs) else 0.0
                        d_min = float(d_mins[d_i]) if d_i < len(d_mins) else 0.0
                        d_c = int(d_codes[d_i]) if d_i < len(d_codes) else 0
                        d_desc, _ = interpret_weather_code(d_c)
                        daily_list.append((lbl, d_min, d_max, d_c, d_desc))

                    t_max = daily_list[0][2] if daily_list else float(curr.get("temperature_2m", 0.0))
                    t_min = daily_list[0][1] if daily_list else float(curr.get("temperature_2m", 0.0))

                    self.data = WeatherData(
                        temperature=float(curr.get("temperature_2m", 0.0)),
                        apparent_temperature=float(curr.get("apparent_temperature", 0.0)),
                        humidity=int(curr.get("relative_humidity_2m", 0)),
                        wind_speed=float(curr.get("wind_speed_10m", 0.0)),
                        precipitation=float(curr.get("precipitation", 0.0)),
                        weather_code=code,
                        condition_text=cond_txt,
                        temp_max=t_max,
                        temp_min=t_min,
                        hourly=hourly_list,
                        daily=daily_list,
                        city_name=city_name,
                        last_updated=time.time(),
                        is_loading=False,
                    )
                    self._save_cache()
                    if self.callback:
                        GLib.idle_add(self.callback)
            except Exception:
                pass

        t = threading.Thread(target=_worker, daemon=True)
        t.start()


# =============================================================================
# Financial Market Manager (IHSG & Kurs Rupiah USD/SGD/EUR)
# =============================================================================
@dataclass
class StockItem:
    symbol: str = "^JKSE"
    name: str = "IHSG"
    price: float = 0.0
    change: float = 0.0
    change_pct: float = 0.0
    prev_close: float = 0.0
    high: float = 0.0
    low: float = 0.0
    history: List[float] = field(default_factory=list)

    @property
    def is_up(self) -> bool:
        return self.change >= 0


@dataclass
class CurrencyItem:
    pair: str = "USD/IDR"
    name: str = "Dolar AS"
    rate: float = 0.0
    change: float = 0.0
    change_pct: float = 0.0

    @property
    def is_up(self) -> bool:
        return self.change >= 0


@dataclass
class FinanceData:
    ihsg: Optional[StockItem] = None
    currencies: List[CurrencyItem] = field(default_factory=list)
    btc: List[CurrencyItem] = field(default_factory=list)
    last_updated: float = 0.0
    is_loading: bool = False


class FinanceManager:
    """Manages real-time IHSG index and Rupiah currency exchange rates (USD, SGD, EUR) via Yahoo Finance API."""

    def __init__(self, callback=None):
        self.callback = callback
        self.data: FinanceData = FinanceData()
        self._load_cache()
        self.fetch_online()

    def _cache_path(self) -> str:
        d = os.path.expanduser("~/.cache/pulo")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "finance_cache.json")

    def _load_cache(self):
        try:
            p = self._cache_path()
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    c = json.load(f)
                    ihsg_raw = c.get("ihsg")
                    ihsg_item = None
                    if ihsg_raw:
                        ihsg_item = StockItem(
                            symbol=ihsg_raw.get("symbol", "^JKSE"),
                            name=ihsg_raw.get("name", "IHSG"),
                            price=float(ihsg_raw.get("price", 0.0)),
                            change=float(ihsg_raw.get("change", 0.0)),
                            change_pct=float(ihsg_raw.get("change_pct", 0.0)),
                            prev_close=float(ihsg_raw.get("prev_close", 0.0)),
                            high=float(ihsg_raw.get("high", 0.0)),
                            low=float(ihsg_raw.get("low", 0.0)),
                            history=[float(x) for x in ihsg_raw.get("history", [])],
                        )
                    currs = []
                    for cr in c.get("currencies", []):
                        currs.append(
                            CurrencyItem(
                                pair=cr.get("pair", ""),
                                name=cr.get("name", ""),
                                rate=float(cr.get("rate", 0.0)),
                                change=float(cr.get("change", 0.0)),
                                change_pct=float(cr.get("change_pct", 0.0)),
                            )
                        )
                    btcs = []
                    for br in c.get("btc", []):
                        btcs.append(
                            CurrencyItem(
                                pair=br.get("pair", ""),
                                name=br.get("name", ""),
                                rate=float(br.get("rate", 0.0)),
                                change=float(br.get("change", 0.0)),
                                change_pct=float(br.get("change_pct", 0.0)),
                            )
                        )
                    self.data = FinanceData(
                        ihsg=ihsg_item,
                        currencies=currs,
                        btc=btcs,
                        last_updated=float(c.get("last_updated", 0.0)),
                        is_loading=False,
                    )
        except Exception:
            pass

    def _save_cache(self):
        if not self.data:
            return
        try:
            p = self._cache_path()
            ihsg_dict = None
            if self.data.ihsg:
                ihsg_dict = {
                    "symbol": self.data.ihsg.symbol,
                    "name": self.data.ihsg.name,
                    "price": self.data.ihsg.price,
                    "change": self.data.ihsg.change,
                    "change_pct": self.data.ihsg.change_pct,
                    "prev_close": self.data.ihsg.prev_close,
                    "high": self.data.ihsg.high,
                    "low": self.data.ihsg.low,
                    "history": self.data.ihsg.history,
                }
            curr_list = []
            for c in self.data.currencies:
                curr_list.append({
                    "pair": c.pair,
                    "name": c.name,
                    "rate": c.rate,
                    "change": c.change,
                    "change_pct": c.change_pct,
                })
            btc_list = []
            for b in self.data.btc:
                btc_list.append({
                    "pair": b.pair,
                    "name": b.name,
                    "rate": b.rate,
                    "change": b.change,
                    "change_pct": b.change_pct,
                })
            with open(p, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "ihsg": ihsg_dict,
                        "currencies": curr_list,
                        "btc": btc_list,
                        "last_updated": self.data.last_updated,
                    },
                    f,
                    indent=2,
                )
        except Exception:
            pass

    def fetch_online(self):
        def _worker():
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            ihsg_item = None
            # 1. Fetch IHSG (^JKSE) - True Daily Change & Sparkline
            try:
                # Daily chart range=7d to calculate genuine daily change against yesterday close
                url_daily = "https://query1.finance.yahoo.com/v8/finance/chart/%5EJKSE?interval=1d&range=7d"
                req_d = urllib.request.Request(url_daily, headers=headers)
                with urllib.request.urlopen(req_d, timeout=6) as resp:
                    raw_d = json.loads(resp.read().decode("utf-8"))
                    res_d = raw_d["chart"]["result"][0]
                    meta_d = res_d["meta"]
                    price = float(meta_d.get("regularMarketPrice", 0.0))
                    high = float(meta_d.get("regularMarketDayHigh", price))
                    low = float(meta_d.get("regularMarketDayLow", price))
                    quotes_d = res_d.get("indicators", {}).get("quote", [{}])[0]
                    closes_d = [float(c) for c in quotes_d.get("close", []) if c is not None]

                    # True yesterday close is the second to last close in daily series
                    if len(closes_d) >= 2:
                        prev = closes_d[-2]
                    elif meta_d.get("previousClose") is not None:
                        prev = float(meta_d["previousClose"])
                    elif closes_d:
                        prev = closes_d[0]
                    else:
                        prev = price

                    if price <= 0.0 and closes_d:
                        price = closes_d[-1]

                    chg = price - prev
                    pct = (chg / prev) * 100.0 if prev > 0.0 else 0.0

                # Fetch today intraday points for the mini sparkline chart
                history_points = []
                try:
                    url_intra = "https://query1.finance.yahoo.com/v8/finance/chart/%5EJKSE?interval=15m&range=1d"
                    req_i = urllib.request.Request(url_intra, headers=headers)
                    with urllib.request.urlopen(req_i, timeout=5) as resp_i:
                        raw_i = json.loads(resp_i.read().decode("utf-8"))
                        quotes_i = raw_i["chart"]["result"][0].get("indicators", {}).get("quote", [{}])[0]
                        history_points = [float(c) for c in quotes_i.get("close", []) if c is not None]
                except Exception:
                    pass

                if not history_points and closes_d:
                    history_points = closes_d

                ihsg_item = StockItem(
                    symbol="^JKSE",
                    name="IHSG",
                    price=price,
                    change=chg,
                    change_pct=pct,
                    prev_close=prev,
                    high=high,
                    low=low,
                    history=history_points,
                )
            except Exception:
                if self.data and self.data.ihsg:
                    ihsg_item = self.data.ihsg

            # 2. Fetch Currencies (USD/IDR, SGD/IDR, EUR/IDR) - True Daily Change
            curr_pairs = [
                ("USDIDR=X", "USD/IDR", "Dolar AS"),
                ("SGDIDR=X", "SGD/IDR", "Dolar SGD"),
                ("EURIDR=X", "EUR/IDR", "Euro"),
            ]
            currencies = []
            for sym, pair_lbl, name_lbl in curr_pairs:
                try:
                    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=7d"
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        raw = json.loads(resp.read().decode("utf-8"))
                        res_c = raw["chart"]["result"][0]
                        meta = res_c["meta"]
                        price = float(meta.get("regularMarketPrice", 0.0))
                        quotes_c = res_c.get("indicators", {}).get("quote", [{}])[0]
                        closes_c = [float(c) for c in quotes_c.get("close", []) if c is not None]

                        if len(closes_c) >= 2:
                            prev = closes_c[-2]
                        elif meta.get("previousClose") is not None:
                            prev = float(meta["previousClose"])
                        elif closes_c:
                            prev = closes_c[0]
                        else:
                            prev = price

                        if price <= 0.0 and closes_c:
                            price = closes_c[-1]

                        chg = price - prev
                        pct = (chg / prev) * 100.0 if prev > 0.0 else 0.0

                        currencies.append(
                            CurrencyItem(
                                pair=pair_lbl,
                                name=name_lbl,
                                rate=price,
                                change=chg,
                                change_pct=pct,
                            )
                        )
                except Exception:
                    pass

            if not currencies and self.data and self.data.currencies:
                currencies = self.data.currencies

            # 3. Fetch Bitcoin (BTC/IDR & BTC/USD) via CoinGecko (with Yahoo BTC-USD fallback)
            btc_items = []
            try:
                cg_url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd,idr&include_24hr_change=true"
                req_cg = urllib.request.Request(cg_url, headers={"User-Agent": "PuloNotch/1.0"})
                with urllib.request.urlopen(req_cg, timeout=6) as resp_cg:
                    cg = json.loads(resp_cg.read().decode("utf-8")).get("bitcoin", {})
                    idr_p = float(cg.get("idr", 0.0))
                    idr_pct = float(cg.get("idr_24h_change", 0.0))
                    usd_p = float(cg.get("usd", 0.0))
                    usd_pct = float(cg.get("usd_24h_change", 0.0))
                    if idr_p > 0.0:
                        btc_items.append(CurrencyItem(
                            pair="BTC/IDR",
                            name="Bitcoin (IDR)",
                            rate=idr_p,
                            change=idr_p * (idr_pct / 100.0),
                            change_pct=idr_pct,
                        ))
                    if usd_p > 0.0:
                        btc_items.append(CurrencyItem(
                            pair="BTC/USD",
                            name="Bitcoin (USD)",
                            rate=usd_p,
                            change=usd_p * (usd_pct / 100.0),
                            change_pct=usd_pct,
                        ))
            except Exception:
                pass

            if not btc_items:
                try:
                    url_b = "https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD?interval=1d&range=7d"
                    req_b = urllib.request.Request(url_b, headers=headers)
                    with urllib.request.urlopen(req_b, timeout=5) as resp_b:
                        raw_b = json.loads(resp_b.read().decode("utf-8"))["chart"]["result"][0]
                        meta_b = raw_b["meta"]
                        usd_p = float(meta_b.get("regularMarketPrice", 0.0))
                        closes_b = [float(c) for c in raw_b.get("indicators", {}).get("quote", [{}])[0].get("close", []) if c is not None]
                        prev_b = closes_b[-2] if len(closes_b) >= 2 else usd_p
                        usd_chg = usd_p - prev_b
                        usd_pct = (usd_chg / prev_b) * 100.0 if prev_b > 0.0 else 0.0
                        usd_idr_rate = next((c.rate for c in currencies if c.pair == "USD/IDR" and c.rate > 0), 0.0)
                        if usd_idr_rate > 0.0 and usd_p > 0.0:
                            idr_p = usd_p * usd_idr_rate
                            btc_items.append(CurrencyItem(
                                pair="BTC/IDR", name="Bitcoin (IDR)",
                                rate=idr_p, change=idr_p * (usd_pct / 100.0), change_pct=usd_pct,
                            ))
                        if usd_p > 0.0:
                            btc_items.append(CurrencyItem(
                                pair="BTC/USD", name="Bitcoin (USD)",
                                rate=usd_p, change=usd_chg, change_pct=usd_pct,
                            ))
                except Exception:
                    pass

            if not btc_items and self.data and self.data.btc:
                btc_items = self.data.btc

            if ihsg_item or currencies or btc_items:
                self.data = FinanceData(
                    ihsg=ihsg_item,
                    currencies=currencies,
                    btc=btc_items,
                    last_updated=time.time(),
                    is_loading=False,
                )
                self._save_cache()
                if self.callback:
                    GLib.idle_add(self.callback)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()


class HealthManager:
    """Smart Ergonomic Health Reminder based on Ophthalmology & Sports Medicine Journals:
    - 20-20-20 Eye Break: Every 20 mins of active screen time, look 20 ft away for 20 secs.
    - Sedentary Posture Break: Every 50 mins of prolonged sitting, stand & stretch for 2-3 mins.
    - Smart Idle Pause: Freezes / resets timer if user steps away (idle > 5 mins).
    """

    def __init__(self, controller):
        self.controller = controller
        self.eye_enabled: bool = True
        self.eye_interval_sec: float = 20.0 * 60.0  # 20 mins (20-20-20 rule)
        self.eye_duration_sec: float = 20.0         # 20 secs countdown

        self.stretch_enabled: bool = True
        self.stretch_interval_sec: float = 50.0 * 60.0 # 50 mins (Anti-sedentary break)
        self.stretch_duration_sec: float = 25.0        # 25 secs display

        self.smart_idle_pause: bool = True
        self.idle_threshold_sec: float = 300.0         # 5 mins

        self.last_eye_break: float = time.time()
        self.last_stretch_break: float = time.time()
        self.is_paused_due_to_idle: bool = False

    def on_second_tick(self):
        now = time.time()
        idle_sec = self.controller.get_idle_seconds()

        # Smart Idle: If user is away for > 5 mins, pause / reset break timer
        if self.smart_idle_pause and idle_sec >= self.idle_threshold_sec:
            self.is_paused_due_to_idle = True
            self.last_eye_break = now
            self.last_stretch_break = now
            return

        if self.is_paused_due_to_idle:
            self.is_paused_due_to_idle = False
            self.last_eye_break = now
            self.last_stretch_break = now
            return

        # Do not trigger while panel is open, or another HUD/toast is active
        if any(w.panel_open for w in self.controller.windows) or self.controller.active_hud is not None or self.controller.active_toast is not None:
            return

        # Check Eye Break (20-20-20 Rule)
        if self.eye_enabled and (now - self.last_eye_break) >= self.eye_interval_sec:
            self.trigger_eye_break()

        # Check Stretch Break (50 min Rule)
        elif self.stretch_enabled and (now - self.last_stretch_break) >= self.stretch_interval_sec:
            self.trigger_stretch_break()

    def trigger_eye_break(self):
        self.last_eye_break = time.time()
        self.controller.show_hud(
            kind="eye_break",
            value=100,
            extra_text="Istirahat Mata: Tatap 6 Meter",
            duration=self.eye_duration_sec,
        )
        if hasattr(self.controller, "quote_mgr") and self.controller.quote_mgr and self.controller.quote_mgr.eye_break_enabled:
            self.controller.quote_mgr.fetch_for_eye_break()

    def trigger_stretch_break(self):
        self.last_stretch_break = time.time()
        self.controller.show_hud(
            kind="stretch_break",
            value=100,
            extra_text="Waktunya Berdiri & Regang",
            duration=self.stretch_duration_sec,
        )

    def get_status_str(self) -> str:
        now = time.time()
        eye_rem = max(0, int(self.eye_interval_sec - (now - self.last_eye_break)))
        str_rem = max(0, int(self.stretch_interval_sec - (now - self.last_stretch_break)))
        return f"Mata 20-20-20: {eye_rem // 60}m {eye_rem % 60}s lagi | Berdiri & Gerak: {str_rem // 60}m {str_rem % 60}s lagi"


QUOTE_CATEGORIES = [
    ("random", "Acak (Semua Kategori)"),
    ("motivasi", "Motivasi"),
    ("kehidupan", "Kehidupan"),
    ("spiritual", "Spiritual"),
    ("ilmu pengetahuan", "Ilmu Pengetahuan"),
    ("kepemimpinan", "Kepemimpinan"),
    ("kreativitas", "Kreativitas"),
    ("pendidikan", "Pendidikan"),
    ("seni", "Seni"),
    ("cinta", "Cinta"),
    ("lingkungan", "Lingkungan"),
    ("nasionalisme", "Nasionalisme"),
    ("perempuan", "Perempuan"),
]


class QuoteManager:
    """Fetches Indonesian quotes from quotes.liupurnomo.com and triggers them
    once in the morning (05:00-11:00) and/or after each 20-20-20 eye break.
    """

    FALLBACK_QUOTES = [
        ("Hidup itu seperti sepeda. Agar tetap seimbang, kamu harus terus bergerak.", "Albert Einstein"),
        ("Cara terbaik untuk memulai adalah berhenti berbicara dan mulai melakukan.", "Walt Disney"),
        ("Kesempatan tidak terjadi begitu saja, kamulah yang menciptakannya.", "Chris Grosser"),
        ("Kegagalan adalah kesempatan untuk memulai lagi dengan lebih cerdas.", "Henry Ford"),
    ]

    def __init__(self, controller):
        self.controller = controller
        self.morning_enabled: bool = True
        self.eye_break_enabled: bool = True
        self.category: str = "random"
        self.last_morning_date: str = ""
        self._fetching: bool = False

    def on_second_tick(self):
        if not self.morning_enabled:
            return
        now_dt = datetime.datetime.now()
        if not (5 <= now_dt.hour < 11):
            return
        today_str = now_dt.strftime("%Y-%m-%d")
        if self.last_morning_date == today_str:
            return
        if any(w.panel_open for w in self.controller.windows) or self.controller.active_hud is not None or self.controller.active_toast is not None:
            return
        if self.controller.get_idle_seconds() >= 180.0:
            return
        self.fetch_and_show("morning")

    def _fetch_quote_sync(self) -> Tuple[str, str]:
        cat = (self.category or "random").strip().lower()
        q_text, q_author = random.choice(self.FALLBACK_QUOTES)
        try:
            if cat == "random" or not cat:
                url = "https://quotes.liupurnomo.com/api/quotes/random"
            else:
                url = f"https://quotes.liupurnomo.com/api/quotes?category={urllib.parse.quote(cat)}&page=1&limit=30"
            req = urllib.request.Request(url, headers={"User-Agent": "PuloNotch/1.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                data = payload.get("data")
                if isinstance(data, list) and data:
                    item = random.choice(data)
                    if item.get("text"):
                        q_text = str(item["text"]).strip()
                        q_author = str(item.get("author") or "Anonim").strip()
                elif isinstance(data, dict) and data.get("text"):
                    q_text = str(data["text"]).strip()
                    q_author = str(data.get("author") or "Anonim").strip()
        except Exception:
            pass
        return q_text, q_author

    def fetch_and_show(self, trigger_reason: str = ""):
        if self._fetching:
            return
        self._fetching = True

        def _worker():
            try:
                q_text, q_author = self._fetch_quote_sync()
            finally:
                self._fetching = False
            GLib.idle_add(self._show_quote_hud, q_text, q_author, trigger_reason)

        threading.Thread(target=_worker, daemon=True).start()

    def fetch_for_eye_break(self):
        def _worker():
            q_text, q_author = self._fetch_quote_sync()
            GLib.idle_add(self._attach_quote_to_eye_break, q_text, q_author)

        threading.Thread(target=_worker, daemon=True).start()

    def _attach_quote_to_eye_break(self, text: str, author: str) -> bool:
        hud = self.controller.active_hud
        if hud is not None and hud.kind == "eye_break":
            hud.extra_text = json.dumps({"text": text, "author": author}, ensure_ascii=False)
            self.controller.trigger_animation()
            self.controller.render_all()
        return False

    def _show_quote_hud(self, text: str, author: str, trigger_reason: str = "") -> bool:
        if trigger_reason == "morning":
            self.last_morning_date = datetime.datetime.now().strftime("%Y-%m-%d")
            self.controller._save_settings()
        extra = json.dumps({"text": text, "author": author}, ensure_ascii=False)
        self.controller.show_hud(
            kind="quote",
            value=0,
            is_muted=False,
            extra_text=extra,
            duration=9.0,
        )
        return False


@dataclass
class SystemControlState:
    wifi_enabled: bool = False
    wifi_ssid: str = ""
    bluetooth_enabled: bool = False
    bluetooth_device: str = ""
    dnd_enabled: bool = False
    mic_muted: bool = False
    volume: int = 70
    volume_muted: bool = False
    brightness: int = 80
    cpu_percent: int = 0
    ram_percent: int = 0
    ram_used_gb: float = 0.0
    ram_total_gb: float = 0.0
    temperature_c: int = 0
    last_updated: float = 0.0


class SystemControlManager:
    """Manages system hardware shortcuts and telemetry: Wi-Fi, BT, Audio, Brightness, DND, Mic, CPU, RAM, Temp."""

    def __init__(self, callback=None, hud_callback=None):
        self.callback = callback
        self.hud_callback = hud_callback
        self.state = SystemControlState()
        self._lock = threading.Lock()
        self._last_cpu_idle: float = 0.0
        self._last_cpu_total: float = 0.0
        self.refresh_async()

    def refresh(self):
        # 1. Wi-Fi
        wifi_on = False
        wifi_ssid = ""
        try:
            w_out = subprocess.check_output(["nmcli", "radio", "wifi"], stderr=subprocess.DEVNULL, timeout=1).decode().strip()
            wifi_on = (w_out == "enabled")
            if wifi_on:
                c_out = subprocess.check_output(["nmcli", "-t", "-f", "TYPE,NAME", "connection", "show", "--active"], stderr=subprocess.DEVNULL, timeout=1).decode()
                for line in c_out.splitlines():
                    if line.startswith("802-11-wireless:"):
                        wifi_ssid = line.split(":", 1)[1].strip()
                        break
        except Exception:
            pass

        # 2. Bluetooth
        bt_on = False
        bt_dev = ""
        try:
            b_out = subprocess.check_output(["bluetoothctl", "show"], stderr=subprocess.DEVNULL, timeout=1).decode()
            bt_on = ("Powered: yes" in b_out)
            if bt_on:
                d_out = subprocess.check_output(["bluetoothctl", "devices", "Connected"], stderr=subprocess.DEVNULL, timeout=1).decode()
                lines = [l.strip() for l in d_out.splitlines() if l.strip()]
                if lines:
                    parts = lines[0].split(None, 2)
                    if len(parts) >= 3:
                        bt_dev = parts[2]
        except Exception:
            pass

        # 3. Audio Sink
        vol = 70
        vol_muted = False
        try:
            a_out = subprocess.check_output(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], stderr=subprocess.DEVNULL, timeout=1).decode().strip()
            m = re.search(r"([0-9.]+)", a_out)
            if m:
                vol = max(0, min(100, int(round(float(m.group(1)) * 100))))
            vol_muted = "[MUTED]" in a_out
        except Exception:
            pass

        # 4. Microphone Source
        mic_muted = False
        try:
            m_out = subprocess.check_output(["wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@"], stderr=subprocess.DEVNULL, timeout=1).decode().strip()
            mic_muted = "[MUTED]" in m_out
        except Exception:
            pass

        # 5. Brightness
        br_pct = 80
        try:
            cur = subprocess.check_output(["qdbus6", "org.kde.Solid.PowerManagement", "/org/kde/Solid/PowerManagement/Actions/BrightnessControl", "org.kde.Solid.PowerManagement.Actions.BrightnessControl.brightness"], stderr=subprocess.DEVNULL, timeout=1).decode().strip()
            mx = subprocess.check_output(["qdbus6", "org.kde.Solid.PowerManagement", "/org/kde/Solid/PowerManagement/Actions/BrightnessControl", "org.kde.Solid.PowerManagement.Actions.BrightnessControl.brightnessMax"], stderr=subprocess.DEVNULL, timeout=1).decode().strip()
            if int(mx) > 0:
                br_pct = max(0, min(100, int(round(int(cur) * 100.0 / int(mx)))))
        except Exception:
            pass

        # 6. CPU Utilization (%)
        cpu_pct = 0
        try:
            with open("/proc/stat", "r") as f:
                line = f.readline()
                if line.startswith("cpu "):
                    parts = [float(x) for x in line.split()[1:]]
                    idle = parts[3] + (parts[4] if len(parts) > 4 else 0.0)
                    total = sum(parts)
                    if self._last_cpu_total > 0:
                        diff_idle = idle - self._last_cpu_idle
                        diff_total = total - self._last_cpu_total
                        if diff_total > 0:
                            cpu_pct = max(0, min(100, int(round((1.0 - diff_idle / diff_total) * 100.0))))
                    self._last_cpu_idle = idle
                    self._last_cpu_total = total
        except Exception:
            pass

        # 7. RAM Utilization (% and GB)
        ram_pct = 0
        ram_used = 0.0
        ram_total = 0.0
        try:
            tot_kb = 0
            avail_kb = 0
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        tot_kb = int(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        avail_kb = int(line.split()[1])
                    if tot_kb > 0 and avail_kb > 0:
                        break
            if tot_kb > 0:
                used_kb = max(0, tot_kb - avail_kb)
                ram_pct = max(0, min(100, int(round((used_kb / tot_kb) * 100.0))))
                ram_used = round(used_kb / (1024.0 * 1024.0), 1)
                ram_total = round(tot_kb / (1024.0 * 1024.0), 1)
        except Exception:
            pass

        # 8. Temperature (°C)
        temp_c = 0
        try:
            candidates = []
            for tz in glob.glob("/sys/class/thermal/thermal_zone*"):
                try:
                    t_type = ""
                    with open(os.path.join(tz, "type"), "r") as f:
                        t_type = f.read().strip().lower()
                    with open(os.path.join(tz, "temp"), "r") as f:
                        raw = int(f.read().strip())
                        val = int(round(raw / 1000.0)) if raw > 1000 else raw
                        if 20 <= val <= 115:
                            score = 10 if "x86_pkg_temp" in t_type else (5 if ("cpu" in t_type or "acpitz" in t_type or "tcpu" in t_type) else 1)
                            candidates.append((score, val))
                except Exception:
                    continue
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                temp_c = candidates[0][1]
        except Exception:
            pass

        with self._lock:
            self.state.wifi_enabled = wifi_on
            self.state.wifi_ssid = wifi_ssid
            self.state.bluetooth_enabled = bt_on
            self.state.bluetooth_device = bt_dev
            self.state.volume = vol
            self.state.volume_muted = vol_muted
            self.state.mic_muted = mic_muted
            self.state.brightness = br_pct
            self.state.cpu_percent = cpu_pct
            self.state.ram_percent = ram_pct
            self.state.ram_used_gb = ram_used
            self.state.ram_total_gb = ram_total
            self.state.temperature_c = temp_c
            self.state.last_updated = time.time()

        if self.callback:
            GLib.idle_add(self.callback)

    def refresh_async(self):
        threading.Thread(target=self.refresh, daemon=True).start()

    def toggle_wifi(self):
        target = "off" if self.state.wifi_enabled else "on"
        self.state.wifi_enabled = not self.state.wifi_enabled
        def _bg():
            subprocess.run(["nmcli", "radio", "wifi", target], stderr=subprocess.DEVNULL)
            time.sleep(0.5)
            self.refresh()
        threading.Thread(target=_bg, daemon=True).start()

    def toggle_bluetooth(self):
        target = "off" if self.state.bluetooth_enabled else "on"
        self.state.bluetooth_enabled = not self.state.bluetooth_enabled
        def _bg():
            subprocess.run(["bluetoothctl", "power", target], stderr=subprocess.DEVNULL)
            time.sleep(0.5)
            self.refresh()
        threading.Thread(target=_bg, daemon=True).start()

    def toggle_mic_mute(self):
        self.state.mic_muted = not self.state.mic_muted
        def _bg():
            subprocess.run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "toggle"], stderr=subprocess.DEVNULL)
            self.refresh()
        threading.Thread(target=_bg, daemon=True).start()

    def toggle_volume_mute(self):
        self.state.volume_muted = not self.state.volume_muted
        def _bg():
            subprocess.run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"], stderr=subprocess.DEVNULL)
            self.refresh()
        threading.Thread(target=_bg, daemon=True).start()

    def set_volume(self, pct: int):
        pct = max(0, min(100, pct))
        self.state.volume = pct
        self.state.volume_muted = (pct == 0)
        def _bg():
            val = f"{pct / 100.0:.2f}"
            subprocess.run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", val], stderr=subprocess.DEVNULL)
        threading.Thread(target=_bg, daemon=True).start()

    def set_brightness(self, pct: int):
        pct = max(5, min(100, pct))
        self.state.brightness = pct
        def _bg():
            val = str(int(pct * 100))
            subprocess.run(["qdbus6", "org.kde.Solid.PowerManagement", "/org/kde/Solid/PowerManagement/Actions/BrightnessControl", "org.kde.Solid.PowerManagement.Actions.BrightnessControl.setBrightness", val], stderr=subprocess.DEVNULL)
        threading.Thread(target=_bg, daemon=True).start()

    def _emit_hud(self, kind: str, value: int = 100, is_muted: bool = False, extra_text: str = "", duration: float = 3.5):
        if self.hud_callback:
            GLib.idle_add(lambda: (self.hud_callback(kind, value, is_muted, extra_text, duration), False)[1])

    @staticmethod
    def _copy_to_clipboard(text: str):
        if not text:
            return
        def _do_copy():
            try:
                cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
                cb.set_text(text, -1)
                cb.store()
            except Exception:
                pass
            try:
                app = QApplication.instance()
                if app is not None:
                    app.clipboard().setText(text)
            except Exception:
                pass
            return False
        GLib.idle_add(_do_copy)

    @staticmethod
    def _clean_gui_env() -> Dict[str, str]:
        env = os.environ.copy()
        env.pop("QT_QPA_PLATFORM", None)
        env.pop("QT_NO_GLIB", None)
        return env

    def take_screenshot(self):
        def _bg():
            clean_env = self._clean_gui_env()
            for cmd in (
                ["spectacle", "-r"],
                ["gnome-screenshot", "-a"],
                ["flameshot", "gui"],
                ["xfce4-screenshooter", "-r"],
            ):
                if shutil.which(cmd[0]):
                    subprocess.Popen(cmd, env=clean_env, stderr=subprocess.DEVNULL)
                    return
            self._emit_hud("utility_msg", value=0, extra_text="Alat screenshot tidak ditemukan", duration=3.0)
        threading.Thread(target=_bg, daemon=True).start()

    def pick_color(self):
        """Pick a screen pixel color via XDG Desktop Portal (Wayland-safe) and copy HEX to clipboard."""
        def _start_portal():
            try:
                import dbus
                bus = dbus.SessionBus()
                sender = bus.get_unique_name().lstrip(":").replace(".", "_")
                token = f"pulo_cp_{int(time.time() * 1000)}"
                expected_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
                req_holder = {"path": expected_path, "match": None}

                def _on_response(response, results, path=None):
                    if path and req_holder["path"] and path != req_holder["path"]:
                        return
                    if req_holder["match"] is not None:
                        try:
                            req_holder["match"].remove()
                        except Exception:
                            pass
                        req_holder["match"] = None
                    if int(response) == 0 and "color" in results:
                        try:
                            rgb = [max(0, min(255, int(round(float(c) * 255.0)))) for c in results["color"]]
                            hex_code = f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"
                            self._copy_to_clipboard(hex_code)
                            self._emit_hud("utility_color", value=100, extra_text=hex_code, duration=4.0)
                        except Exception:
                            pass

                req_holder["match"] = bus.add_signal_receiver(
                    _on_response,
                    signal_name="Response",
                    dbus_interface="org.freedesktop.portal.Request",
                    path_keyword="path",
                )
                portal = bus.get_object("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop")
                iface = dbus.Interface(portal, "org.freedesktop.portal.Screenshot")
                req_path = iface.PickColor("", {"handle_token": token})
                if req_path:
                    req_holder["path"] = str(req_path)
            except Exception:
                self._emit_hud("utility_msg", value=0, extra_text="Color Picker portal tidak tersedia", duration=3.0)
            return False

        GLib.idle_add(_start_portal)

    def extract_text_ocr(self):
        """Capture a screen region, extract text with Tesseract OCR, and copy it to clipboard."""
        def _bg():
            if not shutil.which("tesseract"):
                self._emit_hud("utility_msg", value=0, extra_text="Tesseract OCR belum terpasang", duration=3.5)
                return

            tmp_path = ""
            try:
                fd, tmp_path = tempfile.mkstemp(prefix="pulo_ocr_", suffix=".png")
                os.close(fd)
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

                clean_env = self._clean_gui_env()
                captured = False
                if shutil.which("spectacle"):
                    subprocess.run(["spectacle", "-r", "-b", "-n", "-o", tmp_path], env=clean_env, stderr=subprocess.DEVNULL)
                    captured = os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 0
                elif shutil.which("gnome-screenshot"):
                    subprocess.run(["gnome-screenshot", "-a", "-f", tmp_path], env=clean_env, stderr=subprocess.DEVNULL)
                    captured = os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 0
                elif shutil.which("grim") and shutil.which("slurp"):
                    geom = subprocess.check_output(["slurp"], env=clean_env, stderr=subprocess.DEVNULL).decode().strip()
                    if geom:
                        subprocess.run(["grim", "-g", geom, tmp_path], env=clean_env, stderr=subprocess.DEVNULL)
                        captured = os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 0
                elif shutil.which("import"):
                    subprocess.run(["import", tmp_path], env=clean_env, stderr=subprocess.DEVNULL)
                    captured = os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 0
                else:
                    self._emit_hud("utility_msg", value=0, extra_text="Alat capture layar tidak ditemukan", duration=3.0)
                    return

                if not captured:
                    return

                out = subprocess.check_output(
                    ["tesseract", tmp_path, "stdout", "--psm", "6"],
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                ).decode("utf-8", errors="replace").strip()

                if out:
                    self._copy_to_clipboard(out)
                    self._emit_hud("utility_ocr", value=100, extra_text=out, duration=4.5)
                else:
                    self._emit_hud("utility_msg", value=0, extra_text="Tidak ada teks terdeteksi", duration=3.0)
            except Exception:
                self._emit_hud("utility_msg", value=0, extra_text="Gagal mengekstrak teks", duration=3.0)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass

        threading.Thread(target=_bg, daemon=True).start()

    def lock_screen(self):
        def _bg():
            subprocess.run(["loginctl", "lock-session"], stderr=subprocess.DEVNULL)
        threading.Thread(target=_bg, daemon=True).start()


# =============================================================================
# Real Application Icon Resolver for Desktop Notifications
# =============================================================================
class IconResolver:
    """Resolves and caches real application icons for desktop notifications."""

    _cache: Dict[str, Optional[QPixmap]] = {}
    _desktop_cache: Optional[Dict[str, str]] = None
    _gtk_theme = None

    DESKTOP_DIRS = [
        os.path.expanduser("~/.local/share/applications"),
        "/usr/share/applications",
        "/var/lib/snapd/desktop/applications",
        os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
        "/var/lib/flatpak/exports/share/applications",
    ]

    @classmethod
    def _init_gtk_theme(cls):
        if cls._gtk_theme is None and "Gtk" in globals():
            try:
                cls._gtk_theme = Gtk.IconTheme.get_default()
            except Exception:
                pass

    @classmethod
    def _scan_desktop_files(cls) -> Dict[str, str]:
        if cls._desktop_cache is not None:
            return cls._desktop_cache
        res: Dict[str, str] = {}
        for d in cls.DESKTOP_DIRS:
            if not os.path.isdir(d):
                continue
            try:
                for f in os.listdir(d):
                    if not f.endswith(".desktop"):
                        continue
                    full_p = os.path.join(d, f)
                    try:
                        with open(full_p, "r", encoding="utf-8", errors="ignore") as fp:
                            icon_val = ""
                            name_val = ""
                            for line in fp:
                                if line.startswith("Icon=") and not icon_val:
                                    icon_val = line.strip().split("=", 1)[1]
                                elif line.startswith("Name=") and not name_val:
                                    name_val = line.strip().split("=", 1)[1]
                                if icon_val and name_val:
                                    break
                            if icon_val:
                                base = f[:-8].lower()
                                res[base] = icon_val
                                if name_val:
                                    res[name_val.lower()] = icon_val
                    except Exception:
                        pass
            except Exception:
                pass
        cls._desktop_cache = res
        return res

    @classmethod
    def get_icon_pixmap(cls, app_icon: str, app_name: str, size: int = 34) -> Optional[QPixmap]:
        cache_key = f"{app_icon}|{app_name}|{size}"
        if cache_key in cls._cache:
            return cls._cache[cache_key]

        if len(cls._cache) > 256:
            keys = list(cls._cache.keys())
            for k in keys[:128]:
                cls._cache.pop(k, None)

        cls._init_gtk_theme()
        pm = cls._resolve(app_icon, app_name, size)
        cls._cache[cache_key] = pm
        return pm

    @classmethod
    def _resolve(cls, app_icon: str, app_name: str, size: int) -> Optional[QPixmap]:
        # 1. Direct file path
        if app_icon:
            p = app_icon[7:] if app_icon.startswith("file://") else app_icon
            if os.path.isfile(p):
                pm = QPixmap(p)
                if not pm.isNull():
                    return pm.scaled(size * 2, size * 2, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)

        # 2. Build list of candidate icon names
        cands = []
        if app_icon:
            cands.append(app_icon)
            cands.append(app_icon.lower())

        if app_name:
            clean = app_name.lower().strip()
            cands.append(clean)
            cands.append(clean.replace(" ", "-"))
            cands.append(clean.replace(" ", ""))
            if "baterai" in clean or "battery" in clean or "daya" in clean:
                cands.extend(["battery-charging", "battery-good", "battery"])
            elif "pulo" in clean:
                cands.extend(["preferences-system", "dialog-information"])

        for name in cands:
            pm = cls._load_from_theme(name, size)
            if pm:
                return pm

        # 3. Look up in desktop files
        desktop_map = cls._scan_desktop_files()
        for name in cands:
            if name in desktop_map:
                icon_val = desktop_map[name]
                if os.path.isfile(icon_val):
                    pm = QPixmap(icon_val)
                    if not pm.isNull():
                        return pm.scaled(size * 2, size * 2, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                pm = cls._load_from_theme(icon_val, size)
                if pm:
                    return pm

        for name in cands:
            if len(name) < 3:
                continue
            for k, icon_val in desktop_map.items():
                if name in k:
                    if os.path.isfile(icon_val):
                        pm = QPixmap(icon_val)
                        if not pm.isNull():
                            return pm.scaled(size * 2, size * 2, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                    pm = cls._load_from_theme(icon_val, size)
                    if pm:
                        return pm

        for fb in ["dialog-information", "applications-other", "preferences-system"]:
            pm = cls._load_from_theme(fb, size)
            if pm:
                return pm

        return None

    @classmethod
    def _load_from_theme(cls, name: str, size: int) -> Optional[QPixmap]:
        if not name:
            return None
        try:
            ic = QIcon.fromTheme(name)
            if not ic.isNull():
                pm = ic.pixmap(size * 2, size * 2)
                if not pm.isNull():
                    return pm
        except Exception:
            pass

        if cls._gtk_theme:
            try:
                info = cls._gtk_theme.lookup_icon(name, size, 0)
                if info:
                    fn = info.get_filename()
                    if fn and os.path.isfile(fn):
                        pm = QPixmap(fn)
                        if not pm.isNull():
                            return pm.scaled(size * 2, size * 2, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            except Exception:
                pass
        return None


# =============================================================================
# Notch Vector Renderer (PyQt6 QPainter - Borderless Pure Black)
# =============================================================================
class NotchRenderer:
    """Renders the MacBook-style Notch with smooth concave top ears onto a transparent buffer."""

    C_BLACK = QColor(0, 0, 0, 255)            # #000000 Pure Pitch Black
    C_CARD = QColor(20, 22, 28, 255)          # #14161C Inner Card Surface
    C_CARD_HOVER = QColor(34, 38, 48, 255)    # #222630 Hovered Card/Button
    C_TAB_BG = QColor(15, 17, 22, 255)        # #0F1116 Segmented Tab Track
    C_TAB_ACTIVE = QColor(36, 40, 52, 255)    # #242834 Active Tab Pill
    C_WHITE = QColor(245, 247, 250, 255)      # #F5F7FA Primary Text
    C_WHITE_SEC = QColor(215, 220, 228, 255)  # Slightly softened white
    C_MUTED = QColor(163, 177, 198, 255)      # #A3B1C6 Secondary Text
    C_GREEN = QColor(52, 211, 153, 255)       # #34D399 Charging / Full Emerald
    C_GREEN_BG = QColor(52, 211, 153, 42)     # Subtle Emerald Pill Fill
    C_AMBER = QColor(251, 191, 36, 255)       # #FBBF24 Low Battery / DND
    C_AMBER_BG = QColor(251, 191, 36, 42)
    C_RED_BG = QColor(248, 113, 113, 38)
    C_RED = QColor(248, 113, 113, 255)
    C_BLUE = QColor(56, 189, 248, 255)        # Notification Accent
    C_CYAN = QColor(6, 182, 212, 255)         # #06B6D4 LocalSend Cyan
    C_CYAN_BG = QColor(6, 182, 212, 42)       # Subtle LocalSend Pill Fill
    C_INDIGO = QColor(99, 102, 241, 255)      # Indigo Bluetooth Accent

    @staticmethod
    def make_notch_path(w: float, h: float, ear: float = 11.0, bottom_r: float = 14.0) -> QPainterPath:
        ear = min(ear, h * 0.45)
        bottom_r = min(bottom_r, (h - ear) * 0.85, (w - 2 * ear) * 0.45)

        path = QPainterPath()
        path.moveTo(0.0, 0.0)
        path.quadTo(ear, 0.0, ear, ear)
        path.lineTo(ear, h - bottom_r)
        path.quadTo(ear, h, ear + bottom_r, h)
        path.lineTo(w - ear - bottom_r, h)
        path.quadTo(w - ear, h, w - ear, h - bottom_r)
        path.lineTo(w - ear, ear)
        path.quadTo(w - ear, 0.0, w, 0.0)
        path.closeSubpath()
        return path

    @staticmethod
    def make_pill_path(w: float, h: float, corner_r: Optional[float] = None) -> QPainterPath:
        if corner_r is None:
            corner_r = min(w / 2.0, h / 2.0)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.0, 0.0, float(w), float(h)), float(corner_r), float(corner_r))
        return path

    @classmethod
    def draw_camera_lens(cls, p: QPainter, cx: float, cy: float, dnd: bool = False):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(16, 18, 24, 255))
        p.drawEllipse(QPointF(cx, cy), 4.8, 4.8)

        p.setBrush(QColor(18, 32, 58, 255))
        p.drawEllipse(QPointF(cx, cy), 2.6, 2.6)

        if dnd:
            p.setBrush(cls.C_AMBER)
            p.drawEllipse(QPointF(cx, cy), 2.0, 2.0)
        else:
            p.setBrush(QColor(90, 175, 255, 190))
            p.drawEllipse(QPointF(cx - 0.9, cy - 0.9), 1.0, 1.0)

    @classmethod
    def draw_battery_icon(
        cls,
        p: QPainter,
        x: float,
        y: float,
        w: float,
        h: float,
        bat: BatteryState,
    ):
        is_green = bat.is_full or bat.is_charging or bat.is_plugged_idle
        if is_green:
            main_col = cls.C_GREEN
            track_col = QColor(52, 211, 153, 65)
        elif bat.is_low:
            main_col = cls.C_AMBER
            track_col = QColor(251, 191, 36, 65)
        else:
            main_col = cls.C_WHITE
            track_col = QColor(245, 247, 250, 65)

        body_w = w - 3.0
        p.setPen(QPen(main_col, 1.15))
        p.setBrush(track_col)
        p.drawRoundedRect(QRectF(x, y, body_w, h), 2.8, 2.8)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(main_col)
        nub_h = h * 0.42
        p.drawRoundedRect(QRectF(x + body_w + 1.0, y + (h - nub_h) / 2.0, 1.8, nub_h), 0.9, 0.9)

        pad = 1.8
        avail_w = max(0.0, body_w - pad * 2.0)
        fill_w = max(1.5, avail_w * (bat.capacity / 100.0))
        p.setBrush(main_col)
        p.drawRoundedRect(QRectF(x + pad, y + pad, fill_w, h - pad * 2.0), 1.6, 1.6)

        cx = x + body_w / 2.0
        cy = y + h / 2.0
        if bat.is_full:
            pen = QPen(cls.C_BLACK, 1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            chk = QPainterPath()
            chk.moveTo(cx - 3.0, cy + 0.1)
            chk.lineTo(cx - 0.8, cy + 2.2)
            chk.lineTo(cx + 3.2, cy - 2.0)
            p.drawPath(chk)
        elif bat.is_charging or bat.is_plugged_idle:
            bolt = QPainterPath()
            bolt.moveTo(cx + 0.8, y + 1.0)
            bolt.lineTo(cx - 2.8, cy + 0.4)
            bolt.lineTo(cx - 0.2, cy + 0.4)
            bolt.lineTo(cx - 1.0, y + h - 1.0)
            bolt.lineTo(cx + 2.8, cy - 0.4)
            bolt.lineTo(cx + 0.2, cy - 0.4)
            bolt.closeSubpath()
            p.setPen(QPen(cls.C_BLACK, 0.7))
            p.setBrush(cls.C_WHITE if bat.capacity < 50 else cls.C_BLACK)
            p.drawPath(bolt)

    @classmethod
    def draw_notification_icon(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        size: float = 12.0,
        color: QColor = QColor(56, 189, 248),
    ):
        """Crisp solid bell notification icon matching standard minimalist silhouette."""
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)

        top_y = cy - size / 2.0

        # 1. Main Bell Body with Top Knob and Flared Skirt
        kw = size * 0.145
        kh = size * 0.065
        body = QPainterPath()

        # Top Knob
        body.moveTo(cx + kw / 2.0, top_y + kh)
        body.arcTo(QRectF(cx - kw / 2.0, top_y, kw, kh * 2.0), 0, 180)

        # Left Shoulder & Waist
        body.cubicTo(
            cx - kw / 2.0 - size * 0.04, top_y + kh + size * 0.03,
            cx - size * 0.315, top_y + size * 0.17,
            cx - size * 0.315, top_y + size * 0.36,
        )
        body.lineTo(cx - size * 0.315, top_y + size * 0.52)

        # Left Flare to Bottom Corner
        body.cubicTo(
            cx - size * 0.315, top_y + size * 0.66,
            cx - size * 0.415, top_y + size * 0.72,
            cx - size * 0.415, top_y + size * 0.82,
        )
        # Rounded Bottom-Left Corner & Base Rim
        body.quadTo(
            cx - size * 0.415, top_y + size * 0.845,
            cx - size * 0.365, top_y + size * 0.845,
        )
        body.lineTo(cx + size * 0.365, top_y + size * 0.845)
        # Rounded Bottom-Right Corner
        body.quadTo(
            cx + size * 0.415, top_y + size * 0.845,
            cx + size * 0.415, top_y + size * 0.82,
        )
        # Right Flare up to Waist & Shoulder
        body.cubicTo(
            cx + size * 0.415, top_y + size * 0.72,
            cx + size * 0.315, top_y + size * 0.66,
            cx + size * 0.315, top_y + size * 0.52,
        )
        body.lineTo(cx + size * 0.315, top_y + size * 0.36)
        body.cubicTo(
            cx + size * 0.315, top_y + size * 0.17,
            cx + kw / 2.0 + size * 0.04, top_y + kh + size * 0.03,
            cx + kw / 2.0, top_y + kh,
        )
        body.closeSubpath()
        p.drawPath(body)

        # 2. Clapper (bandul lonceng) hanging centered below the base rim
        cw = size * 0.20
        ch = size * 0.105
        clap_y = top_y + size * 0.89
        clapper = QPainterPath()
        clapper.moveTo(cx - cw / 2.0, clap_y)
        clapper.lineTo(cx + cw / 2.0, clap_y)
        clapper.arcTo(QRectF(cx - cw / 2.0, clap_y - ch, cw, ch * 2.0), 0, -180)
        clapper.closeSubpath()
        p.drawPath(clapper)

        p.restore()

    @classmethod
    def draw_bell(
        cls,
        p: QPainter,
        x: float,
        y: float,
        size: float = 11.0,
        color: QColor = QColor(56, 189, 248),
    ):
        cls.draw_notification_icon(p, x + size / 2.0, y + size / 2.0, size=size, color=color)

    @classmethod
    def draw_lightning_bolt(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        size: float = 8.0,
        color: QColor = QColor(52, 211, 153),
    ):
        """Crisp centered lightning bolt symbol."""
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)

        bolt = QPainterPath()
        bolt.moveTo(cx + 0.6, cy - size / 2.0)
        bolt.lineTo(cx - size * 0.35, cy + 0.2)
        bolt.lineTo(cx - 0.2, cy + 0.2)
        bolt.lineTo(cx - 0.6, cy + size / 2.0)
        bolt.lineTo(cx + size * 0.35, cy - 0.2)
        bolt.lineTo(cx + 0.2, cy - 0.2)
        bolt.closeSubpath()

        p.drawPath(bolt)
        p.restore()

    @classmethod
    def draw_battery_donut(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        battery: BatteryState,
        size: float = 17.0,
    ):
        """Donut-shaped battery gauge with percentage or charging bolt inside."""
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        cap = max(0, min(100, battery.capacity))
        is_charging = battery.is_charging or (battery.ac_online and not battery.is_full and cap < 99)
        is_full = battery.is_full or (battery.ac_online and cap >= 99)
        is_low = battery.is_low

        if is_full or is_charging or battery.ac_online:
            accent_col = cls.C_GREEN
            bg_ring_col = QColor(52, 211, 153, 40)
        elif is_low:
            accent_col = cls.C_RED
            bg_ring_col = QColor(248, 113, 113, 40)
        elif cap <= 30:
            accent_col = cls.C_AMBER
            bg_ring_col = QColor(251, 191, 36, 40)
        else:
            accent_col = cls.C_WHITE
            bg_ring_col = QColor(255, 255, 255, 35)

        r = size / 2.0
        ring_thickness = 2.0
        rect = QRectF(
            cx - r + ring_thickness / 2.0,
            cy - r + ring_thickness / 2.0,
            size - ring_thickness,
            size - ring_thickness,
        )

        # 1. Background ring track
        p.setPen(QPen(bg_ring_col, ring_thickness, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(rect)

        # 2. Foreground progress arc
        if cap > 0:
            p.setPen(QPen(accent_col, ring_thickness, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            start_angle = 90 * 16
            span_angle = -int((cap / 100.0) * 360 * 16)
            p.drawArc(rect, start_angle, span_angle)

        # 3. Donut Center: alternate between lightning bolt & percentage number if charging
        show_bolt = False
        if is_charging:
            # Alternates every 2 seconds
            show_bolt = ((int(time.time()) // 2) % 2 == 0)

        if show_bolt:
            cls.draw_lightning_bolt(p, cx, cy, size=7.8, color=accent_col)
        else:
            f_num = QFont("Sans", 6, QFont.Weight.Bold)
            f_num.setPixelSize(7 if cap < 100 else 6)
            p.setFont(f_num)
            p.setPen(accent_col)
            p.drawText(
                QRectF(cx - r, cy - r + 0.5, size, size),
                int(Qt.AlignmentFlag.AlignCenter),
                str(cap),
            )

        p.restore()

    @classmethod
    def draw_crescent_moon(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        size: float = 12.0,
        color: QColor = QColor(52, 211, 153),
    ):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)

        r = size * 0.52
        outer = QPainterPath()
        outer.addEllipse(QPointF(cx, cy), r, r)

        inner = QPainterPath()
        inner.addEllipse(QPointF(cx + r * 0.50, cy - r * 0.18), r * 0.82, r * 0.82)

        crescent = outer.subtracted(inner)
        p.drawPath(crescent)
        p.restore()

    draw_mosque = draw_crescent_moon

    @classmethod
    def _make_cloud_path(cls, cx: float, cy: float, r: float) -> QPainterPath:
        p_left = QPainterPath()
        p_left.addEllipse(QPointF(cx - r * 0.95, cy + r * 0.483), r * 0.50, r * 0.50)

        p_main = QPainterPath()
        p_main.addEllipse(QPointF(cx - r * 0.15, cy - r * 0.167), r * 0.817, r * 0.817)

        p_right = QPainterPath()
        p_right.addEllipse(QPointF(cx + r * 0.757, cy + r * 0.29), r * 0.693, r * 0.693)

        p_base = QPainterPath()
        cx_l = cx - r * 0.95
        cx_r = cx + r * 0.757
        top_b = cy + r * 0.20
        bot_b = cy + r * 0.983
        p_base.addRect(QRectF(cx_l, top_b, cx_r - cx_l, bot_b - top_b))

        return p_left.united(p_main).united(p_right).united(p_base)

    @classmethod
    def draw_weather_icon(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        code: int = 1,
        size: float = 14.0,
    ):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = size / 2.0
        c_sun = QColor(251, 191, 36)         # Amber-400
        c_rain = QColor(56, 189, 248)        # Sky-400

        c_grad_light = QLinearGradient(cx, cy - r, cx, cy + r)
        c_grad_light.setColorAt(0.0, QColor(255, 255, 255))
        c_grad_light.setColorAt(1.0, QColor(215, 225, 240))

        c_grad_dark = QLinearGradient(cx, cy - r, cx, cy + r)
        c_grad_dark.setColorAt(0.0, QColor(205, 215, 228))
        c_grad_dark.setColorAt(1.0, QColor(145, 160, 180))

        if code == 0:
            # Clear Sky / Sun
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c_sun)
            p.drawEllipse(QPointF(cx, cy), r * 0.78, r * 0.78)
        elif code in (1, 2):
            # Partly cloudy: Sun behind + Fluffy Cloud in front
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c_sun)
            p.drawEllipse(QPointF(cx + r * 0.40, cy - r * 0.35), r * 0.46, r * 0.46)
            p.setBrush(c_grad_light)
            cloud = cls._make_cloud_path(cx - r * 0.08, cy + r * 0.16, r * 0.72)
            p.drawPath(cloud)
        elif code in (3, 45, 48):
            # Overcast / Cloud
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c_grad_light)
            cloud = cls._make_cloud_path(cx, cy, r * 0.82)
            p.drawPath(cloud)
        elif code in (51, 53, 55, 61, 63, 65, 80, 81, 82):
            # Rain
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c_grad_dark)
            p.drawPath(cls._make_cloud_path(cx, cy - r * 0.22, r * 0.75))
            drop_pen = QPen(c_rain, max(1.5, size * 0.08))
            drop_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(drop_pen)
            p.drawLine(QPointF(cx - r * 0.35, cy + r * 0.48), QPointF(cx - r * 0.45, cy + r * 0.82))
            p.drawLine(QPointF(cx, cy + r * 0.48), QPointF(cx - r * 0.10, cy + r * 0.82))
            p.drawLine(QPointF(cx + r * 0.35, cy + r * 0.48), QPointF(cx + r * 0.25, cy + r * 0.82))
        elif code in (95, 96, 99):
            # Thunderstorm
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c_grad_dark)
            p.drawPath(cls._make_cloud_path(cx, cy - r * 0.18, r * 0.75))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c_sun)
            bolt = QPainterPath()
            bolt.moveTo(cx + r * 0.10, cy + r * 0.10)
            bolt.lineTo(cx - r * 0.14, cy + r * 0.48)
            bolt.lineTo(cx + r * 0.06, cy + r * 0.48)
            bolt.lineTo(cx - r * 0.10, cy + r * 0.86)
            bolt.lineTo(cx + r * 0.24, cy + r * 0.36)
            bolt.lineTo(cx + r * 0.02, cy + r * 0.36)
            bolt.closeSubpath()
            p.drawPath(bolt)
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c_grad_light)
            p.drawPath(cls._make_cloud_path(cx, cy, r * 0.80))
        p.restore()

    @classmethod
    def draw_blinking_eyes(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        hovered: bool = False,
        is_music: bool = False,
        idle_sec: float = 0.0,
        is_typing: bool = False,
        is_angry: bool = False,
        is_notif: bool = False,
        cursor_gaze: Tuple[float, float] = (0.0, 0.0),
        color: QColor = QColor(245, 247, 250),
    ):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        now_sec = time.time()
        spacing = 14.0
        eye_w = 6.8
        eye_h = 10.0
        r = 3.4

        # 1. ANGRY (Marah): Slanted red-orange sharp eyes with vibration shake
        if is_angry:
            c_angry = QColor(239, 68, 68)  # Red 500
            sh_x = math.sin(now_sec * 32.0) * 0.75
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c_angry)

            # Left slanted eye (\)
            pl = QPainterPath()
            pl.moveTo(cx - spacing / 2.0 - 4.5 + sh_x, cy - 4.5)
            pl.lineTo(cx - spacing / 2.0 + 4.2 + sh_x, cy - 0.5)
            pl.lineTo(cx - spacing / 2.0 + 3.8 + sh_x, cy + 4.5)
            pl.lineTo(cx - spacing / 2.0 - 4.0 + sh_x, cy + 2.5)
            pl.closeSubpath()
            p.drawPath(pl)

            # Right slanted eye (/)
            pr = QPainterPath()
            pr.moveTo(cx + spacing / 2.0 + 4.5 + sh_x, cy - 4.5)
            pr.lineTo(cx + spacing / 2.0 - 4.2 + sh_x, cy - 0.5)
            pr.lineTo(cx + spacing / 2.0 - 3.8 + sh_x, cy + 4.5)
            pr.lineTo(cx + spacing / 2.0 + 4.0 + sh_x, cy + 2.5)
            pr.closeSubpath()
            p.drawPath(pr)
            p.restore()
            return

        # 2. HOVER (Senang / Curious): Smiling green arcs (^  ^)
        if hovered:
            pen = QPen(cls.C_GREEN, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            for ex in [cx - spacing / 2.0, cx + spacing / 2.0]:
                path = QPainterPath()
                path.moveTo(ex - 3.6, cy + 1.4)
                path.quadTo(ex, cy - 3.4, ex + 3.6, cy + 1.4)
                p.drawPath(path)
            p.restore()
            return

        # 3. NOTIFICATION ALERT (Ekspresi Ada Notif Masuk: Mata penuh putih bersinar / full white wide alert eyes)
        if is_notif:
            notif_sp = 13.6
            notif_w = 7.4
            notif_h = 11.4
            notif_r = 3.7

            # Gentle alert bounce
            bounce_y = math.sin(now_sec * 5.2) * 0.65

            # Periodic natural blink every 3.4s
            cycle = now_sec % 3.4
            is_blink = cycle < 0.14
            cur_h = 1.8 if is_blink else notif_h
            cur_r = min(notif_r, cur_h / 2.0)

            p.setPen(Qt.PenStyle.NoPen)
            for ex in [cx - notif_sp / 2.0, cx + notif_sp / 2.0]:
                # Soft full-white outer glow
                if not is_blink:
                    p.setBrush(QColor(255, 255, 255, 42))
                    glow_rect = QRectF(
                        ex - (notif_w + 3.2) / 2.0,
                        cy + bounce_y - (cur_h + 3.2) / 2.0,
                        notif_w + 3.2,
                        cur_h + 3.2,
                    )
                    p.drawRoundedRect(glow_rect, cur_r + 1.6, cur_r + 1.6)

                # Pure solid full-white eye (no dark pupil)
                p.setBrush(QColor(255, 255, 255))
                rect = QRectF(ex - notif_w / 2.0, cy + bounce_y - cur_h / 2.0, notif_w, cur_h)
                p.drawRoundedRect(rect, cur_r, cur_r)

            p.restore()
            return

        # 4. MUSIC (Grooving beat): Happy bouncing smiling curves (◠  ◠)
        if is_music:
            bounce_y = math.sin(now_sec * 6.5) * 1.4
            pen = QPen(cls.C_GREEN, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            for ex in [cx - spacing / 2.0, cx + spacing / 2.0]:
                path = QPainterPath()
                path.moveTo(ex - 3.8, cy + bounce_y + 1.2)
                path.quadTo(ex, cy + bounce_y - 3.0, ex + 3.8, cy + bounce_y + 1.2)
                p.drawPath(path)
            p.restore()
            return

        # 4. ASLEEP (Tidur pulas dengan zzZZ melayang terus)
        if idle_sec >= 90.0:
            breath_y = math.sin(now_sec * 1.4) * 0.6
            pen = QPen(QColor(148, 163, 184), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            for ex in [cx - spacing / 2.0, cx + spacing / 2.0]:
                path = QPainterPath()
                path.moveTo(ex - 3.6, cy + breath_y + 1.2)
                path.quadTo(ex, cy + breath_y + 3.2, ex + 3.6, cy + breath_y + 1.2)
                p.drawPath(path)

            # Floating animated z z Z Z stream
            f_z = QFont("Sans", 7, QFont.Weight.Bold)
            z_items = [("z", 7, 0.0), ("z", 8, 0.8), ("Z", 10, 1.6)]
            for ch, sz, phase in z_items:
                f_z.setPixelSize(sz)
                p.setFont(f_z)
                progress = ((now_sec + phase) % 2.4) / 2.4
                zx = cx + spacing / 2.0 + 7.5 + progress * 14.0 + math.sin(progress * 4.0) * 2.5
                zy = cy - 2.0 - progress * 14.0
                alpha = int(255 * (1.0 - progress) * min(1.0, progress * 4.0))
                p.setPen(QColor(167, 139, 250, max(0, min(255, alpha))))  # Violet 400
                p.drawText(QPointF(zx, zy), ch)
            p.restore()
            return

        # 5. SLEEPY / DROWSY (Ngantuk sayu)
        if idle_sec >= 40.0:
            cycle = now_sec % 4.5
            is_blink = cycle < 0.35
            cur_h = 1.6 if is_blink else 3.8
            cur_r = min(r, cur_h / 2.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(185, 198, 215))
            for ex in [cx - spacing / 2.0, cx + spacing / 2.0]:
                rect = QRectF(ex - eye_w / 2.0, cy - cur_h / 2.0 + 1.4, eye_w, cur_h)
                p.drawRoundedRect(rect, cur_r, cur_r)
            p.restore()
            return

        # 6. TYPING (Mengetik): Sky blue scanning eyes with white pupil shine and energetic bounce
        if is_typing:
            scan_x = math.sin(now_sec * 14.0) * 1.8
            bounce_y = math.sin(now_sec * 18.0) * 0.8
            c_type = QColor(56, 189, 248)  # Sky 400
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c_type)
            for ex in [cx - spacing / 2.0, cx + spacing / 2.0]:
                rect = QRectF(ex - 4.0 + scan_x, cy - 5.5 + bounce_y, 8.0, 11.0)
                p.drawRoundedRect(rect, 4.0, 4.0)
                # Inner white pupil shine
                p.setBrush(QColor(255, 255, 255))
                p.drawEllipse(QPointF(ex + scan_x * 0.7, cy - 1.8 + bounce_y), 1.6, 1.6)
                p.setBrush(c_type)
            p.restore()
            return

        # 7. NORMAL LIVELY (Mengikuti kursor mouse)
        gx, gy = cursor_gaze
        cycle = now_sec % 3.6
        is_blink = cycle < 0.18
        blink_ratio = math.sin((cycle / 0.18) * math.pi) if is_blink else 0.0
        cur_h = max(1.5, eye_h * (1.0 - blink_ratio * 0.88))
        cur_r = min(r, cur_h / 2.0)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        for ex in [cx - spacing / 2.0, cx + spacing / 2.0]:
            rect = QRectF(ex + gx - eye_w / 2.0, cy + gy - cur_h / 2.0, eye_w, cur_h)
            p.drawRoundedRect(rect, cur_r, cur_r)

        p.restore()

    @classmethod
    def draw_star(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        size: float = 12.0,
        color: QColor = QColor(140, 140, 145),
    ):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(QPen(color, 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        star = QPainterPath()
        r_out = size * 0.5
        r_in = r_out * 0.42
        for i in range(10):
            angle = i * (math.pi / 5.0) - math.pi / 2.0
            r = r_out if i % 2 == 0 else r_in
            px = cx + r * math.cos(angle)
            py = cy + r * math.sin(angle)
            if i == 0:
                star.moveTo(px, py)
            else:
                star.lineTo(px, py)
        star.closeSubpath()
        p.drawPath(star)
        p.restore()

    @classmethod
    def draw_speaker(
        cls,
        p: QPainter,
        x: float,
        cy: float,
        muted: bool = False,
        color: QColor = QColor(245, 247, 250),
    ):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)

        path = QPainterPath()
        path.moveTo(x, cy - 2.5)
        path.lineTo(x + 3.0, cy - 2.5)
        path.lineTo(x + 7.0, cy - 5.5)
        path.lineTo(x + 7.0, cy + 5.5)
        path.lineTo(x + 3.0, cy + 2.5)
        path.lineTo(x, cy + 2.5)
        path.closeSubpath()
        p.drawPath(path)

        if muted:
            pen = QPen(cls.C_RED, 1.5)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawLine(QPointF(x + 9.0, cy - 3.5), QPointF(x + 15.0, cy + 3.5))
            p.drawLine(QPointF(x + 15.0, cy - 3.5), QPointF(x + 9.0, cy + 3.5))
        else:
            pen = QPen(color, 1.4)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawArc(QRectF(x + 6.0, cy - 4.5, 5.0, 9.0), -60 * 16, 120 * 16)
            p.drawArc(QRectF(x + 8.5, cy - 6.5, 7.0, 13.0), -60 * 16, 120 * 16)
        p.restore()

    @classmethod
    def draw_mic(
        cls,
        p: QPainter,
        x: float,
        cy: float,
        muted: bool = False,
        color: QColor = QColor(245, 247, 250),
    ):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)

        p.drawRoundedRect(QRectF(x + 3.0, cy - 5.5, 5.0, 8.5), 2.5, 2.5)
        pen = QPen(color, 1.3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(QRectF(x + 1.0, cy - 4.0, 9.0, 8.0), 0, -180 * 16)
        p.drawLine(QPointF(x + 5.5, cy + 4.0), QPointF(x + 5.5, cy + 6.5))
        p.drawLine(QPointF(x + 3.0, cy + 6.5), QPointF(x + 8.0, cy + 6.5))

        if muted:
            pen_m = QPen(cls.C_RED, 1.6)
            pen_m.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen_m)
            p.drawLine(QPointF(x + 1.0, cy + 5.5), QPointF(x + 10.0, cy - 5.5))
        p.restore()

    @classmethod
    def draw_sun(
        cls,
        p: QPainter,
        x: float,
        cy: float,
        color: QColor = QColor(251, 191, 36),
    ):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        cx = x + 6.0
        p.drawEllipse(QPointF(cx, cy), 3.2, 3.2)

        pen = QPen(color, 1.3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        for i in range(8):
            ang = i * math.pi / 4.0
            r1 = 4.8
            r2 = 6.6
            p.drawLine(
                QPointF(cx + r1 * math.cos(ang), cy + r1 * math.sin(ang)),
                QPointF(cx + r2 * math.cos(ang), cy + r2 * math.sin(ang)),
            )
        p.restore()

    @classmethod
    def draw_bluetooth(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        size: float = 13.0,
        color: QColor = QColor(56, 189, 248),
    ):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(color, 1.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        h = size
        w = size * 0.35
        path = QPainterPath()
        path.moveTo(cx - w, cy - h * 0.28)
        path.lineTo(cx + w, cy + h * 0.28)
        path.lineTo(cx, cy + h * 0.5)
        path.lineTo(cx, cy - h * 0.5)
        path.lineTo(cx + w, cy - h * 0.28)
        path.lineTo(cx - w, cy + h * 0.28)
        p.drawPath(path)
        p.restore()

    @classmethod
    def draw_gear(
        cls,
        p: QPainter,
        gcx: float,
        gcy: float,
        radius: float = 5.6,
        color: QColor = QColor(245, 247, 250),
    ):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        path_g = QPainterPath()
        teeth = 6
        r_outer = radius
        r_inner = radius * 0.72
        hole_r = radius * 0.35
        step = 2 * math.pi / teeth
        w_tooth = step * 0.26
        for i in range(teeth):
            a_mid = i * step
            a1 = a_mid - w_tooth * 1.1
            a2 = a_mid - w_tooth * 0.7
            a3 = a_mid + w_tooth * 0.7
            a4 = a_mid + w_tooth * 1.1
            p1 = QPointF(gcx + r_inner * math.cos(a1), gcy + r_inner * math.sin(a1))
            p2 = QPointF(gcx + r_outer * math.cos(a2), gcy + r_outer * math.sin(a2))
            p3 = QPointF(gcx + r_outer * math.cos(a3), gcy + r_outer * math.sin(a3))
            p4 = QPointF(gcx + r_inner * math.cos(a4), gcy + r_inner * math.sin(a4))
            if i == 0:
                path_g.moveTo(p1)
            else:
                path_g.lineTo(p1)
            path_g.lineTo(p2)
            path_g.lineTo(p3)
            path_g.lineTo(p4)
        path_g.closeSubpath()
        hole = QPainterPath()
        hole.addEllipse(QPointF(gcx, gcy), hole_r, hole_r)
        p.drawPath(path_g.subtracted(hole))
        p.restore()

    @classmethod
    def draw_equalizer(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        t: float = 0.0,
        color: QColor = QColor(52, 211, 153),
        spectrum: Optional[List[float]] = None,
        max_h: float = 12.0,
        min_h: float = 3.0,
    ):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        bars = 4
        bar_w = 2.2
        gap = 2.0
        total_w = bars * bar_w + (bars - 1) * gap
        start_x = cx - total_w / 2.0
        freqs = (9.0, 13.0, 8.0, 11.0)
        phases = (0.0, 1.4, 2.8, 4.2)
        use_real = spectrum is not None and len(spectrum) >= bars
        for i in range(bars):
            if use_real:
                level = max(0.0, min(1.0, float(spectrum[i])))
            else:
                level = 0.5 + 0.5 * math.sin(t * freqs[i] + phases[i])
            h_val = min_h + (max_h - min_h) * level
            bx = start_x + i * (bar_w + gap)
            by = cy - h_val / 2.0
            p.drawRoundedRect(QRectF(bx, by, bar_w, h_val), 1.0, 1.0)
        p.restore()

    @classmethod
    def draw_music_note(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        size: float = 12.0,
        color: QColor = QColor(245, 247, 250),
    ):
        """Crisp solid beamed double eighth note (quavers) icon matching the classic minimalist silhouette."""
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)

        s = size / 128.0

        # 1. Left and Right circular noteheads
        path = QPainterPath()
        r = 22.5 * s
        path.addEllipse(QPointF(cx - 40.0 * s, cy + 41.0 * s), r, r)
        path.addEllipse(QPointF(cx + 40.0 * s, cy + 29.0 * s), r, r)

        # 2. Beamed Stems
        frame = QPainterPath()
        # Left stem outer edge starting at left notehead
        frame.moveTo(cx - 28.5 * s, cy + 41.0 * s)
        frame.lineTo(cx - 28.5 * s, cy - 45.5 * s)
        # Rounded top-left corner of beam
        frame.quadTo(cx - 28.5 * s, cy - 50.5 * s, cx - 24.5 * s, cy - 51.5 * s)
        # Slanted beam top edge
        frame.lineTo(cx + 58.5 * s, cy - 63.5 * s)
        # Rounded top-right corner of beam
        frame.quadTo(cx + 62.5 * s, cy - 64.0 * s, cx + 62.5 * s, cy - 59.5 * s)
        # Right stem outer edge down to right notehead
        frame.lineTo(cx + 62.5 * s, cy + 29.0 * s)
        # Right stem inner edge
        frame.lineTo(cx + 51.5 * s, cy + 29.0 * s)
        # Up to beam inner bottom
        frame.lineTo(cx + 51.5 * s, cy - 29.5 * s)
        # Slanted beam inner bottom edge
        frame.lineTo(cx - 17.5 * s, cy - 16.5 * s)
        # Left stem inner edge down to left notehead
        frame.lineTo(cx - 17.5 * s, cy + 41.0 * s)
        frame.closeSubpath()

        path.addPath(frame)
        p.drawPath(path)
        p.restore()

    @classmethod
    def draw_play_pause(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        playing: bool = True,
        size: float = 14.0,
        color: QColor = QColor(0, 0, 0),
    ):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        if playing:
            bw = size * 0.22
            bh = size * 0.75
            gap = size * 0.22
            p.drawRoundedRect(QRectF(cx - gap / 2.0 - bw, cy - bh / 2.0, bw, bh), 1.2, 1.2)
            p.drawRoundedRect(QRectF(cx + gap / 2.0, cy - bh / 2.0, bw, bh), 1.2, 1.2)
        else:
            tri = QPainterPath()
            r = size * 0.5
            tri.moveTo(cx - r * 0.6, cy - r * 0.8)
            tri.lineTo(cx + r * 0.9, cy)
            tri.lineTo(cx - r * 0.6, cy + r * 0.8)
            tri.closeSubpath()
            p.drawPath(tri)
        p.restore()

    @classmethod
    def draw_prev_next(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        is_next: bool = True,
        size: float = 12.0,
        color: QColor = QColor(245, 247, 250),
    ):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        sign = 1.0 if is_next else -1.0
        r = size * 0.45
        tri = QPainterPath()
        tri.moveTo(cx - sign * r * 0.6, cy - r * 0.75)
        tri.lineTo(cx + sign * r * 0.5, cy)
        tri.lineTo(cx - sign * r * 0.6, cy + r * 0.75)
        tri.closeSubpath()
        p.drawPath(tri)

        bar_w = 1.8
        bar_h = size * 0.75
        bar_x = cx + sign * (r * 0.5 + 2.0) if is_next else cx - (r * 0.5 + 2.0 + bar_w)
        p.drawRoundedRect(QRectF(bar_x, cy - bar_h / 2.0, bar_w, bar_h), 0.8, 0.8)
        p.restore()

    @classmethod
    def draw_localsend_icon(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        size: float = 12.0,
        color: QColor = QColor(6, 182, 212),
    ):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(color, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

        r = size * 0.48
        # Modern paper plane / send icon pointing top-right
        path = QPainterPath()
        path.moveTo(cx + r * 0.95, cy - r * 0.95)
        path.lineTo(cx - r * 0.90, cy - r * 0.15)
        path.lineTo(cx - r * 0.15, cy + r * 0.20)
        path.lineTo(cx + r * 0.20, cy + r * 0.90)
        path.closeSubpath()
        p.drawPath(path)

        # Center spine fold
        p.drawLine(QPointF(cx - r * 0.15, cy + r * 0.20), QPointF(cx + r * 0.95, cy - r * 0.95))
        p.restore()

    @classmethod
    def draw_control_center_icon(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        size: float = 12.0,
        color: QColor = QColor(245, 247, 250),
    ):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(color, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        hw = size * 0.44
        yt = cy - size * 0.22
        yb = cy + size * 0.22
        p.drawLine(QPointF(cx - hw, yt), QPointF(cx + hw, yt))
        p.drawLine(QPointF(cx - hw, yb), QPointF(cx + hw, yb))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        rk = size * 0.16
        p.drawEllipse(QPointF(cx + hw * 0.35, yt), rk, rk)
        p.drawEllipse(QPointF(cx - hw * 0.35, yb), rk, rk)
        p.restore()

    @classmethod
    def draw_finance_icon(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        size: float = 12.0,
        color: QColor = QColor(52, 211, 153),
    ):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(color, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        r = size / 2.0
        path = QPainterPath()
        path.moveTo(cx - r * 0.8, cy + r * 0.4)
        path.lineTo(cx - r * 0.25, cy + r * 0.05)
        path.lineTo(cx + r * 0.15, cy + r * 0.45)
        path.lineTo(cx + r * 0.85, cy - r * 0.65)
        p.drawPath(path)
        p.drawLine(QPointF(cx + r * 0.85, cy - r * 0.65), QPointF(cx + r * 0.45, cy - r * 0.65))
        p.drawLine(QPointF(cx + r * 0.85, cy - r * 0.65), QPointF(cx + r * 0.85, cy - r * 0.25))
        p.restore()

    @classmethod
    def _draw_sparkline(
        cls,
        p: QPainter,
        rect: QRectF,
        values: List[float],
        color: QColor = QColor(52, 211, 153),
    ):
        if len(values) < 2:
            return
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        min_v = min(values)
        max_v = max(values)
        rng = max_v - min_v if max_v != min_v else 1.0

        pts = []
        n = len(values)
        for i, val in enumerate(values):
            x = rect.left() + (i / float(n - 1)) * rect.width()
            y = rect.bottom() - ((val - min_v) / rng) * rect.height()
            pts.append(QPointF(x, y))

        fill_path = QPainterPath()
        fill_path.moveTo(pts[0].x(), rect.bottom())
        for pt in pts:
            fill_path.lineTo(pt)
        fill_path.lineTo(pts[-1].x(), rect.bottom())
        fill_path.closeSubpath()

        grad = QLinearGradient(0.0, rect.top(), 0.0, rect.bottom())
        grad.setColorAt(0.0, QColor(color.red(), color.green(), color.blue(), 55))
        grad.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(grad)
        p.drawPath(fill_path)

        line_path = QPainterPath()
        line_path.moveTo(pts[0])
        for pt in pts[1:]:
            line_path.lineTo(pt)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(color, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawPath(line_path)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawEllipse(pts[-1], 2.4, 2.4)
        p.restore()

    @classmethod
    def draw_wifi_icon(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        size: float = 15.0,
        color: QColor = QColor(245, 247, 250),
    ):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

        base_y = cy + size * 0.32
        dot_r = size * 0.08

        # Outer wave
        r3 = size * 0.72
        p.drawArc(QRectF(cx - r3, base_y - r3, r3 * 2.0, r3 * 2.0), 48 * 16, 84 * 16)

        # Middle wave
        r2 = size * 0.46
        p.drawArc(QRectF(cx - r2, base_y - r2, r2 * 2.0, r2 * 2.0), 48 * 16, 84 * 16)

        # Base anchor dot
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawEllipse(QPointF(cx, base_y), dot_r, dot_r)
        p.restore()

    @classmethod
    def draw_chip_icon(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        size: float = 11.0,
        color: QColor = QColor(245, 247, 250),
    ):
        p.save()
        pen = QPen(color, 1.2, Qt.PenStyle.SolidLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        s = size * 0.65
        p.drawRoundedRect(QRectF(cx - s / 2.0, cy - s / 2.0, s, s), 1.5, 1.5)
        for offset in [-s * 0.25, s * 0.25]:
            p.drawLine(QPointF(cx + offset, cy - s / 2.0), QPointF(cx + offset, cy - s / 2.0 - 2.0))
            p.drawLine(QPointF(cx + offset, cy + s / 2.0), QPointF(cx + offset, cy + s / 2.0 + 2.0))
            p.drawLine(QPointF(cx - s / 2.0, cy + offset), QPointF(cx - s / 2.0 - 2.0, cy + offset))
            p.drawLine(QPointF(cx + s / 2.0, cy + offset), QPointF(cx + s / 2.0 + 2.0, cy + offset))
        p.restore()

    @classmethod
    def draw_ram_icon(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        size: float = 11.0,
        color: QColor = QColor(245, 247, 250),
    ):
        p.save()
        pen = QPen(color, 1.2, Qt.PenStyle.SolidLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        w_m, h_m = size * 0.85, size * 0.45
        p.drawRoundedRect(QRectF(cx - w_m / 2.0, cy - h_m / 2.0, w_m, h_m), 1.0, 1.0)
        p.drawLine(QPointF(cx - w_m * 0.3, cy + h_m / 2.0), QPointF(cx - w_m * 0.3, cy + h_m / 2.0 + 1.5))
        p.drawLine(QPointF(cx, cy + h_m / 2.0), QPointF(cx, cy + h_m / 2.0 + 1.5))
        p.drawLine(QPointF(cx + w_m * 0.3, cy + h_m / 2.0), QPointF(cx + w_m * 0.3, cy + h_m / 2.0 + 1.5))
        p.restore()

    @classmethod
    def draw_thermometer_icon(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        size: float = 11.0,
        color: QColor = QColor(245, 247, 250),
    ):
        p.save()
        p.setPen(QPen(color, 1.2, Qt.PenStyle.SolidLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy + size * 0.22), size * 0.2, size * 0.2)
        p.drawLine(QPointF(cx - size * 0.1, cy + size * 0.05), QPointF(cx - size * 0.1, cy - size * 0.35))
        p.drawLine(QPointF(cx + size * 0.1, cy + size * 0.05), QPointF(cx + size * 0.1, cy - size * 0.35))
        p.drawArc(QRectF(cx - size * 0.1, cy - size * 0.45, size * 0.2, size * 0.2), 0, 180 * 16)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawEllipse(QPointF(cx, cy + size * 0.22), size * 0.12, size * 0.12)
        p.restore()

    @classmethod
    def draw_stretch_icon(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        size: float = 13.0,
        color: QColor = QColor(251, 191, 36),
    ):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(color, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(color)
        p.drawEllipse(QPointF(cx, cy - size * 0.35), size * 0.14, size * 0.14)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx, cy - size * 0.2), QPointF(cx, cy + size * 0.15))
        p.drawLine(QPointF(cx - size * 0.32, cy - size * 0.3), QPointF(cx, cy - size * 0.1))
        p.drawLine(QPointF(cx + size * 0.32, cy - size * 0.3), QPointF(cx, cy - size * 0.1))
        p.drawLine(QPointF(cx, cy + size * 0.15), QPointF(cx - size * 0.22, cy + size * 0.45))
        p.drawLine(QPointF(cx, cy + size * 0.15), QPointF(cx + size * 0.22, cy + size * 0.45))
        p.restore()

    @classmethod
    def draw_camera_icon(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        size: float = 12.0,
        color: QColor = QColor(245, 247, 250),
    ):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(color, 1.3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        w_cam = size * 0.82
        h_cam = size * 0.58
        p.drawRoundedRect(QRectF(cx - w_cam / 2.0, cy - h_cam / 2.0 + 1.0, w_cam, h_cam), 2.2, 2.2)
        p.drawRoundedRect(QRectF(cx - 2.8, cy - h_cam / 2.0 - 1.2, 5.6, 2.2), 0.8, 0.8)
        p.drawEllipse(QPointF(cx, cy + 1.0), 2.4, 2.4)
        p.restore()

    @classmethod
    def draw_eyedropper_icon(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        size: float = 12.0,
        color: QColor = QColor(245, 247, 250),
    ):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(color, 1.35, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        r = size * 0.42
        # Diagonal pipette body + tip + bulb
        path = QPainterPath()
        path.moveTo(cx - r * 0.95, cy + r * 0.95)
        path.lineTo(cx - r * 0.55, cy + r * 0.25)
        path.lineTo(cx + r * 0.28, cy - r * 0.58)
        path.lineTo(cx + r * 0.58, cy - r * 0.28)
        path.lineTo(cx - r * 0.25, cy + r * 0.55)
        path.closeSubpath()
        p.drawPath(path)
        # Crossbar + top bulb
        p.drawLine(QPointF(cx + r * 0.05, cy - r * 0.78), QPointF(cx + r * 0.78, cy - r * 0.05))
        p.drawEllipse(QPointF(cx + r * 0.58, cy - r * 0.58), r * 0.34, r * 0.34)
        p.restore()

    @classmethod
    def draw_ocr_icon(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        size: float = 12.0,
        color: QColor = QColor(245, 247, 250),
    ):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(color, 1.35, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        r = size * 0.46
        c_len = r * 0.55
        # 4 corner brackets
        for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            px = cx + sx * r
            py = cy + sy * r
            p.drawLine(QPointF(px, py), QPointF(px - sx * c_len, py))
            p.drawLine(QPointF(px, py), QPointF(px, py - sy * c_len))
        # Inner 'T' for text extraction
        p.drawLine(QPointF(cx - r * 0.42, cy - r * 0.34), QPointF(cx + r * 0.42, cy - r * 0.34))
        p.drawLine(QPointF(cx, cy - r * 0.34), QPointF(cx, cy + r * 0.42))
        p.restore()

    @classmethod
    def draw_lock_icon(
        cls,
        p: QPainter,
        cx: float,
        cy: float,
        size: float = 12.0,
        color: QColor = QColor(245, 247, 250),
    ):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(color, 1.3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        w_body = size * 0.75
        h_body = size * 0.55
        p.drawArc(QRectF(cx - 3.2, cy - size * 0.45, 6.4, 6.8), 0, 180 * 16)
        p.drawRoundedRect(QRectF(cx - w_body / 2.0, cy - size * 0.1, w_body, h_body), 2.2, 2.2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawEllipse(QPointF(cx, cy + size * 0.12), 1.2, 1.2)
        p.restore()

    @classmethod
    def _draw_hud_area(
        cls,
        p: QPainter,
        w: int,
        h: int,
        ear: float,
        hud: HudItem,
    ):
        cy = h / 2.0
        pad_x = ear + 12.0
        right_pad = ear + 12.0

        f_hud = QFont("Sans", 8, QFont.Weight.Bold)
        f_hud.setPixelSize(11)
        p.setFont(f_hud)

        if hud.kind == "volume":
            cls.draw_speaker(p, pad_x, cy, muted=False, color=cls.C_BLUE)
            bar_x = pad_x + 24.0
            bar_w = w - bar_x - right_pad - 36.0
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 38))
            p.drawRoundedRect(QRectF(bar_x, cy - 2.0, bar_w, 4.0), 2.0, 2.0)
            fill_w = max(2.0, bar_w * (hud.value / 100.0))
            p.setBrush(cls.C_BLUE)
            p.drawRoundedRect(QRectF(bar_x, cy - 2.0, fill_w, 4.0), 2.0, 2.0)
            p.setPen(cls.C_WHITE)
            p.drawText(
                QRectF(w - right_pad - 32.0, 0, 32.0, float(h)),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                f"{hud.value}%",
            )
        elif hud.kind == "mute":
            if hud.is_muted:
                cls.draw_speaker(p, pad_x, cy, muted=True, color=cls.C_RED)
                p.setPen(cls.C_WHITE)
                p.drawText(
                    QRectF(pad_x + 22.0, 0, 110.0, float(h)),
                    int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                    "Suara Bisu",
                )
                tag_w = 60.0
                tag_rect = QRectF(w - right_pad - tag_w, 5.0, tag_w, 18.0)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(cls.C_RED_BG)
                p.drawRoundedRect(tag_rect, 5.0, 5.0)
                p.setPen(cls.C_RED)
                p.drawText(tag_rect, int(Qt.AlignmentFlag.AlignCenter), "MUTED")
            else:
                cls.draw_speaker(p, pad_x, cy, muted=False, color=cls.C_GREEN)
                p.setPen(cls.C_WHITE)
                p.drawText(
                    QRectF(pad_x + 22.0, 0, 110.0, float(h)),
                    int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                    f"Suara {hud.value}%",
                )
                tag_w = 46.0
                tag_rect = QRectF(w - right_pad - tag_w, 5.0, tag_w, 18.0)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(cls.C_GREEN_BG)
                p.drawRoundedRect(tag_rect, 5.0, 5.0)
                p.setPen(cls.C_GREEN)
                p.drawText(tag_rect, int(Qt.AlignmentFlag.AlignCenter), "ON")
        elif hud.kind == "mic":
            if hud.is_muted:
                cls.draw_mic(p, pad_x, cy, muted=True, color=cls.C_RED)
                p.setPen(cls.C_WHITE)
                p.drawText(
                    QRectF(pad_x + 20.0, 0, 120.0, float(h)),
                    int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                    "Mikrofon Bisu",
                )
                tag_w = 48.0
                tag_rect = QRectF(w - right_pad - tag_w, 5.0, tag_w, 18.0)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(cls.C_RED_BG)
                p.drawRoundedRect(tag_rect, 5.0, 5.0)
                p.setPen(cls.C_RED)
                p.drawText(tag_rect, int(Qt.AlignmentFlag.AlignCenter), "OFF")
            else:
                cls.draw_mic(p, pad_x, cy, muted=False, color=cls.C_GREEN)
                p.setPen(cls.C_WHITE)
                p.drawText(
                    QRectF(pad_x + 20.0, 0, 120.0, float(h)),
                    int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                    "Mikrofon Aktif",
                )
                tag_w = 46.0
                tag_rect = QRectF(w - right_pad - tag_w, 5.0, tag_w, 18.0)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(cls.C_GREEN_BG)
                p.drawRoundedRect(tag_rect, 5.0, 5.0)
                p.setPen(cls.C_GREEN)
                p.drawText(tag_rect, int(Qt.AlignmentFlag.AlignCenter), "ON")
        elif hud.kind == "brightness":
            cls.draw_sun(p, pad_x, cy, color=cls.C_AMBER)
            bar_x = pad_x + 22.0
            bar_w = w - bar_x - right_pad - 36.0
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 38))
            p.drawRoundedRect(QRectF(bar_x, cy - 2.0, bar_w, 4.0), 2.0, 2.0)
            fill_w = max(2.0, bar_w * (hud.value / 100.0))
            p.setBrush(cls.C_AMBER)
            p.drawRoundedRect(QRectF(bar_x, cy - 2.0, fill_w, 4.0), 2.0, 2.0)
            p.setPen(cls.C_WHITE)
            p.drawText(
                QRectF(w - right_pad - 32.0, 0, 32.0, float(h)),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                f"{hud.value}%",
            )
        elif hud.kind == "bluetooth":
            cls.draw_bluetooth(p, pad_x + 6.0, cy, size=13.0, color=cls.C_BLUE)
            dev_name = hud.extra_text or "Perangkat Bluetooth"
            connected = not hud.is_muted
            tag_w = 72.0 if connected else 66.0
            tag_rect = QRectF(w - right_pad - tag_w, 5.0, tag_w, 18.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(cls.C_GREEN_BG if connected else cls.C_RED_BG)
            p.drawRoundedRect(tag_rect, 5.0, 5.0)
            f_tag = QFont("Sans", 8, QFont.Weight.Bold)
            f_tag.setPixelSize(9)
            p.setFont(f_tag)
            p.setPen(cls.C_GREEN if connected else cls.C_RED)
            p.drawText(
                tag_rect,
                int(Qt.AlignmentFlag.AlignCenter),
                "TERHUBUNG" if connected else "TERPUTUS",
            )

            f_name = QFont("Sans", 9, QFont.Weight.Bold)
            f_name.setPixelSize(11)
            p.setFont(f_name)
            fm = QFontMetricsF(f_name)
            avail_w = tag_rect.left() - (pad_x + 18.0) - 8.0
            elided = fm.elidedText(dev_name, Qt.TextElideMode.ElideRight, avail_w)
            p.setPen(cls.C_WHITE)
            p.drawText(
                QRectF(pad_x + 18.0, 0, avail_w, float(h)),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                elided,
            )
        elif hud.kind == "prayer":
            f_name = QFont("Sans", 9, QFont.Weight.Bold)
            f_name.setPixelSize(11)
            p.setFont(f_name)
            fm = QFontMetricsF(f_name)

            txt = hud.extra_text or "Adzan"
            icon_size = 14.0
            gap = 8.0
            max_content_w = float(w) - 2.0 * (ear + 14.0)

            raw_txt_w = fm.horizontalAdvance(txt)
            if icon_size + gap + raw_txt_w > max_content_w:
                avail_txt_w = max(20.0, max_content_w - (icon_size + gap))
                disp_txt = fm.elidedText(txt, Qt.TextElideMode.ElideRight, avail_txt_w)
                txt_w = fm.horizontalAdvance(disp_txt)
            else:
                disp_txt = txt
                txt_w = raw_txt_w

            total_content_w = icon_size + gap + txt_w
            start_x = (float(w) - total_content_w) / 2.0

            vib_x = math.sin(time.time() * 26.0) * 1.5
            moon_cx = start_x + icon_size / 2.0 + vib_x
            cls.draw_crescent_moon(p, moon_cx, cy, size=icon_size, color=cls.C_GREEN)

            p.setPen(cls.C_WHITE)
            p.drawText(
                QRectF(start_x + icon_size + gap, 0, txt_w + 4.0, float(h)),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                disp_txt,
            )
        elif hud.kind == "localsend":
            cls.draw_localsend_icon(p, pad_x + 6.0, cy, size=14.0, color=cls.C_CYAN)
            msg = hud.extra_text or "LocalSend"
            f_name = QFont("Sans", 9, QFont.Weight.Bold)
            f_name.setPixelSize(11)
            p.setFont(f_name)
            avail_w = float(w) - (pad_x + 22.0) - right_pad
            fm = QFontMetricsF(f_name)
            elided = fm.elidedText(msg, Qt.TextElideMode.ElideRight, avail_w)
            p.setPen(cls.C_WHITE)
            p.drawText(
                QRectF(pad_x + 22.0, 0, avail_w, float(h)),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                elided,
            )
        elif hud.kind == "eye_break":
            now_sec = time.time()
            rem = max(0.0, hud.duration - (now_sec - hud.started_at))
            progress_ratio = max(0.0, min(1.0, rem / max(0.1, hud.duration)))
            top_h = 28.0
            top_cy = top_h / 2.0

            # Soothing closed relaxed smiling eyes ( ◡ ‿ ◡ ) in emerald green
            pen = QPen(QColor(52, 211, 153), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            for ex in [pad_x + 3.0, pad_x + 14.0]:
                ep = QPainterPath()
                ep.moveTo(ex - 3.2, top_cy - 0.5)
                ep.quadTo(ex, top_cy + 2.5, ex + 3.2, top_cy - 0.5)
                p.drawPath(ep)

            f_hud = QFont("Sans", 8, QFont.Weight.Bold)
            f_hud.setPixelSize(11)
            p.setFont(f_hud)

            # Countdown pill badge
            pill_w = 30.0
            pill_rect = QRectF(float(w) - right_pad - pill_w, 5.0, pill_w, 18.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(52, 211, 153, 40))
            p.drawRoundedRect(pill_rect, 5.0, 5.0)
            f_rem = QFont("Sans", 8, QFont.Weight.Bold)
            f_rem.setPixelSize(10)
            p.setFont(f_rem)
            p.setPen(QColor(52, 211, 153))
            p.drawText(pill_rect, int(Qt.AlignmentFlag.AlignCenter), f"{int(math.ceil(rem))}s")

            # Centered Text between left eyes icon and right countdown badge
            p.setFont(f_hud)
            tx_box = QRectF(pad_x + 20.0, 0, (float(w) - right_pad - pill_w - 6.0) - (pad_x + 20.0), top_h)
            if rem > 1.0:
                p.setPen(QColor(255, 255, 255))
                p.drawText(
                    tx_box,
                    int(Qt.AlignmentFlag.AlignCenter),
                    "Istirahat Mata: Tatap 6m",
                )
            else:
                p.setPen(QColor(52, 211, 153))
                p.drawText(
                    tx_box,
                    int(Qt.AlignmentFlag.AlignCenter),
                    "✓ Mata Segar Kembali!",
                )

            # Optional centered quote attached during eye break
            if h > 42 and (hud.extra_text or "").lstrip().startswith("{"):
                q_text, q_author = "", ""
                try:
                    q_obj = json.loads(hud.extra_text)
                    q_text = str(q_obj.get("text", "")).strip()
                    q_author = str(q_obj.get("author", "")).strip()
                except Exception:
                    pass
                if q_text:
                    inner_x = ear + 18.0
                    inner_w = max(80.0, float(w) - 2.0 * inner_x)
                    p.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
                    p.drawLine(QPointF(inner_x, top_h + 1.0), QPointF(float(w) - inner_x, top_h + 1.0))

                    author_h = 18.0 if q_author else 0.0
                    q_top = top_h + 6.0
                    bot_pad = 10.0
                    f_q = QFont("Sans", 9, QFont.Weight.DemiBold)
                    f_q.setPixelSize(12)
                    p.setFont(f_q)
                    p.setPen(QColor(248, 250, 252))
                    q_rect = QRectF(inner_x, q_top, inner_w, max(22.0, float(h) - q_top - bot_pad - author_h - 4.0))
                    p.drawText(
                        q_rect,
                        int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap),
                        f'"{q_text}"',
                    )
                    if q_author:
                        f_auth = QFont("Sans", 8, QFont.Weight.Bold)
                        f_auth.setPixelSize(10)
                        p.setFont(f_auth)
                        p.setPen(QColor(148, 163, 184))
                        a_rect = QRectF(inner_x, float(h) - bot_pad - author_h, inner_w, author_h)
                        p.drawText(
                            a_rect,
                            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                            f"- {q_author}",
                        )

            # Progress bar along bottom edge
            bar_w = float(w) - 2.0 * (ear + 10.0)
            bar_x = ear + 10.0
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 20))
            p.drawRoundedRect(QRectF(bar_x, float(h) - 2.5, bar_w, 1.5), 0.75, 0.75)
            p.setBrush(QColor(52, 211, 153))
            p.drawRoundedRect(QRectF(bar_x, float(h) - 2.5, bar_w * progress_ratio, 1.5), 0.75, 0.75)

        elif hud.kind == "stretch_break":
            cls.draw_stretch_icon(p, pad_x + 6.0, cy, size=13.0, color=QColor(251, 191, 36))

            # Dismiss button [Tutup ✕]
            p_w = 46.0
            p_rect = QRectF(float(w) - right_pad - p_w, 5.0, p_w, 18.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(251, 191, 36, 40))
            p.drawRoundedRect(p_rect, 5.0, 5.0)
            f_btn = QFont("Sans", 7, QFont.Weight.Bold)
            f_btn.setPixelSize(9)
            p.setFont(f_btn)
            p.setPen(QColor(251, 191, 36))
            p.drawText(p_rect, int(Qt.AlignmentFlag.AlignCenter), "Tutup ✕")

            # Centered Text between left stretch icon and right dismiss button
            f_hud = QFont("Sans", 8, QFont.Weight.Bold)
            f_hud.setPixelSize(11)
            p.setFont(f_hud)
            p.setPen(QColor(255, 255, 255))
            tx_box = QRectF(pad_x + 18.0, 0, (float(w) - right_pad - p_w - 6.0) - (pad_x + 18.0), float(h))
            p.drawText(
                tx_box,
                int(Qt.AlignmentFlag.AlignCenter),
                "Waktunya Berdiri & Regang",
            )
            p.setFont(f_btn)
            p.setPen(QColor(251, 191, 36))
            p.drawText(p_rect, int(Qt.AlignmentFlag.AlignCenter), "Tutup ✕")

        elif hud.kind == "power_plug":
            is_plugged = not hud.is_muted
            cap = max(0, min(100, int(hud.value)))
            elapsed = max(0.0, time.time() - hud.started_at)
            ease_out = 1.0 - math.pow(1.0 - min(1.0, elapsed / 0.55), 3.0)

            if is_plugged:
                accent = QColor(34, 197, 94)      # Emerald green
                accent_bright = QColor(74, 222, 128)
            else:
                accent = QColor(239, 68, 68) if cap <= 20 else QColor(245, 158, 11)
                accent_bright = QColor(248, 113, 113) if cap <= 20 else QColor(251, 191, 36)

            # Left animated icon
            icon_cx = pad_x + 8.0
            if is_plugged:
                # Expanding pulse ripple ring on plug-in
                pulse_t = min(1.0, elapsed / 0.75)
                if pulse_t < 1.0:
                    ring_r = 5.0 + pulse_t * 10.0
                    ring_alpha = int(170 * (1.0 - pulse_t))
                    p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), ring_alpha), 1.6))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawEllipse(QPointF(icon_cx, cy), ring_r, ring_r)

                # Soft glowing backdrop + pop scale for lightning bolt
                glow_alpha = int(38 + 20 * math.sin(elapsed * 6.0))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(accent.red(), accent.green(), accent.blue(), glow_alpha))
                p.drawEllipse(QPointF(icon_cx, cy), 9.5, 9.5)

                bolt_scale = 1.0 + 0.28 * math.exp(-elapsed * 6.5) * math.cos(elapsed * 14.0)
                p.save()
                p.translate(icon_cx, cy)
                p.scale(bolt_scale, bolt_scale)
                bolt = QPainterPath()
                bolt.moveTo(1.2, -6.2)
                bolt.lineTo(-4.0, 0.8)
                bolt.lineTo(-0.3, 0.8)
                bolt.lineTo(-1.4, 6.2)
                bolt.lineTo(4.0, -0.8)
                bolt.lineTo(0.3, -0.8)
                bolt.closeSubpath()
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(accent_bright)
                p.drawPath(bolt)
                p.restore()
            else:
                # Subtle horizontal shake on plug-out
                shake_x = math.sin(elapsed * 32.0) * math.exp(-elapsed * 5.5) * 3.0
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(accent.red(), accent.green(), accent.blue(), 36))
                p.drawEllipse(QPointF(icon_cx + shake_x, cy), 9.0, 9.0)

                # Unplugged / battery alert icon (bolt with slash or plug)
                p.save()
                p.translate(icon_cx + shake_x, cy)
                p.setPen(QPen(accent_bright, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.setBrush(Qt.BrushStyle.NoBrush)
                # Simple plug prongs + body
                p.drawRoundedRect(QRectF(-4.2, -3.5, 8.4, 6.2), 2.0, 2.0)
                p.drawLine(QPointF(-2.0, -5.8), QPointF(-2.0, -3.5))
                p.drawLine(QPointF(2.0, -5.8), QPointF(2.0, -3.5))
                p.drawLine(QPointF(0.0, 2.7), QPointF(0.0, 5.5))
                # Diagonal slash
                p.setPen(QPen(QColor(255, 255, 255, 220), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(-5.5, 5.0), QPointF(5.5, -5.0))
                p.restore()

            # Right side: Percentage + Animated Battery Pill
            bat_pill_w = 22.0
            bat_pill_h = 11.0
            bat_pill_x = float(w) - right_pad - bat_pill_w - 2.0
            bat_pill_y = cy - bat_pill_h / 2.0

            pct_str = f"{cap}%"
            f_pct = QFont("Sans", 8, QFont.Weight.Bold)
            f_pct.setPixelSize(11)
            p.setFont(f_pct)
            fm_pct = QFontMetricsF(f_pct)
            pct_w = fm_pct.horizontalAdvance(pct_str)
            pct_x = bat_pill_x - 6.0 - pct_w
            p.setPen(accent_bright)
            p.drawText(
                QRectF(pct_x, 0, pct_w + 2.0, float(h)),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                pct_str,
            )

            # Battery body
            bw = bat_pill_w - 3.0
            p.setPen(QPen(accent, 1.2))
            p.setBrush(QColor(accent.red(), accent.green(), accent.blue(), 26))
            p.drawRoundedRect(QRectF(bat_pill_x, bat_pill_y, bw, bat_pill_h), 2.8, 2.8)
            # Battery nub
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(accent)
            nub_h = bat_pill_h * 0.45
            p.drawRoundedRect(
                QRectF(bat_pill_x + bw + 1.0, cy - nub_h / 2.0, 1.8, nub_h),
                0.8, 0.8,
            )
            # Animated fill inside battery
            b_pad = 1.8
            avail_bw = max(0.0, bw - b_pad * 2.0)
            target_ratio = cap / 100.0
            anim_ratio = (target_ratio * ease_out) if is_plugged else target_ratio
            fill_bw = max(1.5, avail_bw * anim_ratio)
            p.setBrush(accent_bright)
            p.drawRoundedRect(
                QRectF(bat_pill_x + b_pad, bat_pill_y + b_pad, fill_bw, bat_pill_h - b_pad * 2.0),
                1.4, 1.4,
            )

            # Center Label
            f_hud = QFont("Sans", 8, QFont.Weight.Bold)
            f_hud.setPixelSize(11)
            p.setFont(f_hud)
            p.setPen(QColor(255, 255, 255))
            label_txt = ("Daya Penuh" if cap >= 99 else "Mengisi Daya") if is_plugged else "Charger Dilepas"
            tx_left = icon_cx + 14.0
            tx_right = pct_x - 6.0
            if tx_right > tx_left:
                p.drawText(
                    QRectF(tx_left, 0, tx_right - tx_left, float(h)),
                    int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                    label_txt,
                )

        elif hud.kind == "quote":
            q_text = ""
            q_author = ""
            try:
                q_obj = json.loads(hud.extra_text)
                q_text = str(q_obj.get("text", "")).strip()
                q_author = str(q_obj.get("author", "")).strip()
            except Exception:
                q_text = (hud.extra_text or "").strip()

            inner_x = ear + 18.0
            inner_w = max(80.0, float(w) - 2.0 * inner_x)
            author_h = 18.0 if q_author else 0.0
            top_pad = 12.0
            bot_pad = 12.0

            f_q = QFont("Sans", 9, QFont.Weight.DemiBold)
            f_q.setPixelSize(12)
            p.setFont(f_q)
            p.setPen(QColor(248, 250, 252))
            q_rect = QRectF(inner_x, top_pad, inner_w, max(24.0, float(h) - top_pad - bot_pad - author_h - 4.0))
            p.drawText(
                q_rect,
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap),
                f'"{q_text}"' if q_text else "",
            )

            if q_author:
                f_auth = QFont("Sans", 8, QFont.Weight.Bold)
                f_auth.setPixelSize(10)
                p.setFont(f_auth)
                p.setPen(QColor(148, 163, 184))
                a_rect = QRectF(inner_x, float(h) - bot_pad - author_h, inner_w, author_h)
                p.drawText(
                    a_rect,
                    int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                    f"- {q_author}",
                )

        elif hud.kind == "utility_color":
            hex_code = (hud.extra_text or "#FFFFFF").strip()
            sw_col = QColor(hex_code)
            if not sw_col.isValid():
                sw_col = QColor(255, 255, 255)
            p.setPen(QPen(QColor(255, 255, 255, 190), 1.4))
            p.setBrush(sw_col)
            p.drawRoundedRect(QRectF(pad_x + 1.0, (float(h) - 14.0) / 2.0, 14.0, 14.0), 4.0, 4.0)

            p.setFont(f_hud)
            p.setPen(QColor(248, 250, 252))
            p.drawText(
                QRectF(pad_x + 22.0, 0.0, float(w) - pad_x - right_pad - 90.0, float(h)),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                "Warna Disalin",
            )
            f_hex = QFont("Monospace", 9, QFont.Weight.Bold)
            f_hex.setPixelSize(11)
            p.setFont(f_hex)
            p.setPen(QColor(52, 211, 153))
            p.drawText(
                QRectF(float(w) - right_pad - 72.0, 0.0, 72.0, float(h)),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                hex_code,
            )

        elif hud.kind == "utility_ocr":
            cls.draw_ocr_icon(p, pad_x + 8.0, float(h) / 2.0, size=13.0, color=QColor(56, 189, 248))
            preview = " ".join((hud.extra_text or "").split())
            p.setFont(f_hud)
            p.setPen(QColor(248, 250, 252))
            tx_rect = QRectF(pad_x + 22.0, 0.0, max(60.0, float(w) - pad_x - right_pad - 24.0), float(h))
            fm_u = QFontMetricsF(f_hud)
            label_u = fm_u.elidedText(f"Teks Disalin: {preview}", Qt.TextElideMode.ElideRight, tx_rect.width())
            p.drawText(tx_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), label_u)

        elif hud.kind == "utility_msg":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(251, 191, 36))
            p.drawEllipse(QPointF(pad_x + 8.0, float(h) / 2.0), 4.0, 4.0)
            p.setFont(f_hud)
            p.setPen(QColor(248, 250, 252))
            tx_rect = QRectF(pad_x + 20.0, 0.0, max(60.0, float(w) - pad_x - right_pad - 22.0), float(h))
            fm_u = QFontMetricsF(f_hud)
            p.drawText(tx_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), fm_u.elidedText(hud.extra_text or "", Qt.TextElideMode.ElideRight, tx_rect.width()))

    @classmethod
    def render_frame(
        cls,
        w: int,
        h: int,
        mode: str,
        active_tab: str,
        bat: BatteryState,
        toast: Optional[NotificationItem],
        toast_ratio: float,
        notifications: List[NotificationItem],
        hovered: bool,
        focused: bool,
        dnd: bool,
        kde_autohide: bool,
        monitor_label: str,
        hover_pos: Tuple[float, float],
        hit_boxes: Dict[str, QRectF],
        hud: Optional[HudItem] = None,
        mpris: Optional[MprisWatcher] = None,
        music_view_mode: str = "player",
        prayer: Optional[PrayerManager] = None,
        prayer_view_mode: str = "schedule",
        standby_display: str = "clock",
        notif_scroll_y: float = 0.0,
        scale: float = 1.0,
        weather: Optional[WeatherManager] = None,
        standby_weather: bool = True,
        idle_sec: float = 0.0,
        battery: Optional[BatteryState] = None,
        standby_battery: bool = True,
        is_drag_target: bool = False,
        show_remaining_music_time: bool = False,
        standby_lyrics: bool = True,
        is_typing: bool = False,
        is_angry: bool = False,
        is_notif: bool = False,
        cursor_gaze: Tuple[float, float] = (0.0, 0.0),
        system_ctrl: Optional[SystemControlManager] = None,
        island_shape: str = "notch",
        finance: Optional[FinanceManager] = None,
        standby_finance: bool = True,
        audio_spectrum: Optional[List[float]] = None,
        privacy: Optional[PrivacyMonitor] = None,
        standby_privacy: bool = True,
    ) -> QImage:
        hit_boxes.clear()
        is_pill = (island_shape == "pill")
        ear = 2.0 if is_pill else 11.0
        bottom_r = 12.0 if h <= 34 else (18.0 if mode == "toast" else 20.0)

        phys_w = max(1, int(round(w * scale)))
        phys_h = max(1, int(round(h * scale)))

        img = QImage(phys_w, phys_h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0)

        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if scale != 1.0:
            p.scale(scale, scale)

        # 1. Pure Black Island Silhouette (MacBook Notch or Floating Pill Capsule)
        if is_pill:
            pill_r = float(h) / 2.0 if h <= 34 else (18.0 if mode == "toast" else 20.0)
            notch_path = cls.make_pill_path(float(w), float(h), corner_r=pill_r)
        else:
            notch_path = cls.make_notch_path(float(w), float(h), ear=ear, bottom_r=bottom_r)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(cls.C_BLACK)
        p.drawPath(notch_path)

        p.setClipPath(notch_path)

        # 2. Mode-Specific Content
        if mode == "hud" and hud is not None:
            cls._draw_hud_area(p, w, h, ear, hud)
        elif mode == "toast" and toast is not None and h >= 36:
            cls._draw_toast_area(p, w, h, ear, toast, toast_ratio, hover_pos, hit_boxes)
        elif mode == "panel" and h >= 90:
            cls._draw_panel_area(
                p,
                w,
                h,
                ear,
                active_tab,
                bat,
                notifications,
                dnd,
                kde_autohide,
                monitor_label,
                hover_pos,
                hit_boxes,
                mpris,
                music_view_mode,
                prayer,
                prayer_view_mode,
                standby_display,
                notif_scroll_y,
                weather,
                standby_weather,
                is_drag_active=is_drag_target,
                show_remaining_music_time=show_remaining_music_time,
                system_ctrl=system_ctrl,
                finance=finance,
                audio_spectrum=audio_spectrum,
                privacy=privacy,
            )
        else:
            # Standby mode status strip (clock/eyes + weather + equalizer + unread bell + prayer alert)
            top_h = 28.0 if mode != "standby" else float(h)
            cy = top_h / 2.0

            if is_drag_target:
                # Standby Drag & Drop Capsule
                cx = float(w) / 2.0
                cls.draw_localsend_icon(p, cx - 62.0, cy, size=14.0, color=cls.C_CYAN)
                f_drop = QFont("Sans", 9, QFont.Weight.Bold)
                f_drop.setPixelSize(11)
                p.setFont(f_drop)
                p.setPen(cls.C_WHITE)
                p.drawText(
                    QRectF(cx - 48.0, 0, 115.0, top_h),
                    int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                    "Drop File Kirim",
                )
                p.setPen(QPen(cls.C_CYAN, 1.4))
                p.drawLine(QPointF(ear + 10.0, top_h - 1.2), QPointF(float(w) - ear - 10.0, top_h - 1.2))
                p.end()
                return img

            now = datetime.datetime.now()
            clock_str = now.strftime("%H:%M:%S" if (hovered and mode == "standby") else "%H:%M")

            f_clock = QFont("Sans", 9, QFont.Weight.Bold)
            f_clock.setPixelSize(12)
            p.setFont(f_clock)
            p.setPen(cls.C_WHITE)

            notif_count = len(notifications)
            has_music = mpris is not None and mpris.is_playing
            prayer_alert = prayer.check_alert(now) if prayer else None
            wdata = weather.data if (weather and standby_weather) else None
            has_battery = standby_battery and battery is not None and battery.present
            has_finance = standby_finance and finance is not None and finance.data is not None and finance.data.ihsg is not None
            has_privacy = standby_privacy and privacy is not None and (privacy.cam_active or privacy.mic_active)

            if prayer_alert is not None and prayer_alert.is_adzan:
                # Full Notch Adzan Active Banner (Centered)
                f_name = QFont("Sans", 9, QFont.Weight.Bold)
                f_name.setPixelSize(11)
                p.setFont(f_name)
                fm = QFontMetricsF(f_name)

                adzan_txt = f"Telah Memasuki Waktu Adzan {prayer_alert.name}"
                icon_size = 14.0
                gap = 8.0
                max_content_w = float(w) - 2.0 * (ear + 14.0)

                raw_txt_w = fm.horizontalAdvance(adzan_txt)
                if icon_size + gap + raw_txt_w > max_content_w:
                    avail_txt_w = max(20.0, max_content_w - (icon_size + gap))
                    disp_txt = fm.elidedText(adzan_txt, Qt.TextElideMode.ElideRight, avail_txt_w)
                    txt_w = fm.horizontalAdvance(disp_txt)
                else:
                    disp_txt = adzan_txt
                    txt_w = raw_txt_w

                total_content_w = icon_size + gap + txt_w
                start_x = (float(w) - total_content_w) / 2.0

                vib_x = math.sin(time.time() * 28.0) * 1.5
                moon_cx = start_x + icon_size / 2.0 + vib_x
                cls.draw_crescent_moon(p, moon_cx, cy, size=icon_size, color=cls.C_GREEN)

                p.setPen(cls.C_WHITE)
                p.drawText(
                    QRectF(start_x + icon_size + gap, 0, txt_w + 4.0, top_h),
                    int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                    disp_txt,
                )
            elif has_music and standby_lyrics:
                # Live Karaoke Lyrics in Standby Notch
                cur_lyric_txt = ""
                if mpris:
                    cur_lyric_txt, _, _ = mpris.get_current_lyric()
                    if not cur_lyric_txt:
                        if mpris.lyrics_loading:
                            cur_lyric_txt = "Memuat lirik..."
                        elif mpris.title:
                            clean_t = mpris.clean_title
                            clean_a = mpris.clean_artist
                            cur_lyric_txt = f"{clean_a} - {clean_t}" if (clean_a and clean_a.lower() not in clean_t.lower()) else clean_t

                left_pad = ear + 13.0
                right_pad = ear + 13.0
                item_gap = 10.0

                # 1. Left Icon: Animated Equalizer
                eq_cx = left_pad + 8.0
                cls.draw_equalizer(p, eq_cx, cy, t=time.time(), color=cls.C_GREEN, spectrum=audio_spectrum)
                left_end_x = eq_cx + 8.0

                # 2. Right items: Battery Donut and/or Unread Notifications
                cur_right_x = float(w) - right_pad

                if has_battery:
                    donut_size = 17.0
                    b_x = cur_right_x - donut_size / 2.0
                    item_start_x = cur_right_x - donut_size
                    hit_boxes["standby_battery"] = QRectF(item_start_x - 2.0, 0, donut_size + 4.0, top_h)
                    cls.draw_battery_donut(p, b_x, cy, battery=battery, size=donut_size)
                    cur_right_x = item_start_x - item_gap

                if notif_count > 0:
                    cnt_str = f"{notif_count}" if notif_count <= 99 else "99+"
                    f_cnt = QFont("Sans", 8, QFont.Weight.Bold)
                    f_cnt.setPixelSize(11)
                    fm_cnt = QFontMetricsF(f_cnt)
                    cnt_w = fm_cnt.horizontalAdvance(cnt_str)
                    icon_size = 12.0
                    gap = 4.0
                    badge_total_w = icon_size + gap + cnt_w
                    notif_x = cur_right_x - badge_total_w
                    cls.draw_notification_icon(p, notif_x + icon_size / 2.0, cy, size=icon_size, color=cls.C_BLUE)
                    p.setFont(f_cnt)
                    p.setPen(cls.C_BLUE)
                    p.drawText(
                        QRectF(notif_x + icon_size + gap, 0, cnt_w + 4.0, top_h),
                        int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                        cnt_str,
                    )
                    cur_right_x = notif_x - item_gap

                # 3. Center Lyric Text Area with Ping-Pong Marquee
                avail_x = left_end_x + 6.0
                avail_w = cur_right_x - avail_x - 6.0

                if cur_lyric_txt and avail_w > 15.0:
                    f_lyric = QFont("Sans", 9, QFont.Weight.Bold)
                    f_lyric.setPixelSize(11)
                    p.setFont(f_lyric)
                    fm_lyr = QFontMetricsF(f_lyric)
                    txt_w = fm_lyr.horizontalAdvance(cur_lyric_txt)

                    p.save()
                    p.setClipRect(QRectF(avail_x, 0, avail_w, top_h))
                    p.setPen(cls.C_WHITE)

                    if txt_w <= avail_w:
                        lyric_rect = QRectF(avail_x, 0, avail_w, top_h)
                        p.drawText(lyric_rect, int(Qt.AlignmentFlag.AlignCenter), cur_lyric_txt)
                    else:
                        excess = txt_w - avail_w
                        scroll_dur = max(1.0, excess / 28.0)
                        cycle = 1.2 + scroll_dur + 1.2 + scroll_dur
                        t_in_cycle = (time.time() % cycle)
                        if t_in_cycle < 1.2:
                            offset = 0.0
                        elif t_in_cycle < 1.2 + scroll_dur:
                            offset = ((t_in_cycle - 1.2) / scroll_dur) * excess
                        elif t_in_cycle < 1.2 + scroll_dur + 1.2:
                            offset = excess
                        else:
                            offset = (1.0 - (t_in_cycle - (1.2 + scroll_dur + 1.2)) / scroll_dur) * excess

                        scroll_x = avail_x - offset
                        p.drawText(
                            QRectF(scroll_x, 0, txt_w + 10.0, top_h),
                            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                            cur_lyric_txt,
                        )
                    p.restore()
            elif not has_music and notif_count == 0 and prayer_alert is None and wdata is None and not has_battery and not has_finance and not has_privacy:
                if standby_display == "eyes":
                    hit_boxes["standby_eyes"] = QRectF(float(w) / 2.0 - 24.0, 0, 48.0, top_h)
                    cls.draw_blinking_eyes(
                        p,
                        cx=float(w) / 2.0,
                        cy=cy,
                        hovered=hovered,
                        is_music=has_music,
                        idle_sec=idle_sec,
                        is_typing=is_typing,
                        is_angry=is_angry,
                        is_notif=is_notif or (notif_count > 0) or (toast is not None),
                        cursor_gaze=cursor_gaze,
                    )
                else:
                    p.drawText(
                        QRectF(0, 0, float(w), top_h),
                        int(Qt.AlignmentFlag.AlignCenter),
                        clock_str,
                    )
            else:
                is_pill = (island_shape == "pill")
                left_pad = 16.0 if is_pill else 22.0
                right_pad = 16.0 if is_pill else 22.0
                item_gap = 8.0

                if standby_display == "eyes":
                    left_w = 26.0
                    hit_boxes["standby_eyes"] = QRectF(left_pad, 0, left_w, top_h)
                    cls.draw_blinking_eyes(
                        p,
                        cx=left_pad + 12.0,
                        cy=cy,
                        hovered=hovered,
                        is_music=has_music,
                        idle_sec=idle_sec,
                        is_typing=is_typing,
                        is_angry=is_angry,
                        is_notif=is_notif or (notif_count > 0) or (toast is not None),
                        cursor_gaze=cursor_gaze,
                    )
                    left_end_x = left_pad + left_w
                else:
                    f_clock = QFont("Sans", 9, QFont.Weight.Bold)
                    f_clock.setPixelSize(12)
                    p.setFont(f_clock)
                    fm_clk = QFontMetricsF(f_clock)
                    clk_w = fm_clk.horizontalAdvance(clock_str)
                    hit_boxes["standby_clock"] = QRectF(left_pad - 4.0, 0, clk_w + 8.0, top_h)
                    p.setPen(cls.C_WHITE)
                    p.drawText(
                        QRectF(left_pad, 0, clk_w + 4.0, top_h),
                        int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                        clock_str,
                    )
                    left_w = clk_w
                    left_end_x = left_pad + left_w

                # Collision safety threshold: right items cannot cross into the left territory!
                min_safe_x = left_end_x + 8.0
                cur_right_x = float(w) - right_pad

                # 0. Camera & Mic Privacy Indicator Dots (far right edge, ala macOS/iOS)
                if has_privacy:
                    dots = []
                    if privacy.cam_active:
                        dots.append(QColor(34, 197, 94))   # Green camera dot
                    if privacy.mic_active:
                        dots.append(QColor(249, 115, 22))  # Orange mic dot
                    dots_w = len(dots) * 8.0 + max(0, len(dots) - 1) * 1.5
                    app_lbl = ""
                    lbl_w = 0.0
                    if hovered:
                        raw_apps = privacy.cam_apps or privacy.mic_apps
                        if raw_apps:
                            app_lbl = raw_apps[0]
                            f_priv = QFont("Sans", 8, QFont.Weight.Bold)
                            f_priv.setPixelSize(10)
                            fm_priv = QFontMetricsF(f_priv)
                            lbl_w = 4.0 + fm_priv.horizontalAdvance(app_lbl)
                    priv_total_w = dots_w + lbl_w
                    priv_x = cur_right_x - priv_total_w
                    if priv_x >= min_safe_x:
                        hit_boxes["standby_privacy"] = QRectF(priv_x - 3.0, 0, priv_total_w + 6.0, top_h)
                        p.save()
                        p.setPen(Qt.PenStyle.NoPen)
                        dx_dot = priv_x + 4.0
                        for col_d in dots:
                            p.setBrush(QColor(col_d.red(), col_d.green(), col_d.blue(), 80))
                            p.drawEllipse(QPointF(dx_dot, cy), 4.6, 4.6)
                            p.setBrush(col_d)
                            p.drawEllipse(QPointF(dx_dot, cy), 2.8, 2.8)
                            dx_dot += 9.5
                        if app_lbl:
                            p.setFont(f_priv)
                            p.setPen(dots[0])
                            p.drawText(
                                QRectF(priv_x + dots_w + 3.0, 0, lbl_w, top_h),
                                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                                app_lbl,
                            )
                        p.restore()
                        cur_right_x = priv_x - item_gap

                # 1. Battery Indicator Donut
                if has_battery:
                    donut_size = 17.0
                    b_x = cur_right_x - donut_size / 2.0
                    item_start_x = cur_right_x - donut_size
                    if item_start_x >= min_safe_x:
                        hit_boxes["standby_battery"] = QRectF(item_start_x - 2.0, 0, donut_size + 4.0, top_h)
                        cls.draw_battery_donut(p, b_x, cy, battery=battery, size=donut_size)
                        cur_right_x = item_start_x - item_gap

                # 2. Active Music Equalizer
                if has_music:
                    eq_w = 16.0
                    item_start_x = cur_right_x - eq_w
                    if item_start_x >= min_safe_x:
                        cls.draw_equalizer(p, cur_right_x - 8.0, cy, t=time.time(), color=cls.C_GREEN, spectrum=audio_spectrum)
                        cur_right_x = item_start_x - item_gap

                # 3. Unread Notifications
                if notif_count > 0:
                    cnt_str = f"{notif_count}" if notif_count <= 99 else "99+"
                    f_cnt = QFont("Sans", 8, QFont.Weight.Bold)
                    f_cnt.setPixelSize(11)
                    fm_cnt = QFontMetricsF(f_cnt)
                    cnt_w = fm_cnt.horizontalAdvance(cnt_str)

                    icon_size = 12.0
                    gap = 4.0
                    badge_total_w = icon_size + gap + cnt_w
                    notif_x = cur_right_x - badge_total_w
                    if notif_x >= min_safe_x:
                        cls.draw_notification_icon(p, notif_x + icon_size / 2.0, cy, size=icon_size, color=cls.C_BLUE)
                        p.setFont(f_cnt)
                        p.setPen(cls.C_BLUE)
                        p.drawText(
                            QRectF(notif_x + icon_size + gap, 0, cnt_w + 4.0, top_h),
                            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                            cnt_str,
                        )
                        cur_right_x = notif_x - item_gap

                # 4. Weather Indicator (Icon + Temperature)
                if wdata is not None:
                    temp_txt = f"{round(wdata.temperature):.0f}°"
                    if hovered:
                        temp_txt = f"{round(wdata.temperature):.0f}° {wdata.condition_text}"
                    f_wt = QFont("Sans", 8, QFont.Weight.Bold)
                    f_wt.setPixelSize(10)
                    fm_wt = QFontMetricsF(f_wt)
                    txt_w = fm_wt.horizontalAdvance(temp_txt)

                    w_icon_size = 12.0
                    gap = 4.0
                    w_total_w = w_icon_size + gap + txt_w
                    w_badge_x = cur_right_x - w_total_w
                    if w_badge_x >= min_safe_x:
                        hit_boxes["standby_weather"] = QRectF(w_badge_x - 3.0, 0, w_total_w + 6.0, top_h)
                        cls.draw_weather_icon(p, w_badge_x + w_icon_size / 2.0, cy, wdata.weather_code, size=w_icon_size)
                        p.setFont(f_wt)
                        p.setPen(cls.C_WHITE)
                        p.drawText(
                            QRectF(w_badge_x + w_icon_size + gap, 0, txt_w + 4.0, top_h),
                            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                            temp_txt,
                        )
                        cur_right_x = w_badge_x - item_gap

                # 5. Prayer Alert Indicator (Icon + Name / Adzan)
                if prayer_alert is not None:
                    p_name = prayer_alert.name
                    p_time = prayer_alert.time_str
                    is_adz = prayer_alert.is_adzan
                    p_txt = f"Adzan {p_name}" if is_adz else f"{p_name} {p_time}"

                    f_pr = QFont("Sans", 8, QFont.Weight.Bold)
                    f_pr.setPixelSize(10)
                    fm_pr = QFontMetricsF(f_pr)
                    txt_w = fm_pr.horizontalAdvance(p_txt)

                    mosque_size = 12.0
                    gap = 4.0
                    total_w = mosque_size + gap + txt_w
                    badge_x = cur_right_x - total_w
                    if badge_x >= min_safe_x:
                        col = cls.C_GREEN if is_adz else cls.C_AMBER
                        shake_x = 0.0
                        shake_y = 0.0
                        if is_adz:
                            t_sec = time.time()
                            shake_x = math.sin(t_sec * 38.0) * 1.5
                            shake_y = math.cos(t_sec * 28.0) * 0.6

                        cls.draw_crescent_moon(
                            p,
                            badge_x + mosque_size / 2.0 + shake_x,
                            cy + shake_y,
                            size=mosque_size,
                            color=col,
                        )

                        p.setFont(f_pr)
                        p.setPen(col)
                        p.drawText(
                            QRectF(badge_x + mosque_size + gap, 0, txt_w + 4.0, top_h),
                            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                            p_txt,
                        )
                        cur_right_x = badge_x - item_gap

                # 6. Finance / IHSG Indicator (Icon + Trend Pct / Full on Hover)
                if finance and finance.data and finance.data.ihsg and standby_finance:
                    ihsg = finance.data.ihsg
                    fin_col = cls.C_GREEN if ihsg.is_up else (QColor(248, 113, 113) if ihsg.change_pct < 0 else cls.C_MUTED)
                    sign = "+" if ihsg.change_pct > 0 else ""
                    arrow = "▲" if ihsg.is_up else ("▼" if ihsg.change_pct < 0 else "")
                    fin_txt = f"IHSG {ihsg.price:,.0f} {arrow}{sign}{ihsg.change_pct:.1f}%" if hovered else f"{arrow}{sign}{ihsg.change_pct:.1f}%"
                    f_ft = QFont("Sans", 8, QFont.Weight.Bold)
                    f_ft.setPixelSize(10)
                    fm_ft = QFontMetricsF(f_ft)
                    txt_w = fm_ft.horizontalAdvance(fin_txt)

                    fin_icon_size = 11.0
                    gap = 3.5
                    fin_total_w = fin_icon_size + gap + txt_w
                    fin_badge_x = cur_right_x - fin_total_w
                    if fin_badge_x >= min_safe_x:
                        hit_boxes["standby_finance"] = QRectF(fin_badge_x - 3.0, 0, fin_total_w + 6.0, top_h)
                        cls.draw_finance_icon(p, fin_badge_x + fin_icon_size / 2.0, cy, size=fin_icon_size, color=fin_col)
                        p.setFont(f_ft)
                        p.setPen(fin_col)
                        p.drawText(
                            QRectF(fin_badge_x + fin_icon_size + gap, 0, txt_w + 4.0, top_h),
                            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                            fin_txt,
                        )
                        cur_right_x = fin_badge_x - item_gap

        p.end()
        return img

    @classmethod
    def _draw_toast_area(
        cls,
        p: QPainter,
        w: int,
        h: int,
        ear: float,
        toast: NotificationItem,
        toast_ratio: float,
        hover_pos: Tuple[float, float],
        hit_boxes: Dict[str, QRectF],
    ):
        hx, hy = hover_pos
        pad_x = ear + 13.0

        # Real App Icon
        icon_size = 38.0
        badge_rect = QRectF(pad_x, (float(h) - icon_size) / 2.0, icon_size, icon_size)
        pm = IconResolver.get_icon_pixmap(toast.app_icon, toast.app_name, int(icon_size))
        if pm and not pm.isNull():
            p.drawPixmap(badge_rect.toRect(), pm)
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(cls.C_CARD)
            p.drawRoundedRect(badge_rect, 9.0, 9.0)
            initial = (toast.app_name[:1] or "N").upper()
            f_init = QFont("Sans", 10, QFont.Weight.Bold)
            f_init.setPixelSize(14)
            p.setFont(f_init)
            p.setPen(cls.C_GREEN if "Daya" in toast.app_name else cls.C_WHITE)
            p.drawText(badge_rect, int(Qt.AlignmentFlag.AlignCenter), initial)

        # Close Button
        close_rect = QRectF(w - pad_x - 22.0, (float(h) - 22.0) / 2.0, 22.0, 22.0)
        hit_boxes["toast_close"] = close_rect
        is_close_hov = close_rect.contains(QPointF(hx, hy))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(cls.C_CARD_HOVER if is_close_hov else QColor(255, 255, 255, 14))
        p.drawEllipse(close_rect)

        f_x = QFont("Sans", 9, QFont.Weight.Bold)
        f_x.setPixelSize(12)
        p.setFont(f_x)
        p.setPen(cls.C_WHITE)
        p.drawText(close_rect, int(Qt.AlignmentFlag.AlignCenter), "×")

        # Text Area (Stacked Vertically - Never Overlapping)
        text_x = pad_x + icon_size + 10.0
        text_w = max(80.0, close_rect.left() - text_x - 10.0)

        # Row 1: Judul Notif (Full text_w, NO jam sejajar)
        f_title = QFont("Sans", 9, QFont.Weight.Bold)
        f_title.setPixelSize(11)
        p.setFont(f_title)
        fm_title = QFontMetricsF(f_title)
        elided_title = fm_title.elidedText(toast.summary, Qt.TextElideMode.ElideRight, text_w)
        p.setPen(cls.C_WHITE)
        p.drawText(
            QRectF(text_x, 9.0, text_w, 16.0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            elided_title,
        )

        # Row 2: Isi Notif
        f_body = QFont("Sans", 8)
        f_body.setPixelSize(10)
        p.setFont(f_body)
        fm_body = QFontMetricsF(f_body)
        body_raw = toast.body if toast.body else "Klik notch untuk membuka pusat notifikasi"
        elided_body = fm_body.elidedText(body_raw, Qt.TextElideMode.ElideRight, text_w)
        p.setPen(cls.C_WHITE_SEC)
        p.drawText(
            QRectF(text_x, 26.0, text_w, 15.0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            elided_body,
        )

        # Row 3: Jam & Nama Aplikasi (Dibawahnya saja)
        f_meta = QFont("Sans", 8)
        f_meta.setPixelSize(9)
        p.setFont(f_meta)
        fm_meta = QFontMetricsF(f_meta)
        meta_txt = f"{toast.app_name} • {toast.timestamp}"
        elided_meta = fm_meta.elidedText(meta_txt, Qt.TextElideMode.ElideRight, text_w)
        p.setPen(cls.C_MUTED)
        p.drawText(
            QRectF(text_x, 42.0, text_w, 13.0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            elided_meta,
        )

        prog_w = (w - 2 * (ear + 18.0)) * max(0.0, min(1.0, toast_ratio))
        if prog_w > 2.0:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(cls.C_GREEN)
            p.drawRoundedRect(QRectF(ear + 18.0, h - 3.5, prog_w, 2.0), 1.0, 1.0)

    @classmethod
    def _draw_weather_tab(
        cls,
        p: QPainter,
        w: int,
        h: int,
        ear: float,
        weather: Optional[WeatherManager],
        prayer: Optional[PrayerManager],
        hover_pos: Tuple[float, float],
        hit_boxes: Dict[str, QRectF],
    ):
        hx, hy = hover_pos
        pad_x = ear + 14.0
        inner_w = w - 2 * pad_x
        y_cur = 36.0

        wdata = weather.data if weather else None
        city_name = wdata.city_name if wdata else (prayer.current_city["name"] if prayer else "Banyumas")
        temp = f"{round(wdata.temperature):.0f}°" if wdata else "--°"
        apparent = f"{round(wdata.apparent_temperature):.0f}°" if wdata else "--°"
        cond_txt = wdata.condition_text if wdata else "Memuat..."
        wcode = wdata.weather_code if wdata else 1
        t_max = f"{round(wdata.temp_max):.0f}°" if wdata else "--°"
        t_min = f"{round(wdata.temp_min):.0f}°" if wdata else "--°"
        humidity = f"{wdata.humidity}%" if wdata else "--%"
        wind = f"{wdata.wind_speed:.1f} km/j" if wdata else "-- km/j"
        precip = f"{wdata.precipitation:.1f} mm" if wdata else "-- mm"

        # 1. Header: Borderless City Selector + Condition + Refresh Button
        row1_h = 22.0
        f_loc = QFont("Sans", 10, QFont.Weight.Bold)
        f_loc.setPixelSize(12)
        fm_loc = QFontMetricsF(f_loc)
        loc_txt = f"{city_name} ▾"
        loc_w = fm_loc.horizontalAdvance(loc_txt)
        loc_rect = QRectF(pad_x, y_cur, loc_w + 10.0, row1_h)
        hit_boxes["btn_select_city"] = loc_rect
        loc_hov = loc_rect.contains(QPointF(hx, hy))

        p.setFont(f_loc)
        p.setPen(cls.C_GREEN if loc_hov else cls.C_WHITE)
        p.drawText(
            QRectF(pad_x, y_cur, loc_w, row1_h),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            loc_txt,
        )

        # Condition inline beside city (no box)
        f_cond = QFont("Sans", 9)
        f_cond.setPixelSize(11)
        p.setFont(f_cond)
        p.setPen(cls.C_BLUE)
        p.drawText(
            QRectF(pad_x + loc_w + 10.0, y_cur, 140.0, row1_h),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            f"•  {cond_txt}",
        )

        # Refresh button on the right (no box)
        btn_ref_w = 22.0
        ref_rect = QRectF(pad_x + inner_w - btn_ref_w, y_cur, btn_ref_w, row1_h)
        hit_boxes["btn_refresh_weather"] = ref_rect
        ref_hov = ref_rect.contains(QPointF(hx, hy))
        f_ref = QFont("Sans", 9, QFont.Weight.Bold)
        f_ref.setPixelSize(12)
        p.setFont(f_ref)
        p.setPen(cls.C_GREEN if ref_hov else cls.C_MUTED)
        p.drawText(ref_rect, int(Qt.AlignmentFlag.AlignCenter), "⟳")

        # 2. Hero Section: Large Temperature, Feels Like, High/Low, Big Weather Icon (no box)
        hero_y = y_cur + 26.0
        hero_h = 60.0

        f_big_t = QFont("Sans", 28, QFont.Weight.Bold)
        f_big_t.setPixelSize(40)
        p.setFont(f_big_t)
        p.setPen(cls.C_WHITE)
        p.drawText(
            QRectF(pad_x, hero_y, 82.0, hero_h),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            temp,
        )

        f_st = QFont("Sans", 8)
        f_st.setPixelSize(10)
        p.setFont(f_st)
        p.setPen(cls.C_MUTED)
        p.drawText(
            QRectF(pad_x + 88.0, hero_y + 12.0, 160.0, 16.0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            f"Terasa seperti {apparent}",
        )
        p.setPen(cls.C_WHITE_SEC)
        p.drawText(
            QRectF(pad_x + 88.0, hero_y + 32.0, 160.0, 16.0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            f"▲ {t_max}   ▼ {t_min}",
        )

        # Big Weather Icon floating cleanly on the right
        icon_cx = pad_x + inner_w - 32.0
        icon_cy = hero_y + hero_h / 2.0
        cls.draw_weather_icon(p, icon_cx, icon_cy, wcode, size=44.0)

        # Subtle Separator Line
        p.setPen(QPen(QColor(255, 255, 255, 16), 0.8))
        p.drawLine(QPointF(pad_x, 126.0), QPointF(pad_x + inner_w, 126.0))

        # 3. Weather Stats: 3 Clean Borderless Columns (Kelembaban, Angin, Curah Hujan)
        stats_y = 132.0
        stats_h = 38.0
        stat_w = inner_w / 3.0

        stats_data = [
            ("Kelembaban", humidity, cls.C_BLUE),
            ("Kec. Angin", wind, cls.C_WHITE),
            ("Curah Hujan", precip, cls.C_GREEN),
        ]

        for s_i, (lbl, val, col) in enumerate(stats_data):
            sx = pad_x + s_i * stat_w

            f_sl = QFont("Sans", 8)
            f_sl.setPixelSize(9)
            p.setFont(f_sl)
            p.setPen(cls.C_MUTED)
            p.drawText(
                QRectF(sx, stats_y + 2.0, stat_w, 13.0),
                int(Qt.AlignmentFlag.AlignCenter),
                lbl,
            )

            f_sv = QFont("Sans", 9, QFont.Weight.Bold)
            f_sv.setPixelSize(12)
            p.setFont(f_sv)
            p.setPen(col)
            p.drawText(
                QRectF(sx, stats_y + 18.0, stat_w, 16.0),
                int(Qt.AlignmentFlag.AlignCenter),
                val,
            )

        # Subtle Separator Line
        p.setPen(QPen(QColor(255, 255, 255, 16), 0.8))
        p.drawLine(QPointF(pad_x, 176.0), QPointF(pad_x + inner_w, 176.0))

        # 4. Hourly / Daily Forecast: 5 Clean Borderless Columns
        fc_y = 182.0
        f_fch = QFont("Sans", 8, QFont.Weight.Bold)
        f_fch.setPixelSize(9)
        p.setFont(f_fch)
        p.setPen(cls.C_MUTED)
        p.drawText(
            QRectF(pad_x, fc_y, inner_w, 14.0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            "PERKIRAAN JAM & HARI BERIKUTNYA",
        )

        fc_cols_y = fc_y + 18.0
        fc_h = 58.0

        slots = []
        if wdata and wdata.hourly:
            for item in wdata.hourly[:3]:
                slots.append((item[0], f"{round(item[1]):.0f}°", item[2]))
        if wdata and wdata.daily:
            for d_item in wdata.daily[1:3]:
                slots.append((d_item[0], f"{round(d_item[2]):.0f}°", d_item[3]))

        while len(slots) < 5:
            slots.append(("--", "--°", 1))

        n_slots = 5
        slot_w = inner_w / n_slots

        for idx, (lbl, s_temp, s_code) in enumerate(slots):
            slot_x = pad_x + idx * slot_w

            # Slot label (e.g. "Sekarang", "18:00", "Besok")
            f_lbl = QFont("Sans", 7)
            f_lbl.setPixelSize(9)
            p.setFont(f_lbl)
            p.setPen(cls.C_MUTED)
            p.drawText(
                QRectF(slot_x, fc_cols_y, slot_w, 13.0),
                int(Qt.AlignmentFlag.AlignCenter),
                lbl,
            )

            # Slot icon
            cls.draw_weather_icon(p, slot_x + slot_w / 2.0, fc_cols_y + 25.0, s_code, size=20.0)

            # Slot temperature
            f_tmp = QFont("Sans", 8, QFont.Weight.Bold)
            f_tmp.setPixelSize(10)
            p.setFont(f_tmp)
            p.setPen(cls.C_WHITE)
            p.drawText(
                QRectF(slot_x, fc_cols_y + 40.0, slot_w, 14.0),
                int(Qt.AlignmentFlag.AlignCenter),
                s_temp,
            )

    @classmethod
    def _draw_finance_tab(
        cls,
        p: QPainter,
        w: int,
        h: int,
        ear: float,
        finance: Optional[FinanceManager],
        hover_pos: Tuple[float, float],
        hit_boxes: Dict[str, QRectF],
    ):
        hx, hy = hover_pos
        pad_x = ear + 14.0
        inner_w = w - 2 * pad_x
        y_cur = 36.0

        fdata = finance.data if finance else None
        ihsg = fdata.ihsg if fdata else None
        currs = fdata.currencies if fdata else []

        # 1. Header Row: Title "PASAR & KEUANGAN" + Refresh Button + Market Status
        row1_h = 20.0
        f_tit = QFont("Sans", 10, QFont.Weight.Bold)
        f_tit.setPixelSize(12)
        p.setFont(f_tit)
        p.setPen(cls.C_WHITE)
        p.drawText(
            QRectF(pad_x, y_cur, 160.0, row1_h),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            "PASAR & KEUANGAN",
        )

        # Refresh button
        btn_ref_w = 22.0
        ref_rect = QRectF(pad_x + inner_w - btn_ref_w, y_cur, btn_ref_w, row1_h)
        hit_boxes["btn_refresh_finance"] = ref_rect
        ref_hov = ref_rect.contains(QPointF(hx, hy))
        f_ref = QFont("Sans", 9, QFont.Weight.Bold)
        f_ref.setPixelSize(13)
        p.setFont(f_ref)
        p.setPen(cls.C_GREEN if ref_hov else cls.C_MUTED)
        p.drawText(ref_rect, int(Qt.AlignmentFlag.AlignCenter), "⟳")

        # Market Status Tag
        now = datetime.datetime.now()
        is_weekday = now.weekday() < 5
        is_trading_hr = (9 <= now.hour < 16)
        is_market_open = is_weekday and is_trading_hr
        m_txt = "BEI Buka" if is_market_open else "BEI Tutup"
        m_col = cls.C_GREEN if is_market_open else cls.C_MUTED
        f_tag = QFont("Sans", 8, QFont.Weight.Bold)
        f_tag.setPixelSize(9)
        p.setFont(f_tag)
        fm_tag = QFontMetricsF(f_tag)
        m_w = fm_tag.horizontalAdvance(m_txt) + 12.0
        tag_rect = QRectF(pad_x + inner_w - btn_ref_w - m_w - 6.0, y_cur + 1.0, m_w, 18.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(52, 211, 153, 28) if is_market_open else QColor(255, 255, 255, 16))
        p.drawRoundedRect(tag_rect, 4.0, 4.0)
        p.setPen(m_col)
        p.drawText(tag_rect, int(Qt.AlignmentFlag.AlignCenter), m_txt)

        # 2. Card 1: IHSG Overview & Sparkline Chart
        card1_y = y_cur + 22.0
        card1_h = 84.0
        card1_rect = QRectF(pad_x, card1_y, inner_w, card1_h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(cls.C_CARD)
        p.drawRoundedRect(card1_rect, 10.0, 10.0)

        f_name = QFont("Sans", 9, QFont.Weight.Bold)
        f_name.setPixelSize(11)
        p.setFont(f_name)
        p.setPen(cls.C_WHITE)
        p.drawText(QRectF(pad_x + 12.0, card1_y + 6.0, 100.0, 15.0), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), "IHSG (IDX)")

        f_desc = QFont("Sans", 8)
        f_desc.setPixelSize(9)
        p.setFont(f_desc)
        p.setPen(cls.C_MUTED)
        p.drawText(QRectF(pad_x + 76.0, card1_y + 7.0, 180.0, 14.0), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), "Indeks Harga Saham Gabungan")

        if ihsg:
            price_str = f"{ihsg.price:,.2f}"
            chg = ihsg.change
            pct = ihsg.change_pct
            is_up = (chg >= 0)
            col = cls.C_GREEN if is_up else cls.C_RED
            chg_str = f"{'▲ +' if is_up else '▼ '}{chg:.2f} ({'+' if is_up else ''}{pct:.2f}%)"

            # Large Price Text
            f_p = QFont("Sans", 15, QFont.Weight.Bold)
            f_p.setPixelSize(18)
            p.setFont(f_p)
            p.setPen(cls.C_WHITE)
            fm_p = QFontMetricsF(f_p)
            pw = fm_p.horizontalAdvance(price_str)
            p.drawText(QRectF(pad_x + 12.0, card1_y + 21.0, pw + 4.0, 22.0), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), price_str)

            # Change badge
            f_c = QFont("Sans", 8, QFont.Weight.Bold)
            f_c.setPixelSize(10)
            p.setFont(f_c)
            fm_c = QFontMetricsF(f_c)
            cw = fm_c.horizontalAdvance(chg_str) + 10.0
            badge_r = QRectF(pad_x + 16.0 + pw, card1_y + 23.0, cw, 18.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(cls.C_GREEN_BG if is_up else cls.C_RED_BG)
            p.drawRoundedRect(badge_r, 4.0, 4.0)
            p.setPen(col)
            p.drawText(badge_r, int(Qt.AlignmentFlag.AlignCenter), chg_str)

            # Sparkline Chart (if history available)
            chart_rect = QRectF(pad_x + 12.0, card1_y + 45.0, inner_w - 24.0, 18.0)
            history = ihsg.history
            if len(history) >= 2:
                cls._draw_sparkline(p, chart_rect, history, color=col)
            else:
                p.setPen(cls.C_MUTED)
                p.drawLine(QPointF(chart_rect.left(), chart_rect.center().y()), QPointF(chart_rect.right(), chart_rect.center().y()))

            # Stats line (High, Low, Prev Close)
            f_sub = QFont("Sans", 8)
            f_sub.setPixelSize(9)
            p.setFont(f_sub)
            p.setPen(cls.C_MUTED)
            stat_txt = f"Tertinggi: {ihsg.high:,.1f}   •   Terendah: {ihsg.low:,.1f}   •   Penutupan: {ihsg.prev_close:,.1f}"
            p.drawText(QRectF(pad_x + 12.0, card1_y + 66.0, inner_w - 24.0, 13.0), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), stat_txt)
        else:
            f_load = QFont("Sans", 9)
            f_load.setPixelSize(11)
            p.setFont(f_load)
            p.setPen(cls.C_MUTED)
            p.drawText(QRectF(pad_x + 12.0, card1_y + 32.0, 200.0, 20.0), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), "Memuat data IHSG...")

        # 3. Card 2: Kurs Nilai Tukar Rupiah (3 Tiles: USD, SGD, EUR)
        card2_y = card1_y + card1_h + 6.0
        card2_h = 58.0
        tile_gap = 6.0
        tile_w = (inner_w - 2.0 * tile_gap) / 3.0

        for i, citem in enumerate(currs[:3]):
            tx = pad_x + i * (tile_w + tile_gap)
            t_rect = QRectF(tx, card2_y, tile_w, card2_h)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(cls.C_CARD)
            p.drawRoundedRect(t_rect, 8.0, 8.0)

            # Currency Title
            p.setFont(f_name)
            p.setPen(cls.C_WHITE)
            p.drawText(QRectF(tx + 8.0, card2_y + 6.0, tile_w - 16.0, 13.0), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), citem.pair)

            # Rate
            f_rate = QFont("Sans", 10, QFont.Weight.Bold)
            f_rate.setPixelSize(12)
            p.setFont(f_rate)
            p.setPen(cls.C_WHITE)
            p.drawText(QRectF(tx + 8.0, card2_y + 23.0, tile_w - 16.0, 15.0), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), f"Rp {citem.rate:,.0f}")

            # Percentage Change
            is_up = (citem.change >= 0)
            col = cls.C_GREEN if is_up else cls.C_RED
            p.setFont(f_desc)
            p.setPen(col)
            pct_s = f"{'+' if is_up else ''}{citem.change_pct:.2f}%"
            p.drawText(QRectF(tx + 8.0, card2_y + 40.0, tile_w - 16.0, 12.0), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), pct_s)

        # 4. Card 3: Bitcoin (2 Tiles: BTC/IDR & BTC/USD)
        btcs = fdata.btc if fdata else []
        card3_y = card2_y + card2_h + 6.0
        card3_h = 46.0
        btc_w = (inner_w - tile_gap) / 2.0

        for i, bitem in enumerate(btcs[:2]):
            bx = pad_x + i * (btc_w + tile_gap)
            b_rect = QRectF(bx, card3_y, btc_w, card3_h)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(cls.C_CARD)
            p.drawRoundedRect(b_rect, 8.0, 8.0)

            # Bitcoin orange badge + pair
            p.setBrush(QColor(247, 147, 26, 36))
            p.drawEllipse(QPointF(bx + 15.0, card3_y + 14.0), 6.5, 6.5)
            f_btc_sym = QFont("Sans", 7, QFont.Weight.Bold)
            f_btc_sym.setPixelSize(8)
            p.setFont(f_btc_sym)
            p.setPen(QColor(247, 147, 26))
            p.drawText(QRectF(bx + 8.5, card3_y + 7.5, 13.0, 13.0), int(Qt.AlignmentFlag.AlignCenter), "₿")

            p.setFont(f_name)
            p.setPen(cls.C_WHITE)
            p.drawText(QRectF(bx + 25.0, card3_y + 7.0, 68.0, 14.0), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), bitem.pair)

            # 24h % Change on top right
            b_up = (bitem.change_pct >= 0)
            b_col = cls.C_GREEN if b_up else cls.C_RED
            p.setFont(f_desc)
            p.setPen(b_col)
            bpct_s = f"{'+' if b_up else ''}{bitem.change_pct:.2f}%"
            p.drawText(QRectF(bx + btc_w - 58.0, card3_y + 7.0, 50.0, 14.0), int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), bpct_s)

            # Price on bottom row
            f_brate = QFont("Sans", 10, QFont.Weight.Bold)
            f_brate.setPixelSize(12)
            p.setFont(f_brate)
            p.setPen(cls.C_WHITE)
            b_price_str = f"Rp {bitem.rate:,.0f}" if "IDR" in bitem.pair else f"$ {bitem.rate:,.0f}"
            p.drawText(QRectF(bx + 9.0, card3_y + 24.0, btc_w - 18.0, 16.0), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), b_price_str)

        # 5. Footer Row: Last Updated Timestamp
        foot_y = card3_y + card3_h + 3.0
        up_time = datetime.datetime.fromtimestamp(fdata.last_updated).strftime("%H:%M") if fdata and fdata.last_updated else "--:--"
        f_foot = QFont("Sans", 8)
        f_foot.setPixelSize(9)
        p.setFont(f_foot)
        p.setPen(cls.C_MUTED)
        p.drawText(QRectF(pad_x + 4.0, foot_y, inner_w - 8.0, 14.0), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), f"Diperbarui pukul {up_time} WIB  •  Yahoo Finance & CoinGecko")

    @classmethod
    def _draw_panel_area(
        cls,
        p: QPainter,
        w: int,
        h: int,
        ear: float,
        active_tab: str,
        bat: BatteryState,
        notifications: List[NotificationItem],
        dnd: bool,
        kde_autohide: bool,
        monitor_label: str,
        hover_pos: Tuple[float, float],
        hit_boxes: Dict[str, QRectF],
        mpris: Optional[MprisWatcher] = None,
        music_view_mode: str = "player",
        prayer: Optional[PrayerManager] = None,
        prayer_view_mode: str = "schedule",
        standby_display: str = "clock",
        notif_scroll_y: float = 0.0,
        weather: Optional[WeatherManager] = None,
        standby_weather: bool = True,
        is_drag_active: bool = False,
        show_remaining_music_time: bool = False,
        system_ctrl: Optional[SystemControlManager] = None,
        finance: Optional[FinanceManager] = None,
        audio_spectrum: Optional[List[float]] = None,
        privacy: Optional[PrivacyMonitor] = None,
    ):
        hx, hy = hover_pos
        pad_x = ear + 12.0
        inner_w = w - 2 * pad_x

        # ---------------------------------------------------------------------
        # 1. Top Header Bar: [ 🔔  🎵  🌙  ⛅  🚀  🎛️ ] ... [ 🔋 100%   13:00   ⚙️ ]
        # ---------------------------------------------------------------------
        header_y = 3.0
        header_h = 25.0

        # Segmented Icon Tab Track (7 tabs: Notifikasi, Musik, Jadwal Sholat, Cuaca, Keuangan, LocalSend, Control Center)
        tab_bar_w = 205.0
        tab_bar_h = 24.0
        tab_bar_rect = QRectF(pad_x, header_y, tab_bar_w, tab_bar_h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(cls.C_TAB_BG)
        p.drawRoundedRect(tab_bar_rect, 7.0, 7.0)

        btn_w = 26.0
        btn_notif = QRectF(pad_x + 2.0, header_y + 2.0, btn_w, 20.0)
        btn_music = QRectF(pad_x + 31.0, header_y + 2.0, btn_w, 20.0)
        btn_prayer = QRectF(pad_x + 60.0, header_y + 2.0, btn_w, 20.0)
        btn_weather = QRectF(pad_x + 89.0, header_y + 2.0, btn_w, 20.0)
        btn_finance = QRectF(pad_x + 118.0, header_y + 2.0, btn_w, 20.0)
        btn_localsend = QRectF(pad_x + 147.0, header_y + 2.0, btn_w, 20.0)
        btn_control = QRectF(pad_x + 176.0, header_y + 2.0, btn_w, 20.0)
        hit_boxes["tab_notifications"] = btn_notif
        hit_boxes["tab_music"] = btn_music
        hit_boxes["tab_prayer"] = btn_prayer
        hit_boxes["tab_weather"] = btn_weather
        hit_boxes["tab_finance"] = btn_finance
        hit_boxes["tab_localsend"] = btn_localsend
        hit_boxes["tab_control"] = btn_control

        notif_hov = btn_notif.contains(QPointF(hx, hy))
        music_hov = btn_music.contains(QPointF(hx, hy))
        prayer_hov = btn_prayer.contains(QPointF(hx, hy))
        weather_hov = btn_weather.contains(QPointF(hx, hy))
        finance_hov = btn_finance.contains(QPointF(hx, hy))
        localsend_hov = btn_localsend.contains(QPointF(hx, hy))
        control_hov = btn_control.contains(QPointF(hx, hy))

        if active_tab == "notifications":
            p.setBrush(cls.C_TAB_ACTIVE)
            p.drawRoundedRect(btn_notif, 6.0, 6.0)
        elif notif_hov:
            p.setBrush(cls.C_CARD_HOVER)
            p.drawRoundedRect(btn_notif, 6.0, 6.0)

        if active_tab == "music":
            p.setBrush(cls.C_TAB_ACTIVE)
            p.drawRoundedRect(btn_music, 6.0, 6.0)
        elif music_hov:
            p.setBrush(cls.C_CARD_HOVER)
            p.drawRoundedRect(btn_music, 6.0, 6.0)

        if active_tab == "prayer":
            p.setBrush(cls.C_TAB_ACTIVE)
            p.drawRoundedRect(btn_prayer, 6.0, 6.0)
        elif prayer_hov:
            p.setBrush(cls.C_CARD_HOVER)
            p.drawRoundedRect(btn_prayer, 6.0, 6.0)

        if active_tab == "weather":
            p.setBrush(cls.C_TAB_ACTIVE)
            p.drawRoundedRect(btn_weather, 6.0, 6.0)
        elif weather_hov:
            p.setBrush(cls.C_CARD_HOVER)
            p.drawRoundedRect(btn_weather, 6.0, 6.0)

        if active_tab == "finance":
            p.setBrush(cls.C_TAB_ACTIVE)
            p.drawRoundedRect(btn_finance, 6.0, 6.0)
        elif finance_hov:
            p.setBrush(cls.C_CARD_HOVER)
            p.drawRoundedRect(btn_finance, 6.0, 6.0)

        if active_tab == "localsend":
            p.setBrush(cls.C_TAB_ACTIVE)
            p.drawRoundedRect(btn_localsend, 6.0, 6.0)
        elif localsend_hov:
            p.setBrush(cls.C_CARD_HOVER)
            p.drawRoundedRect(btn_localsend, 6.0, 6.0)

        if active_tab == "control":
            p.setBrush(cls.C_TAB_ACTIVE)
            p.drawRoundedRect(btn_control, 6.0, 6.0)
        elif control_hov:
            p.setBrush(cls.C_CARD_HOVER)
            p.drawRoundedRect(btn_control, 6.0, 6.0)

        # Tab 1: Notification icon
        bell_col = cls.C_WHITE if (active_tab == "notifications" or notif_hov) else cls.C_MUTED
        bcx = btn_notif.center().x()
        bcy = btn_notif.center().y()
        cls.draw_notification_icon(p, bcx, bcy, size=12.0, color=bell_col)
        if notifications:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(cls.C_BLUE)
            p.drawEllipse(QPointF(bcx + 4.2, bcy - 4.0), 1.6, 1.6)

        # Tab 2: Music icon
        music_col = (
            cls.C_GREEN
            if (mpris and mpris.is_playing)
            else (cls.C_WHITE if (active_tab == "music" or music_hov) else cls.C_MUTED)
        )
        cls.draw_music_note(
            p,
            btn_music.center().x(),
            btn_music.center().y(),
            size=11.0,
            color=music_col,
        )
        if mpris and mpris.is_playing:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(cls.C_GREEN)
            p.drawEllipse(QPointF(btn_music.center().x() + 4.0, btn_music.center().y() - 4.0), 1.6, 1.6)

        # Tab 3: Crescent Moon (Prayer) icon
        prayer_alert = prayer.check_alert() if prayer else None
        prayer_col = (
            cls.C_GREEN
            if (prayer_alert is not None)
            else (cls.C_WHITE if (active_tab == "prayer" or prayer_hov) else cls.C_MUTED)
        )
        cls.draw_crescent_moon(
            p,
            btn_prayer.center().x(),
            btn_prayer.center().y(),
            size=11.0,
            color=prayer_col,
        )
        if prayer_alert is not None:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(cls.C_GREEN)
            p.drawEllipse(QPointF(btn_prayer.center().x() + 4.0, btn_prayer.center().y() - 4.0), 1.6, 1.6)

        # Tab 4: Weather icon
        w_code = weather.data.weather_code if (weather and weather.data) else 1
        cls.draw_weather_icon(
            p,
            btn_weather.center().x(),
            btn_weather.center().y(),
            code=w_code,
            size=12.0,
        )

        # Tab 5: Finance icon
        fin_col = cls.C_GREEN if (active_tab == "finance" or finance_hov) else cls.C_MUTED
        cls.draw_finance_icon(
            p,
            btn_finance.center().x(),
            btn_finance.center().y(),
            size=11.5,
            color=fin_col,
        )

        # Tab 6: LocalSend icon
        ls_col = cls.C_CYAN if (active_tab == "localsend" or localsend_hov) else cls.C_MUTED
        cls.draw_localsend_icon(
            p,
            btn_localsend.center().x(),
            btn_localsend.center().y(),
            size=11.0,
            color=ls_col,
        )

        # Tab 7: Control Center icon
        ctrl_col = cls.C_WHITE if (active_tab == "control" or control_hov) else cls.C_MUTED
        cls.draw_control_center_icon(
            p,
            btn_control.center().x(),
            btn_control.center().y(),
            size=11.5,
            color=ctrl_col,
        )

        # Top Right elements: Settings [⚙️], Clock [13:00], Battery [🔋 100%]
        right_x = pad_x + inner_w

        # Settings button [⚙️] (Opens standalone Settings window)
        settings_w = 20.0
        settings_rect = QRectF(right_x - settings_w, header_y + 2.0, settings_w, 20.0)
        hit_boxes["btn_settings"] = settings_rect
        set_hov = settings_rect.contains(QPointF(hx, hy))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(cls.C_CARD_HOVER if set_hov else cls.C_TAB_BG)
        p.drawRoundedRect(settings_rect, 6.0, 6.0)
        cls.draw_gear(
            p,
            settings_rect.center().x(),
            settings_rect.center().y(),
            radius=5.2,
            color=cls.C_WHITE if set_hov else cls.C_MUTED,
        )

        # Digital Clock
        f_clk = QFont("Sans", 9, QFont.Weight.Bold)
        f_clk.setPixelSize(11)
        p.setFont(f_clk)
        clock_str = datetime.datetime.now().strftime("%H:%M")
        fm_clk = QFontMetricsF(f_clk)
        clk_w = fm_clk.horizontalAdvance(clock_str)
        clk_rect = QRectF(settings_rect.left() - 10.0 - clk_w, header_y, clk_w, header_h)
        p.setPen(cls.C_WHITE)
        p.drawText(clk_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight), clock_str)

        # Battery indicator: [🔋 100%]
        bat_pct_str = f"{bat.capacity}%"
        f_bat = QFont("Sans", 8, QFont.Weight.Bold)
        f_bat.setPixelSize(10)
        p.setFont(f_bat)
        fm_bat = QFontMetricsF(f_bat)
        bat_txt_w = fm_bat.horizontalAdvance(bat_pct_str)
        bat_icon_w = 18.0
        bat_icon_h = 9.5
        bat_total_w = bat_icon_w + 5.0 + bat_txt_w
        bat_x = clk_rect.left() - 12.0 - bat_total_w
        bat_pill = QRectF(bat_x - 4.0, header_y + 2.0, bat_total_w + 8.0, 20.0)
        hit_boxes["header_battery"] = bat_pill

        # Privacy indicator pill in panel header (to the left of battery)
        if privacy is not None and (privacy.cam_active or privacy.mic_active):
            p_dots = []
            if privacy.cam_active:
                p_dots.append(QColor(34, 197, 94))
            if privacy.mic_active:
                p_dots.append(QColor(249, 115, 22))
            p_apps = privacy.cam_apps or privacy.mic_apps
            p_lbl = p_apps[0] if p_apps else ("Cam" if privacy.cam_active else "Mic")
            f_p = QFont("Sans", 8, QFont.Weight.Bold)
            f_p.setPixelSize(9)
            fm_p = QFontMetricsF(f_p)
            p_lbl_w = fm_p.horizontalAdvance(p_lbl)
            p_dots_w = len(p_dots) * 7.0 + max(0, len(p_dots) - 1) * 1.5
            p_pill_w = p_dots_w + 4.0 + p_lbl_w + 10.0
            p_pill_x = bat_pill.left() - 8.0 - p_pill_w
            if p_pill_x > btn_control.right() + 6.0:
                p_pill = QRectF(p_pill_x, header_y + 3.0, p_pill_w, 18.0)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(p_dots[0].red(), p_dots[0].green(), p_dots[0].blue(), 28))
                p.drawRoundedRect(p_pill, 9.0, 9.0)
                dx_p = p_pill_x + 8.0
                cy_p = p_pill.center().y()
                for col_pd in p_dots:
                    p.setBrush(col_pd)
                    p.drawEllipse(QPointF(dx_p, cy_p), 2.6, 2.6)
                    dx_p += 8.5
                p.setFont(f_p)
                p.setPen(p_dots[0])
                p.drawText(
                    QRectF(p_pill_x + 5.0 + p_dots_w + 3.0, header_y + 3.0, p_lbl_w + 2.0, 18.0),
                    int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                    p_lbl,
                )
        bat_hov = bat_pill.contains(QPointF(hx, hy))
        if bat_hov:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(cls.C_TAB_BG)
            p.drawRoundedRect(bat_pill, 5.0, 5.0)

        is_green = bat.is_full or bat.is_charging or bat.is_plugged_idle
        b_main_col = cls.C_GREEN if is_green else (cls.C_AMBER if bat.is_low else cls.C_WHITE)
        b_track_col = cls.C_GREEN_BG if is_green else (cls.C_AMBER_BG if bat.is_low else QColor(255, 255, 255, 30))

        bx = bat_x
        by = header_y + (header_h - bat_icon_h) / 2.0
        bw = bat_icon_w - 3.0
        p.setPen(QPen(b_main_col, 1.0))
        p.setBrush(b_track_col)
        p.drawRoundedRect(QRectF(bx, by, bw, bat_icon_h), 2.2, 2.2)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(b_main_col)
        nub_h = bat_icon_h * 0.45
        p.drawRoundedRect(QRectF(bx + bw + 1.0, by + (bat_icon_h - nub_h) / 2.0, 1.5, nub_h), 0.7, 0.7)

        pad = 1.4
        avail_w = max(0.0, bw - pad * 2.0)
        fill_w = max(1.2, avail_w * (bat.capacity / 100.0))
        p.drawRoundedRect(QRectF(bx + pad, by + pad, fill_w, bat_icon_h - pad * 2.0), 1.2, 1.2)

        cx_b = bx + bw / 2.0
        cy_b = by + bat_icon_h / 2.0
        if bat.is_full:
            pen_chk = QPen(cls.C_BLACK, 1.3)
            pen_chk.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen_chk)
            chk = QPainterPath()
            chk.moveTo(cx_b - 2.4, cy_b + 0.1)
            chk.lineTo(cx_b - 0.6, cy_b + 1.9)
            chk.lineTo(cx_b + 2.6, cy_b - 1.8)
            p.drawPath(chk)
        elif bat.is_charging or bat.is_plugged_idle:
            bolt = QPainterPath()
            bolt.moveTo(cx_b + 0.6, by + 1.0)
            bolt.lineTo(cx_b - 2.2, cy_b + 0.3)
            bolt.lineTo(cx_b - 0.2, cy_b + 0.3)
            bolt.lineTo(cx_b - 0.8, by + bat_icon_h - 1.0)
            bolt.lineTo(cx_b + 2.2, cy_b - 0.3)
            bolt.lineTo(cx_b + 0.2, cy_b - 0.3)
            bolt.closeSubpath()
            p.setPen(QPen(cls.C_BLACK, 0.6))
            p.setBrush(cls.C_WHITE if bat.capacity < 50 else cls.C_BLACK)
            p.drawPath(bolt)

        p.setFont(f_bat)
        p.setPen(b_main_col)
        p.drawText(
            QRectF(bx + bat_icon_w + 4.0, header_y, bat_txt_w + 2.0, header_h),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            bat_pct_str,
        )

        # Subtle divider below header
        p.setPen(QPen(QColor(255, 255, 255, 18), 1.0))
        p.drawLine(QPointF(pad_x, 32.0), QPointF(pad_x + inner_w, 32.0))

        # ---------------------------------------------------------------------
        # 2. Tab Content (Starts at y_cur = 36.0, directly below 32px header)
        # ---------------------------------------------------------------------
        y_cur = 36.0

        # 2A. TAB: NOTIFIKASI
        if active_tab == "notifications":
            if not notifications:
                avail_h = max(30.0, h - y_cur - 12.0)
                empty_rect = QRectF(pad_x, y_cur, inner_w, avail_h)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(cls.C_CARD)
                p.drawRoundedRect(empty_rect, 10.0, 10.0)

                f_em = QFont("Sans", 9, QFont.Weight.Bold)
                f_em.setPixelSize(11)
                p.setFont(f_em)
                p.setPen(cls.C_WHITE)
                p.drawText(
                    QRectF(pad_x, y_cur + (avail_h - 34.0) / 2.0, inner_w, 18.0),
                    int(Qt.AlignmentFlag.AlignCenter),
                    "Belum Ada Notifikasi Baru",
                )

                f_ems = QFont("Sans", 8)
                f_ems.setPixelSize(9)
                p.setFont(f_ems)
                p.setPen(cls.C_MUTED)
                p.drawText(
                    QRectF(pad_x + 16.0, y_cur + (avail_h - 34.0) / 2.0 + 17.0, inner_w - 32.0, 16.0),
                    int(Qt.AlignmentFlag.AlignCenter),
                    "Notifikasi aplikasi & perangkat akan tampil di sini.",
                )
            else:
                card_h = 56.0
                gap = 6.0
                clr_h = 22.0
                clr_pad_bottom = 8.0
                clr_rect = QRectF(pad_x + inner_w - 116.0, h - (clr_h + clr_pad_bottom), 116.0, clr_h)
                hit_boxes["btn_clear"] = clr_rect
                clr_hov = clr_rect.contains(QPointF(hx, hy))

                list_top = y_cur
                list_bottom = clr_rect.top() - 6.0
                visible_list_h = max(20.0, list_bottom - list_top)
                list_clip_rect = QRectF(pad_x, list_top, inner_w, visible_list_h)

                n_items = len(notifications)
                total_content_h = n_items * card_h + max(0, n_items - 1) * gap
                max_scroll = max(0.0, total_content_h - visible_list_h)
                scroll_y = max(0.0, min(max_scroll, float(notif_scroll_y)))

                p.save()
                p.setClipRect(list_clip_rect)

                # Pre-instantiate fonts and metrics outside the item loop for maximum scroll performance
                f_del = QFont("Sans", 8, QFont.Weight.Bold)
                f_del.setPixelSize(10)
                f_ct = QFont("Sans", 9, QFont.Weight.Bold)
                f_ct.setPixelSize(11)
                fm_ct = QFontMetricsF(f_ct)
                f_cb = QFont("Sans", 8)
                f_cb.setPixelSize(10)
                fm_cb = QFontMetricsF(f_cb)
                f_cm = QFont("Sans", 8)
                f_cm.setPixelSize(9)
                fm_cm = QFontMetricsF(f_cm)

                for idx, item in enumerate(notifications):
                    cy_card = list_top - scroll_y + idx * (card_h + gap)
                    if cy_card + card_h < list_top - 5.0 or cy_card > list_bottom + 5.0:
                        continue

                    c_rect = QRectF(pad_x, cy_card, inner_w, card_h)
                    del_rect = QRectF(pad_x + inner_w - 22.0, cy_card + 8.0, 15.0, 15.0)
                    del_hov = del_rect.contains(QPointF(hx, hy)) and list_clip_rect.contains(QPointF(hx, hy))
                    card_hov = c_rect.contains(QPointF(hx, hy)) and list_clip_rect.contains(QPointF(hx, hy)) and not del_hov
                    card_hit = c_rect.intersected(list_clip_rect)
                    if not card_hit.isEmpty():
                        hit_boxes[f"notif_card_{item.id}"] = card_hit
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(cls.C_CARD_HOVER if card_hov else cls.C_CARD)
                    p.drawRoundedRect(c_rect, 10.0, 10.0)

                    # Real App Icon (cached on item)
                    icon_size = 34.0
                    ic_rect = QRectF(pad_x + 8.0, cy_card + (card_h - icon_size) / 2.0, icon_size, icon_size)
                    if getattr(item, "resolved_icon", None) is None:
                        item.resolved_icon = IconResolver.get_icon_pixmap(item.app_icon, item.app_name, int(icon_size))
                    pm = item.resolved_icon
                    if pm and not pm.isNull():
                        p.drawPixmap(ic_rect.toRect(), pm)
                    else:
                        p.setPen(Qt.PenStyle.NoPen)
                        p.setBrush(QColor(255, 255, 255, 14))
                        p.drawRoundedRect(ic_rect, 8.0, 8.0)
                        cls.draw_notification_icon(p, ic_rect.center().x(), ic_rect.center().y(), size=14.0, color=cls.C_MUTED)

                    # Delete Button (top right of card)
                    if list_clip_rect.contains(del_rect.center()):
                        hit_boxes[f"del_{item.id}"] = del_rect
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(cls.C_CARD_HOVER if del_hov else QColor(255, 255, 255, 14))
                    p.drawEllipse(del_rect)

                    p.setFont(f_del)
                    p.setPen(cls.C_WHITE if del_hov else cls.C_MUTED)
                    p.drawText(del_rect, int(Qt.AlignmentFlag.AlignCenter), "×")

                    # Text Area (Vertically Stacked - Never Overlapping)
                    text_x = pad_x + 8.0 + icon_size + 10.0
                    text_w = inner_w - (8.0 + icon_size + 10.0) - 24.0

                    elided = getattr(item, "_elided_cache", None)
                    if elided is None or elided[0] != text_w:
                        head_txt = fm_ct.elidedText(item.summary, Qt.TextElideMode.ElideRight, text_w)
                        body_txt = fm_cb.elidedText(
                            item.body or "Tidak ada teks tambahan", Qt.TextElideMode.ElideRight, text_w
                        )
                        meta_txt = f"{item.app_name} • {item.timestamp}"
                        elided_meta = fm_cm.elidedText(meta_txt, Qt.TextElideMode.ElideRight, text_w)
                        elided = (text_w, head_txt, body_txt, elided_meta)
                        item._elided_cache = elided
                    else:
                        _, head_txt, body_txt, elided_meta = elided

                    # Row 1: Judul Notif (Full text_w, NO jam sejajar)
                    p.setFont(f_ct)
                    p.setPen(cls.C_WHITE)
                    p.drawText(
                        QRectF(text_x, cy_card + 6.0, text_w, 15.0),
                        int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                        head_txt,
                    )

                    # Row 2: Isi Notif
                    p.setFont(f_cb)
                    p.setPen(cls.C_WHITE_SEC)
                    p.drawText(
                        QRectF(text_x, cy_card + 22.0, text_w, 14.0),
                        int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                        body_txt,
                    )

                    # Row 3: Jam & Nama Aplikasi (Dibawahnya saja)
                    p.setFont(f_cm)
                    p.setPen(cls.C_MUTED)
                    p.drawText(
                        QRectF(text_x, cy_card + 37.0, text_w, 13.0),
                        int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                        elided_meta,
                    )

                p.restore()

                # Subtle vertical scrollbar indicator if scrollable
                if max_scroll > 0.0:
                    sb_w = 3.0
                    sb_x = pad_x + inner_w - sb_w - 1.0
                    sb_track_h = visible_list_h
                    sb_thumb_h = max(18.0, sb_track_h * (visible_list_h / total_content_h))
                    sb_ratio = scroll_y / max_scroll if max_scroll > 0 else 0.0
                    sb_thumb_y = list_top + sb_ratio * (sb_track_h - sb_thumb_h)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QColor(255, 255, 255, 60))
                    p.drawRoundedRect(QRectF(sb_x, sb_thumb_y, sb_w, sb_thumb_h), 1.5, 1.5)

                # "Bersihkan Semua" pill
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(cls.C_CARD_HOVER if clr_hov else cls.C_CARD)
                p.drawRoundedRect(clr_rect, 7.0, 7.0)
                f_clr = QFont("Sans", 8, QFont.Weight.Bold)
                f_clr.setPixelSize(9)
                p.setFont(f_clr)
                p.setPen(cls.C_WHITE if clr_hov else cls.C_MUTED)
                p.drawText(clr_rect, int(Qt.AlignmentFlag.AlignCenter), "Bersihkan Semua")

        # 2B. TAB: MUSIK (Minimalist Apple/Dynamic Island Player)
        elif active_tab == "music":
            if not mpris or (not mpris.title and not mpris.active_player):
                avail_h = h - y_cur - 12.0
                empty_rect = QRectF(pad_x, y_cur, inner_w, avail_h)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(cls.C_CARD)
                p.drawRoundedRect(empty_rect, 11.0, 11.0)

                cls.draw_music_note(
                    p,
                    empty_rect.center().x(),
                    y_cur + (avail_h - 40.0) / 2.0 - 10.0,
                    size=28.0,
                    color=cls.C_MUTED,
                )
                f_em = QFont("Sans", 9, QFont.Weight.Bold)
                f_em.setPixelSize(12)
                p.setFont(f_em)
                p.setPen(cls.C_WHITE)
                p.drawText(
                    QRectF(pad_x, y_cur + (avail_h - 40.0) / 2.0 + 10.0, inner_w, 20.0),
                    int(Qt.AlignmentFlag.AlignCenter),
                    "Tidak Ada Musik yang Diputar",
                )

                f_ems = QFont("Sans", 8)
                f_ems.setPixelSize(10)
                p.setFont(f_ems)
                p.setPen(cls.C_MUTED)
                p.drawText(
                    QRectF(pad_x + 16.0, y_cur + (avail_h - 40.0) / 2.0 + 32.0, inner_w - 32.0, 18.0),
                    int(Qt.AlignmentFlag.AlignCenter),
                    "Buka Spotify, YouTube, atau pemutar media untuk kontrol.",
                )
            else:
                # Row 1: Cover Art (40x40) + Title + Artist + Equalizer Waveform
                art_size = 40.0
                art_rect = QRectF(pad_x + 6.0, y_cur + 4.0, art_size, art_size)
                cover_img = mpris.get_cover_image()
                p.save()
                art_path = QPainterPath()
                art_path.addRoundedRect(art_rect, 8.0, 8.0)
                p.setClipPath(art_path)
                if cover_img:
                    scaled = cover_img.scaled(
                        int(art_size),
                        int(art_size),
                        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    sx = art_rect.left() + (art_size - scaled.width()) / 2.0
                    sy = art_rect.top() + (art_size - scaled.height()) / 2.0
                    p.drawImage(QPointF(sx, sy), scaled)
                else:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(cls.C_CARD_HOVER)
                    p.drawRect(art_rect)
                    cls.draw_music_note(
                        p,
                        art_rect.center().x(),
                        art_rect.center().y(),
                        size=18.0,
                        color=cls.C_GREEN,
                    )
                p.restore()

                eq_w = 22.0
                info_x = pad_x + 6.0 + art_size + 10.0
                avail_title_w = inner_w - (art_size + 16.0 + eq_w + 10.0)

                f_title = QFont("Sans", 9, QFont.Weight.Bold)
                f_title.setPixelSize(12)
                p.setFont(f_title)
                fm_title = QFontMetricsF(f_title)
                title_raw = mpris.title or "Musik Sedang Diputar"
                title_txt = fm_title.elidedText(title_raw, Qt.TextElideMode.ElideRight, avail_title_w)
                p.setPen(cls.C_WHITE)
                p.drawText(
                    QRectF(info_x, y_cur + 4.0, avail_title_w, 18.0),
                    int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                    title_txt,
                )

                f_art = QFont("Sans", 8)
                f_art.setPixelSize(10)
                p.setFont(f_art)
                fm_art = QFontMetricsF(f_art)
                art_raw = mpris.artist or mpris.album or (mpris.player_name or "Media Player")
                art_txt = fm_art.elidedText(art_raw, Qt.TextElideMode.ElideRight, avail_title_w)
                p.setPen(cls.C_MUTED)
                p.drawText(
                    QRectF(info_x, y_cur + 24.0, avail_title_w, 16.0),
                    int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                    art_txt,
                )

                # Equalizer Waveform on far right of Row 1
                eq_cx = pad_x + inner_w - 14.0
                eq_cy = y_cur + 24.0
                now_sec = time.time()
                is_play = mpris.is_playing
                use_real_spec = is_play and audio_spectrum is not None and len(audio_spectrum) >= 4
                p.setPen(Qt.PenStyle.NoPen)
                b_col = cls.C_GREEN if is_play else cls.C_MUTED
                p.setBrush(b_col)
                for bi, b_offset in enumerate([-7.0, -2.5, 2.0, 6.5]):
                    if use_real_spec:
                        lvl = max(0.0, min(1.0, float(audio_spectrum[bi])))
                        bh_anim = 3.0 + 11.0 * lvl
                    elif is_play:
                        bh_anim = 4.0 + 8.0 * (0.5 + 0.5 * math.sin(now_sec * 8.0 + bi * 1.5))
                    else:
                        bh_anim = 3.0
                    p.drawRoundedRect(
                        QRectF(eq_cx + b_offset, eq_cy - bh_anim / 2.0, 2.2, bh_anim),
                        1.0,
                        1.0,
                    )

                # Row 2: Elapsed Time + Thin Progress Bar + End Time (or Remaining Time)
                y_p = y_cur + 54.0
                s_cur, s_tot = mpris.time_strings
                s_rem = mpris.remaining_time_str
                show_rem = bool(show_remaining_music_time)
                s_right = s_rem if show_rem else s_tot

                f_time = QFont("Sans", 8)
                f_time.setPixelSize(9)
                p.setFont(f_time)
                p.setPen(cls.C_WHITE_SEC)
                fm_time = QFontMetricsF(f_time)

                time_w = max(38.0, fm_time.horizontalAdvance(s_cur) + 4.0, fm_time.horizontalAdvance(s_right) + 4.0)
                p.drawText(
                    QRectF(pad_x + 6.0, y_p - 1.0, time_w, 14.0),
                    int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                    s_cur,
                )
                right_rect = QRectF(pad_x + inner_w - time_w - 6.0, y_p - 1.0, time_w, 14.0)
                p.drawText(
                    right_rect,
                    int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                    s_right,
                )
                hit_boxes["music_time_toggle"] = QRectF(right_rect.left() - 4.0, y_p - 5.0, time_w + 8.0, 24.0)

                # Progress Track with Interactive Seek Hit Box
                bar_x = pad_x + 6.0 + time_w + 6.0
                bar_w = inner_w - (time_w + 6.0) * 2.0
                prog_bar_rect = QRectF(bar_x, y_p + 4.5, bar_w, 3.5)
                hit_boxes["music_timeline"] = QRectF(bar_x, y_p - 5.0, bar_w, 22.0)

                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 255, 255, 40))
                p.drawRoundedRect(prog_bar_rect, 1.75, 1.75)

                ratio = mpris.progress_ratio
                fill_w = max(3.5, min(bar_w, bar_w * ratio))
                prog_fill = QRectF(bar_x, y_p + 4.5, fill_w, 3.5)
                p.setBrush(cls.C_WHITE)
                p.drawRoundedRect(prog_fill, 1.75, 1.75)

                # Scrubber knob at current position
                scrub_x = bar_x + fill_w
                p.setBrush(cls.C_WHITE)
                p.drawEllipse(QPointF(scrub_x, y_p + 6.25), 3.2, 3.2)

                # Row 3: Prev | Play/Pause | Next (Centered, clean minimal controls)
                y_c = y_cur + 88.0
                cx_mid = pad_x + inner_w / 2.0

                # Prev Button
                prev_rect = QRectF(cx_mid - 62.0, y_c - 15.0, 30.0, 30.0)
                hit_boxes["music_prev"] = prev_rect
                prev_hov = prev_rect.contains(QPointF(hx, hy))
                if prev_hov:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(cls.C_CARD_HOVER)
                    p.drawRoundedRect(prev_rect, 7.0, 7.0)
                cls.draw_prev_next(
                    p,
                    prev_rect.center().x(),
                    prev_rect.center().y(),
                    is_next=False,
                    size=14.0,
                    color=cls.C_WHITE if prev_hov else cls.C_WHITE_SEC,
                )

                # Play / Pause Button
                play_rect = QRectF(cx_mid - 18.0, y_c - 17.0, 36.0, 36.0)
                hit_boxes["music_play_pause"] = play_rect
                play_hov = play_rect.contains(QPointF(hx, hy))
                if play_hov:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(cls.C_CARD_HOVER)
                    p.drawRoundedRect(play_rect, 8.0, 8.0)
                cls.draw_play_pause(
                    p,
                    play_rect.center().x(),
                    play_rect.center().y(),
                    playing=mpris.is_playing,
                    size=17.0,
                    color=cls.C_WHITE,
                )

                # Next Button
                next_rect = QRectF(cx_mid + 32.0, y_c - 15.0, 30.0, 30.0)
                hit_boxes["music_next"] = next_rect
                next_hov = next_rect.contains(QPointF(hx, hy))
                if next_hov:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(cls.C_CARD_HOVER)
                    p.drawRoundedRect(next_rect, 7.0, 7.0)
                cls.draw_prev_next(
                    p,
                    next_rect.center().x(),
                    next_rect.center().y(),
                    is_next=True,
                    size=14.0,
                    color=cls.C_WHITE if next_hov else cls.C_WHITE_SEC,
                )

                # Lyrics Toggle Button (Default tertutup, klik untuk tampilkan/sembunyikan)
                btn_lyr_w = 60.0
                btn_lyr_h = 24.0
                btn_lyr_x = pad_x + inner_w - btn_lyr_w - 4.0
                btn_lyr_y = y_c - 12.0
                btn_lyr_rect = QRectF(btn_lyr_x, btn_lyr_y, btn_lyr_w, btn_lyr_h)
                hit_boxes["music_lyrics_toggle"] = btn_lyr_rect
                btn_lyr_hov = btn_lyr_rect.contains(QPointF(hx, hy))
                is_lyr_open = (music_view_mode == "lyrics")

                p.setPen(Qt.PenStyle.NoPen)
                if is_lyr_open:
                    p.setBrush(cls.C_GREEN_BG)
                elif btn_lyr_hov:
                    p.setBrush(cls.C_CARD_HOVER)
                else:
                    p.setBrush(cls.C_TAB_BG)
                p.drawRoundedRect(btn_lyr_rect, 6.0, 6.0)

                if is_lyr_open:
                    p.setPen(QPen(cls.C_GREEN, 1.0))
                    p.drawRoundedRect(btn_lyr_rect, 6.0, 6.0)

                f_btn_l = QFont("Sans", 8, QFont.Weight.Bold)
                f_btn_l.setPixelSize(10)
                p.setFont(f_btn_l)
                p.setPen(cls.C_GREEN if is_lyr_open else (cls.C_WHITE if btn_lyr_hov else cls.C_MUTED))
                p.drawText(
                    btn_lyr_rect,
                    int(Qt.AlignmentFlag.AlignCenter),
                    "✓ Lirik" if is_lyr_open else "♫ Lirik",
                )

                # Row 4: Live Karaoke Lyrics Card (Hanya muncul jika tombol Lirik aktif)
                if is_lyr_open:
                    y_lyr = y_cur + 112.0
                    lyr_card_h = 56.0
                    lyr_rect = QRectF(pad_x + 4.0, y_lyr, inner_w - 8.0, lyr_card_h)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(cls.C_CARD)
                    p.drawRoundedRect(lyr_rect, 8.0, 8.0)

                    cur_l, next_l, _ = mpris.get_current_lyric()
                    f_cur_l = QFont("Sans", 9, QFont.Weight.Bold)
                    f_cur_l.setPixelSize(11)
                    fm_cur_l = QFontMetricsF(f_cur_l)

                    f_nxt_l = QFont("Sans", 8)
                    f_nxt_l.setPixelSize(9)
                    fm_nxt_l = QFontMetricsF(f_nxt_l)

                    if mpris.lyrics_loading:
                        p.setFont(f_nxt_l)
                        p.setPen(cls.C_MUTED)
                        p.drawText(lyr_rect, int(Qt.AlignmentFlag.AlignCenter), "Memuat lirik...")
                    elif cur_l or next_l:
                        disp_cur = fm_cur_l.elidedText(cur_l or "♫ ...", Qt.TextElideMode.ElideRight, lyr_rect.width() - 16.0)
                        p.setFont(f_cur_l)
                        p.setPen(cls.C_GREEN)
                        p.drawText(
                            QRectF(lyr_rect.left() + 8.0, lyr_rect.top() + 6.0, lyr_rect.width() - 16.0, 20.0),
                            int(Qt.AlignmentFlag.AlignCenter),
                            disp_cur,
                        )
                        if next_l:
                            disp_nxt = fm_nxt_l.elidedText(next_l, Qt.TextElideMode.ElideRight, lyr_rect.width() - 16.0)
                            p.setFont(f_nxt_l)
                            p.setPen(cls.C_MUTED)
                            p.drawText(
                                QRectF(lyr_rect.left() + 8.0, lyr_rect.top() + 28.0, lyr_rect.width() - 16.0, 18.0),
                                int(Qt.AlignmentFlag.AlignCenter),
                                disp_nxt,
                            )
                    else:
                        p.setFont(f_nxt_l)
                        p.setPen(cls.C_MUTED)
                        p.drawText(lyr_rect, int(Qt.AlignmentFlag.AlignCenter), "Lirik tidak tersedia")

        # 2C. TAB: JADWAL SHOLAT (Dedicated Prayer Times Tab)
        elif active_tab == "prayer":
            city_name = prayer.current_city["name"] if prayer else "Jakarta"

            if prayer_view_mode == "select_city":
                # Select City Grid View
                back_rect = QRectF(pad_x + 2.0, y_cur + 2.0, 72.0, 22.0)
                hit_boxes["btn_back_prayer"] = back_rect
                b_hov = back_rect.contains(QPointF(hx, hy))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(cls.C_CARD_HOVER if b_hov else cls.C_TAB_BG)
                p.drawRoundedRect(back_rect, 6.0, 6.0)
                f_bk = QFont("Sans", 8, QFont.Weight.Bold)
                f_bk.setPixelSize(9)
                p.setFont(f_bk)
                p.setPen(cls.C_WHITE if b_hov else cls.C_MUTED)
                p.drawText(back_rect, int(Qt.AlignmentFlag.AlignCenter), "← Kembali")

                f_hd = QFont("Sans", 9, QFont.Weight.Bold)
                f_hd.setPixelSize(11)
                p.setFont(f_hd)
                p.setPen(cls.C_WHITE)
                p.drawText(
                    QRectF(pad_x + 82.0, y_cur + 2.0, inner_w - 82.0, 22.0),
                    int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                    "Pilih Wilayah / Kota",
                )

                # 2-column grid of cities
                top_cities = [
                    "Banyumas", "Jakarta", "Bandung", "Surabaya",
                    "Semarang", "Yogyakarta", "Medan", "Palembang",
                    "Makassar", "Denpasar", "Balikpapan", "Banda Aceh"
                ]
                col_w = (inner_w - 8.0) / 2.0
                row_h = 28.0
                gap_y = 5.0
                grid_y0 = y_cur + 30.0

                for i, c_name in enumerate(top_cities):
                    col = i % 2
                    row = i // 2
                    cx_cell = pad_x + col * (col_w + 8.0)
                    cy_cell = grid_y0 + row * (row_h + gap_y)
                    c_rect = QRectF(cx_cell, cy_cell, col_w, row_h)

                    c_key = f"sel_city_{c_name}"
                    hit_boxes[c_key] = c_rect
                    c_hov = c_rect.contains(QPointF(hx, hy))
                    is_cur = (c_name.lower() == city_name.lower())

                    p.setPen(Qt.PenStyle.NoPen)
                    if is_cur:
                        p.setBrush(cls.C_GREEN_BG)
                    elif c_hov:
                        p.setBrush(cls.C_CARD_HOVER)
                    else:
                        p.setBrush(cls.C_CARD)
                    p.drawRoundedRect(c_rect, 6.0, 6.0)

                    if is_cur:
                        p.setPen(QPen(cls.C_GREEN, 1.0))
                        p.drawRoundedRect(c_rect, 6.0, 6.0)

                    f_cn = QFont("Sans", 8, QFont.Weight.Bold if is_cur else QFont.Weight.Normal)
                    f_cn.setPixelSize(10)
                    p.setFont(f_cn)
                    p.setPen(cls.C_GREEN if is_cur else (cls.C_WHITE if c_hov else cls.C_WHITE_SEC))
                    p.drawText(
                        QRectF(cx_cell + 8.0, cy_cell, col_w - 28.0, row_h),
                        int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                        c_name,
                    )

                    if is_cur:
                        p.drawText(
                            QRectF(cx_cell + col_w - 20.0, cy_cell, 14.0, row_h),
                            int(Qt.AlignmentFlag.AlignCenter),
                            "✓",
                        )
            else:
                # Schedule Mode
                # Row 1: Header
                f_sh = QFont("Sans", 9, QFont.Weight.Bold)
                f_sh.setPixelSize(11)
                p.setFont(f_sh)
                p.setPen(cls.C_WHITE)
                p.drawText(
                    QRectF(pad_x + 4.0, y_cur + 2.0, 140.0, 22.0),
                    int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                    "Jadwal Waktu Sholat",
                )

                # City Selector Button: [ Jakarta ▾ ]
                city_btn = QRectF(pad_x + inner_w - 110.0, y_cur + 2.0, 110.0, 22.0)
                hit_boxes["btn_select_city"] = city_btn
                c_hov = city_btn.contains(QPointF(hx, hy))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(cls.C_CARD_HOVER if c_hov else cls.C_TAB_BG)
                p.drawRoundedRect(city_btn, 6.0, 6.0)

                f_cb = QFont("Sans", 8, QFont.Weight.Bold)
                f_cb.setPixelSize(10)
                p.setFont(f_cb)
                p.setPen(cls.C_GREEN if c_hov else cls.C_WHITE)
                p.drawText(
                    city_btn,
                    int(Qt.AlignmentFlag.AlignCenter),
                    f"{city_name} ▾",
                )

                # Next Prayer Hero Countdown Card
                nxt_name, nxt_time, nxt_left = prayer.get_next_prayer() if prayer else ("Sholat", "--:--", 0)
                alert = prayer.check_alert() if prayer else None

                hero_rect = QRectF(pad_x, y_cur + 30.0, inner_w, 42.0)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(cls.C_GREEN_BG if alert else cls.C_CARD)
                p.drawRoundedRect(hero_rect, 9.0, 9.0)

                if alert:
                    p.setPen(QPen(cls.C_GREEN, 1.0))
                    p.drawRoundedRect(hero_rect, 9.0, 9.0)

                cls.draw_crescent_moon(
                    p,
                    pad_x + 22.0,
                    y_cur + 51.0,
                    size=16.0,
                    color=cls.C_GREEN,
                )

                # Format countdown in hours, minutes, seconds
                rem_sec = max(0, int(nxt_left))
                h_left = rem_sec // 3600
                m_left = (rem_sec % 3600) // 60
                s_left = rem_sec % 60

                if h_left > 0:
                    cd_str = f"{h_left} jam {m_left} menit {s_left} detik lagi"
                elif m_left > 0:
                    cd_str = f"{m_left} menit {s_left} detik lagi"
                else:
                    cd_str = f"{s_left} detik lagi"

                f_hn = QFont("Sans", 9, QFont.Weight.Bold)
                f_hn.setPixelSize(11)
                p.setFont(f_hn)
                p.setPen(cls.C_WHITE)
                hero_title = (
                    f"Waktu Adzan {alert.name} Sedang Berlangsung"
                    if (alert and alert.is_adzan)
                    else f"Berikutnya: {nxt_name}"
                )
                p.drawText(
                    QRectF(pad_x + 40.0, y_cur + 34.0, inner_w - 90.0, 16.0),
                    int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                    hero_title,
                )

                f_hs = QFont("Sans", 8)
                f_hs.setPixelSize(9)
                p.setFont(f_hs)
                p.setPen(cls.C_GREEN if alert else cls.C_MUTED)
                hero_sub = (
                    "Selamat menunaikan ibadah sholat"
                    if (alert and alert.is_adzan)
                    else f"Pukul {nxt_time} • {cd_str}"
                )
                p.drawText(
                    QRectF(pad_x + 40.0, y_cur + 51.0, inner_w - 90.0, 15.0),
                    int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                    hero_sub,
                )

                # Hero Time Badge
                time_badge = QRectF(pad_x + inner_w - 56.0, y_cur + 38.0, 48.0, 24.0)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(cls.C_GREEN if alert else QColor(255, 255, 255, 16))
                p.drawRoundedRect(time_badge, 5.0, 5.0)
                f_tb = QFont("Sans", 8, QFont.Weight.Bold)
                f_tb.setPixelSize(10)
                p.setFont(f_tb)
                p.setPen(cls.C_BLACK if alert else cls.C_WHITE)
                p.drawText(time_badge, int(Qt.AlignmentFlag.AlignCenter), nxt_time)

                # 5 Prayer Rows (Subuh, Dzuhur, Ashar, Maghrib, Isya)
                prayer_list = []
                for pname in ["Subuh", "Dzuhur", "Ashar", "Maghrib", "Isya"]:
                    if prayer:
                        t_val = prayer.today_times.get(pname) or prayer.today_times.get(pname.lower())
                        if t_val:
                            ptime_str = t_val[0] if isinstance(t_val, (tuple, list)) else str(t_val)
                        else:
                            ptime_str = "--:--"
                    else:
                        ptime_str = "--:--"
                    prayer_list.append((pname, ptime_str))

                p_row_y = y_cur + 80.0
                p_row_h = 28.0
                p_row_gap = 4.0
                for pi, (pname, ptime) in enumerate(prayer_list):
                    ry = p_row_y + pi * (p_row_h + p_row_gap)
                    r_box = QRectF(pad_x, ry, inner_w, p_row_h)
                    is_active = (pname.lower() == nxt_name.lower())

                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(cls.C_GREEN_BG if is_active else cls.C_CARD)
                    p.drawRoundedRect(r_box, 7.0, 7.0)

                    if is_active:
                        p.setPen(QPen(cls.C_GREEN, 0.8))
                        p.drawRoundedRect(r_box, 7.0, 7.0)

                    # Dot indicator
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(cls.C_GREEN if is_active else cls.C_MUTED)
                    p.drawEllipse(QPointF(pad_x + 14.0, ry + p_row_h / 2.0), 2.5, 2.5)

                    f_pn = QFont("Sans", 8, QFont.Weight.Bold if is_active else QFont.Weight.Normal)
                    f_pn.setPixelSize(10)
                    p.setFont(f_pn)
                    p.setPen(cls.C_WHITE if is_active else cls.C_WHITE_SEC)
                    p.drawText(
                        QRectF(pad_x + 26.0, ry, 120.0, p_row_h),
                        int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                        pname,
                    )

                    f_pt = QFont("Sans", 8, QFont.Weight.Bold if is_active else QFont.Weight.Normal)
                    f_pt.setPixelSize(11)
                    p.setFont(f_pt)
                    p.setPen(cls.C_GREEN if is_active else cls.C_WHITE)
                    p.drawText(
                        QRectF(pad_x + inner_w - 70.0, ry, 58.0, p_row_h),
                        int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                        ptime,
                    )

        # 2D. TAB: CUACA (Weather Forecast & Current Conditions)
        elif active_tab == "weather":
            cls._draw_weather_tab(
                p=p,
                w=w,
                h=h,
                ear=ear,
                weather=weather,
                prayer=prayer,
                hover_pos=hover_pos,
                hit_boxes=hit_boxes,
            )

        # 2E. TAB: KEUANGAN (Pasar Saham & Valuta Asing)
        elif active_tab == "finance":
            cls._draw_finance_tab(
                p=p,
                w=w,
                h=h,
                ear=ear,
                finance=finance,
                hover_pos=hover_pos,
                hit_boxes=hit_boxes,
            )

        # 2F. TAB: LOCALSEND (File Sharing & Drop Zone)
        elif active_tab == "localsend":
            cls._draw_localsend_tab(
                p=p,
                w=w,
                h=h,
                ear=ear,
                hover_pos=hover_pos,
                hit_boxes=hit_boxes,
                is_drag_active=is_drag_active,
            )
        # 2G. TAB: CONTROL CENTER (Pintasan Sistem Cepat)
        elif active_tab == "control":
            cls._draw_control_tab(
                p=p,
                w=w,
                h=h,
                ear=ear,
                system_ctrl=system_ctrl,
                hover_pos=hover_pos,
                hit_boxes=hit_boxes,
            )
        else:
            cls._draw_weather_tab(
                p=p,
                w=w,
                h=h,
                ear=ear,
                weather=weather,
                prayer=prayer,
                hover_pos=hover_pos,
                hit_boxes=hit_boxes,
            )

    @classmethod
    def _draw_localsend_tab(
        cls,
        p: QPainter,
        w: int,
        h: int,
        ear: float,
        hover_pos: Tuple[float, float],
        hit_boxes: Dict[str, QRectF],
        is_drag_active: bool = False,
    ):
        hx, hy = hover_pos
        pad_x = ear + 14.0
        inner_w = w - 2 * pad_x
        y_cur = 36.0

        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 1. Main Drop Zone Card
        drop_h = 88.0
        drop_rect = QRectF(pad_x, y_cur, inner_w, drop_h)
        hit_boxes["localsend_drop_zone"] = drop_rect
        drop_hov = drop_rect.contains(QPointF(hx, hy)) or is_drag_active

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(cls.C_CARD_HOVER if drop_hov else cls.C_CARD)
        p.drawRoundedRect(drop_rect, 10.0, 10.0)

        # Dashed border when hovered or dragging
        if drop_hov:
            dash_pen = QPen(cls.C_CYAN, 1.4, Qt.PenStyle.DashLine)
            dash_pen.setDashPattern([5, 4])
            p.setPen(dash_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(drop_rect.adjusted(1.0, 1.0, -1.0, -1.0), 9.0, 9.0)

        # Drop Zone Icon
        cls.draw_localsend_icon(p, drop_rect.center().x(), y_cur + 24.0, size=22.0, color=cls.C_CYAN)

        # Drop Zone Text
        f_title = QFont("Sans", 9, QFont.Weight.Bold)
        f_title.setPixelSize(12)
        p.setFont(f_title)
        p.setPen(cls.C_WHITE)
        title_txt = "Lepaskan Berkas di Sini" if is_drag_active else "Tarik & Lepas File ke Sini"
        p.drawText(
            QRectF(pad_x, y_cur + 40.0, inner_w, 20.0),
            int(Qt.AlignmentFlag.AlignCenter),
            title_txt,
        )

        f_sub = QFont("Sans", 8)
        f_sub.setPixelSize(10)
        p.setFont(f_sub)
        p.setPen(cls.C_MUTED)
        p.drawText(
            QRectF(pad_x, y_cur + 60.0, inner_w, 18.0),
            int(Qt.AlignmentFlag.AlignCenter),
            "Kirim cepat ke HP atau PC via LocalSend",
        )

        # 2. Action Buttons Row (Pilih File... & Buka LocalSend)
        btn_y = y_cur + drop_h + 8.0
        btn_h = 28.0
        gap = 8.0
        half_w = (inner_w - gap) / 2.0

        # Button 1: Pilih File...
        btn_pick = QRectF(pad_x, btn_y, half_w, btn_h)
        hit_boxes["btn_localsend_pick"] = btn_pick
        pick_hov = btn_pick.contains(QPointF(hx, hy))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(cls.C_CARD_HOVER if pick_hov else cls.C_CARD)
        p.drawRoundedRect(btn_pick, 7.0, 7.0)

        f_btn = QFont("Sans", 9, QFont.Weight.Bold)
        f_btn.setPixelSize(11)
        p.setFont(f_btn)
        p.setPen(cls.C_WHITE if pick_hov else cls.C_WHITE_SEC)
        p.drawText(
            btn_pick,
            int(Qt.AlignmentFlag.AlignCenter),
            "📂  Pilih File...",
        )

        # Button 2: Buka LocalSend
        btn_open = QRectF(pad_x + half_w + gap, btn_y, half_w, btn_h)
        hit_boxes["btn_localsend_open"] = btn_open
        open_hov = btn_open.contains(QPointF(hx, hy))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(cls.C_CARD_HOVER if open_hov else cls.C_CARD)
        p.drawRoundedRect(btn_open, 7.0, 7.0)

        p.setFont(f_btn)
        p.setPen(cls.C_CYAN if open_hov else cls.C_WHITE_SEC)
        p.drawText(
            btn_open,
            int(Qt.AlignmentFlag.AlignCenter),
            "🚀  Buka Aplikasi",
        )

        # 3. Footer Status
        foot_y = btn_y + btn_h + 8.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(cls.C_GREEN)
        p.drawEllipse(QPointF(pad_x + 8.0, foot_y + 7.0), 3.0, 3.0)

        f_foot = QFont("Sans", 8)
        f_foot.setPixelSize(10)
        p.setFont(f_foot)
        p.setPen(cls.C_MUTED)
        p.drawText(
            QRectF(pad_x + 18.0, foot_y, inner_w - 20.0, 14.0),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            "LocalSend v1.18 Aktif di Jaringan Lokal (Port 53317)",
        )

        p.restore()

    @classmethod
    def _draw_control_tab(
        cls,
        p: QPainter,
        w: int,
        h: int,
        ear: float,
        system_ctrl: Optional[SystemControlManager],
        hover_pos: Tuple[float, float],
        hit_boxes: Dict[str, QRectF],
    ):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        hx, hy = hover_pos
        pad_x = ear + 3.0
        inner_w = w - 2.0 * pad_x
        st = system_ctrl.state if system_ctrl else SystemControlState()

        f_bold = QFont("Sans", 9, QFont.Weight.Bold)
        f_bold.setPixelSize(11)
        f_sub = QFont("Sans", 8, QFont.Weight.Normal)
        f_sub.setPixelSize(9)
        fm_sub = QFontMetricsF(f_sub)

        # -------------------------------------------------------------
        # 1. Top Row: Wi-Fi Card (Left) & Bluetooth Card (Right)
        # -------------------------------------------------------------
        y_top = 34.0
        card_w = (inner_w - 8.0) / 2.0
        card_h = 46.0

        # Wi-Fi Card
        w_rect = QRectF(pad_x, y_top, card_w, card_h)
        hit_boxes["ctrl_wifi_card"] = w_rect
        w_hov = w_rect.contains(QPointF(hx, hy))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(cls.C_CARD_HOVER if w_hov else QColor(255, 255, 255, 14))
        p.drawRoundedRect(w_rect, 12.0, 12.0)

        w_circle = QRectF(pad_x + 7.0, y_top + 7.0, 32.0, 32.0)
        p.setBrush(QColor(255, 255, 255, 235) if st.wifi_enabled else QColor(255, 255, 255, 20))
        p.drawEllipse(w_circle)
        w_ic_col = QColor(15, 17, 22) if st.wifi_enabled else cls.C_MUTED
        cls.draw_wifi_icon(p, pad_x + 23.0, y_top + 23.0, size=15.0, color=w_ic_col)

        p.setFont(f_bold)
        p.setPen(cls.C_WHITE)
        p.drawText(QRectF(pad_x + 45.0, y_top + 8.0, card_w - 50.0, 16.0), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), "Wi-Fi")
        p.setFont(f_sub)
        p.setPen(cls.C_WHITE_SEC if st.wifi_enabled else cls.C_MUTED)
        w_sub = st.wifi_ssid if st.wifi_enabled else "Mati"
        p.drawText(QRectF(pad_x + 45.0, y_top + 24.0, card_w - 50.0, 14.0), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), fm_sub.elidedText(w_sub, Qt.TextElideMode.ElideRight, card_w - 50.0))

        # Bluetooth Card
        bt_x = pad_x + card_w + 8.0
        bt_rect = QRectF(bt_x, y_top, card_w, card_h)
        hit_boxes["ctrl_bt_card"] = bt_rect
        bt_hov = bt_rect.contains(QPointF(hx, hy))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(cls.C_CARD_HOVER if bt_hov else QColor(255, 255, 255, 14))
        p.drawRoundedRect(bt_rect, 12.0, 12.0)

        bt_circle = QRectF(bt_x + 7.0, y_top + 7.0, 32.0, 32.0)
        p.setBrush(QColor(255, 255, 255, 235) if st.bluetooth_enabled else QColor(255, 255, 255, 20))
        p.drawEllipse(bt_circle)
        bt_ic_col = QColor(15, 17, 22) if st.bluetooth_enabled else cls.C_MUTED
        cls.draw_bluetooth(p, bt_x + 23.0, y_top + 23.0, size=13.0, color=bt_ic_col)

        p.setFont(f_bold)
        p.setPen(cls.C_WHITE)
        p.drawText(QRectF(bt_x + 45.0, y_top + 8.0, card_w - 50.0, 16.0), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), "Bluetooth")
        p.setFont(f_sub)
        p.setPen(cls.C_WHITE_SEC if st.bluetooth_enabled else cls.C_MUTED)
        bt_sub = st.bluetooth_device if (st.bluetooth_enabled and st.bluetooth_device) else ("Aktif" if st.bluetooth_enabled else "Mati")
        p.drawText(QRectF(bt_x + 45.0, y_top + 24.0, card_w - 50.0, 14.0), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), fm_sub.elidedText(bt_sub, Qt.TextElideMode.ElideRight, card_w - 50.0))

        # -------------------------------------------------------------
        # 2. Sliders Card: INLINE (Speaker/Sun + Bar + Percent)
        # -------------------------------------------------------------
        y_slid = 88.0
        slid_h = 60.0
        slid_rect = QRectF(pad_x, y_slid, inner_w, slid_h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 14))
        p.drawRoundedRect(slid_rect, 12.0, 12.0)

        t_x = pad_x + 36.0
        t_w = inner_w - 36.0 - 46.0
        t_h = 4.0
        thumb_r = 5.5

        # Volume Row (Inline)
        y_vr = y_slid + 15.0
        cls.draw_speaker(p, pad_x + 12.0, y_vr, muted=st.volume_muted, color=cls.C_WHITE if not st.volume_muted else cls.C_MUTED)
        hit_boxes["ctrl_speaker_icon"] = QRectF(pad_x + 4.0, y_vr - 12.0, 24.0, 24.0)

        # Volume Track & Hitbox
        y_vt = y_vr - t_h / 2.0
        hit_boxes["ctrl_slider_volume"] = QRectF(t_x, y_vt - 8.0, t_w, t_h + 16.0)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 28))
        p.drawRoundedRect(QRectF(t_x, y_vt, t_w, t_h), t_h / 2.0, t_h / 2.0)

        v_ratio = max(0.0, min(1.0, st.volume / 100.0)) if not st.volume_muted else 0.0
        knob_vx = t_x + thumb_r + (t_w - 2.0 * thumb_r) * v_ratio
        fill_wv = max(t_h, (t_w - 2.0 * thumb_r) * v_ratio + thumb_r)
        if not st.volume_muted and st.volume > 0:
            p.setBrush(QColor(255, 255, 255, 235))
            p.drawRoundedRect(QRectF(t_x, y_vt, fill_wv, t_h), t_h / 2.0, t_h / 2.0)
        p.setBrush(QColor(255, 255, 255, 255))
        p.drawEllipse(QPointF(knob_vx, y_vt + t_h / 2.0), thumb_r, thumb_r)

        vol_txt = "Mute" if st.volume_muted else f"{st.volume}%"
        p.setFont(f_sub)
        p.setPen(cls.C_MUTED)
        p.drawText(QRectF(pad_x + inner_w - 42.0, y_vr - 8.0, 36.0, 16.0), int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), vol_txt)

        # Brightness Row (Inline)
        y_br = y_slid + 45.0
        cls.draw_sun(p, pad_x + 12.0, y_br, color=cls.C_WHITE)

        # Brightness Track & Hitbox
        y_bt = y_br - t_h / 2.0
        hit_boxes["ctrl_slider_brightness"] = QRectF(t_x, y_bt - 8.0, t_w, t_h + 16.0)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 28))
        p.drawRoundedRect(QRectF(t_x, y_bt, t_w, t_h), t_h / 2.0, t_h / 2.0)

        b_ratio = max(0.0, min(1.0, st.brightness / 100.0))
        knob_bx = t_x + thumb_r + (t_w - 2.0 * thumb_r) * b_ratio
        fill_wb = max(t_h, (t_w - 2.0 * thumb_r) * b_ratio + thumb_r)
        if st.brightness > 0:
            p.setBrush(QColor(255, 255, 255, 235))
            p.drawRoundedRect(QRectF(t_x, y_bt, fill_wb, t_h), t_h / 2.0, t_h / 2.0)
        p.setBrush(QColor(255, 255, 255, 255))
        p.drawEllipse(QPointF(knob_bx, y_bt + t_h / 2.0), thumb_r, thumb_r)

        p.setFont(f_sub)
        p.setPen(cls.C_MUTED)
        p.drawText(QRectF(pad_x + inner_w - 42.0, y_br - 8.0, 36.0, 16.0), int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), f"{st.brightness}%")

        # -------------------------------------------------------------
        # 3. Hardware Telemetry Card: CPU, RAM, Suhu
        # -------------------------------------------------------------
        y_stat = 156.0
        stat_h = 46.0
        stat_rect = QRectF(pad_x, y_stat, inner_w, stat_h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 14))
        p.drawRoundedRect(stat_rect, 12.0, 12.0)

        col_w = (inner_w - 16.0) / 3.0
        stats_data = [
            ("CPU", f"{st.cpu_percent}%", st.cpu_percent, cls.draw_chip_icon),
            ("RAM", f"{st.ram_percent}%", st.ram_percent, cls.draw_ram_icon),
            ("Suhu", f"{st.temperature_c}°C" if st.temperature_c > 0 else "--°C", min(100, int(st.temperature_c)), cls.draw_thermometer_icon),
        ]

        f_st_title = QFont("Sans", 8)
        f_st_title.setPixelSize(9)
        f_st_val = QFont("Sans", 9, QFont.Weight.Bold)
        f_st_val.setPixelSize(11)

        for idx, (label, val_str, pct, icon_fn) in enumerate(stats_data):
            cx_col = pad_x + 8.0 + idx * col_w

            # Subtle vertical separator between columns
            if idx > 0:
                p.setPen(QPen(QColor(255, 255, 255, 18), 1.0))
                p.drawLine(QPointF(cx_col - 4.0, y_stat + 8.0), QPointF(cx_col - 4.0, y_stat + stat_h - 8.0))

            # Icon + Label
            icon_fn(p, cx_col + 7.0, y_stat + 14.0, size=12.0, color=cls.C_MUTED)
            p.setFont(f_st_title)
            p.setPen(cls.C_MUTED)
            p.drawText(QRectF(cx_col + 17.0, y_stat + 6.0, col_w - 20.0, 14.0), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), label)

            # Value
            p.setFont(f_st_val)
            p.setPen(cls.C_WHITE)
            p.drawText(QRectF(cx_col + 17.0, y_stat + 18.0, col_w - 20.0, 16.0), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), val_str)

            # Mini progress meter bar
            bar_y = y_stat + 35.0
            bar_w = col_w - 14.0
            bar_h = 3.0
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 25))
            p.drawRoundedRect(QRectF(cx_col + 2.0, bar_y, bar_w, bar_h), 1.5, 1.5)

            fill_w = max(3.0, min(bar_w, bar_w * (max(0, pct) / 100.0)))
            p.setBrush(QColor(255, 255, 255, 220))
            p.drawRoundedRect(QRectF(cx_col + 2.0, bar_y, fill_w, bar_h), 1.5, 1.5)

        # -------------------------------------------------------------
        # 4. Utility Shortcuts Row: Color Picker, Ekstrak Teks, Screenshot
        # -------------------------------------------------------------
        y_util = 210.0
        util_h = 38.0
        u_gap = 8.0
        u_w = (inner_w - 2.0 * u_gap) / 3.0
        f_util = QFont("Sans", 9, QFont.Weight.DemiBold)
        f_util.setPixelSize(10)

        util_buttons = [
            ("ctrl_btn_color_picker", "Ambil Warna", cls.draw_eyedropper_icon, QColor(244, 114, 182)),
            ("ctrl_btn_ocr", "Ekstrak Teks", cls.draw_ocr_icon, QColor(56, 189, 248)),
            ("ctrl_btn_screenshot", "Screenshot", cls.draw_camera_icon, QColor(52, 211, 153)),
        ]
        for idx, (u_key, u_label, u_icon, u_accent) in enumerate(util_buttons):
            ux = pad_x + idx * (u_w + u_gap)
            u_rect = QRectF(ux, y_util, u_w, util_h)
            hit_boxes[u_key] = u_rect
            u_hov = u_rect.contains(QPointF(hx, hy))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(cls.C_CARD_HOVER if u_hov else QColor(255, 255, 255, 14))
            p.drawRoundedRect(u_rect, 11.0, 11.0)

            u_icon(p, ux + 17.0, y_util + util_h / 2.0, size=12.5, color=u_accent if u_hov else cls.C_WHITE)
            p.setFont(f_util)
            p.setPen(cls.C_WHITE if u_hov else cls.C_WHITE_SEC)
            p.drawText(
                QRectF(ux + 28.0, y_util, u_w - 32.0, util_h),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                u_label,
            )

        p.restore()


CSS_RESET = b"""
window, window.background, eventbox, image, drawingarea {
    background-color: rgba(0, 0, 0, 0);
    background-image: none;
    border: none;
    box-shadow: none;
    margin: 0;
    padding: 0;
}

window.settings-window {
    background-color: #0d1117;
    color: #e6edf3;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif;
}

window.settings-window .settings-container {
    padding: 22px;
}

window.settings-window .settings-card {
    background-color: #161b22;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 12px;
    padding: 14px 16px;
    margin-bottom: 12px;
}

window.settings-window label.section-header {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.5px;
    color: #58a6ff;
    margin-bottom: 6px;
}

window.settings-window label.row-title {
    font-size: 13px;
    font-weight: 600;
    color: #f0f6fc;
}

window.settings-window label.row-desc {
    font-size: 11px;
    color: #8b949e;
}

window.settings-window button {
    background-color: #21262d;
    color: #f0f6fc;
    border: 1px solid rgba(240, 246, 252, 0.12);
    border-radius: 8px;
    padding: 6px 12px;
    font-size: 12px;
    font-weight: 600;
}

window.settings-window button:hover {
    background-color: #30363d;
    border-color: #8b949e;
}

window.settings-window button.action-btn {
    background-color: #238636;
    color: #ffffff;
    border: 1px solid rgba(240, 246, 252, 0.12);
}

window.settings-window button.action-btn:hover {
    background-color: #2ea043;
}

window.settings-window button.danger-btn {
    background-color: rgba(248, 81, 73, 0.15);
    color: #f85149;
    border: 1px solid rgba(248, 81, 73, 0.4);
}

window.settings-window button.danger-btn:hover {
    background-color: #da3633;
    color: #ffffff;
}

window.settings-window combobox,
window.settings-window combobox button {
    background-color: #21262d;
    color: #f0f6fc;
    border: 1px solid rgba(240, 246, 252, 0.12);
    border-radius: 8px;
    padding: 4px 8px;
    font-size: 12px;
}

window.settings-window switch {
    border-radius: 14px;
}
"""


class SettingsWindow(Gtk.Window):
    """Modern standalone GTK window for configuring Pulo Notch."""

    def __init__(self, controller):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.controller = controller
        self.set_title("Pengaturan Pulo Notch")
        self.set_role("pulo-settings")
        self.set_default_size(480, 560)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.get_style_context().add_class("settings-window")

        self.connect("delete-event", self._on_delete_event)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.add(scrolled)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        main_box.get_style_context().add_class("settings-container")
        scrolled.add(main_box)

        # Header Title
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        h_lbl = Gtk.Label(xalign=0)
        h_lbl.set_markup("<span size='large' weight='bold' color='#f0f6fc'>Pulo Notch</span>")
        sub_lbl = Gtk.Label(xalign=0)
        sub_lbl.set_markup("<span size='small' color='#8b949e'>Pusat Pengaturan &amp; Personalisasi Dynamic Island</span>")
        title_box.pack_start(h_lbl, False, False, 0)
        title_box.pack_start(sub_lbl, False, False, 0)
        main_box.pack_start(title_box, False, False, 4)

        # ---------------------------------------------------------
        # Card 1: Desain & Tampilan Standby (Idle)
        # ---------------------------------------------------------
        card1 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card1.get_style_context().add_class("settings-card")

        c1_head = Gtk.Label(xalign=0)
        c1_head.get_style_context().add_class("section-header")
        c1_head.set_text("DESAIN & TAMPILAN STANDBY")
        card1.pack_start(c1_head, False, False, 0)

        # Row 0: Island Shape (Notch vs Floating Pill)
        r0_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        r0_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        r0_t = Gtk.Label(label="Bentuk Tampilan Island", xalign=0)
        r0_t.get_style_context().add_class("row-title")
        r0_d = Gtk.Label(label="Pilih bentuk: MacBook Notch (menempel tepi atas) atau Floating Pill (kapsul melayang)", xalign=0)
        r0_d.get_style_context().add_class("row-desc")
        r0_txt.pack_start(r0_t, False, False, 0)
        r0_txt.pack_start(r0_d, False, False, 0)
        r0_box.pack_start(r0_txt, True, True, 0)

        self.combo_shape = Gtk.ComboBoxText()
        self.combo_shape.append("notch", "MacBook Notch")
        self.combo_shape.append("pill", "Floating Pill")
        self.combo_shape.set_active_id(getattr(self.controller, "island_shape", "notch"))
        self.combo_shape.connect("changed", self._on_shape_changed)
        r0_box.pack_start(self.combo_shape, False, False, 0)
        card1.pack_start(r0_box, False, False, 2)

        # Row 1: Standby Animation Mode
        r1_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        r1_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        r1_t = Gtk.Label(label="Mode Tampilan Standby", xalign=0)
        r1_t.get_style_context().add_class("row-title")
        r1_d = Gtk.Label(label="Pilihan tampilan utama: Animasi atau Jam saja", xalign=0)
        r1_d.get_style_context().add_class("row-desc")
        r1_txt.pack_start(r1_t, False, False, 0)
        r1_txt.pack_start(r1_d, False, False, 0)
        r1_box.pack_start(r1_txt, True, True, 0)

        self.combo_standby = Gtk.ComboBoxText()
        self.combo_standby.append("eyes", "Animasi")
        self.combo_standby.append("clock", "Jam Aja")
        if self.controller.standby_display not in ("eyes", "clock"):
            self.controller.standby_display = "clock"
        self.combo_standby.set_active_id(self.controller.standby_display)
        self.combo_standby.connect("changed", self._on_standby_changed)
        r1_box.pack_start(self.combo_standby, False, False, 0)
        card1.pack_start(r1_box, False, False, 2)

        # Row 2: Weather on Standby Switch
        r2_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        r2_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        r2_t = Gtk.Label(label="Cuaca di Sisi Kanan Notch", xalign=0)
        r2_t.get_style_context().add_class("row-title")
        r2_d = Gtk.Label(label="Suhu & ikon cuaca di sayap kanan saat standby", xalign=0)
        r2_d.get_style_context().add_class("row-desc")
        r2_txt.pack_start(r2_t, False, False, 0)
        r2_txt.pack_start(r2_d, False, False, 0)
        r2_box.pack_start(r2_txt, True, True, 0)

        self.switch_weather = Gtk.Switch()
        self.switch_weather.set_valign(Gtk.Align.CENTER)
        self.switch_weather.set_active(self.controller.standby_weather)
        self.switch_weather.connect("notify::active", self._on_weather_switch_toggled)
        r2_box.pack_start(self.switch_weather, False, False, 0)
        card1.pack_start(r2_box, False, False, 2)

        # Row 3: Battery Donut on Standby Switch
        r_bat_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        r_bat_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        r_bat_t = Gtk.Label(label="Persentase Baterai di Standby", xalign=0)
        r_bat_t.get_style_context().add_class("row-title")
        r_bat_d = Gtk.Label(label="Indikator donat baterai dengan angka persen & animasi charger", xalign=0)
        r_bat_d.get_style_context().add_class("row-desc")
        r_bat_txt.pack_start(r_bat_t, False, False, 0)
        r_bat_txt.pack_start(r_bat_d, False, False, 0)
        r_bat_box.pack_start(r_bat_txt, True, True, 0)

        self.switch_battery = Gtk.Switch()
        self.switch_battery.set_valign(Gtk.Align.CENTER)
        self.switch_battery.set_active(self.controller.standby_battery)
        self.switch_battery.connect("notify::active", self._on_battery_switch_toggled)
        r_bat_box.pack_start(self.switch_battery, False, False, 0)
        card1.pack_start(r_bat_box, False, False, 2)

        # Row 4: Live Karaoke Lyrics on Standby Switch
        r_lyr_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        r_lyr_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        r_lyr_t = Gtk.Label(label="Lirik Musik di Standby (Live Karaoke)", xalign=0)
        r_lyr_t.get_style_context().add_class("row-title")
        r_lyr_d = Gtk.Label(label="Tampilkan lirik lagu berjalan di notch saat memutar musik", xalign=0)
        r_lyr_d.get_style_context().add_class("row-desc")
        r_lyr_txt.pack_start(r_lyr_t, False, False, 0)
        r_lyr_txt.pack_start(r_lyr_d, False, False, 0)
        r_lyr_box.pack_start(r_lyr_txt, True, True, 0)

        self.switch_lyrics = Gtk.Switch()
        self.switch_lyrics.set_valign(Gtk.Align.CENTER)
        self.switch_lyrics.set_active(getattr(self.controller, "standby_lyrics", True))
        self.switch_lyrics.connect("notify::active", self._on_lyrics_switch_toggled)
        r_lyr_box.pack_start(self.switch_lyrics, False, False, 0)
        card1.pack_start(r_lyr_box, False, False, 2)

        # Row 5: Finance / IHSG on Standby Switch
        r_fin_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        r_fin_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        r_fin_t = Gtk.Label(label="Info IHSG di Standby", xalign=0)
        r_fin_t.get_style_context().add_class("row-title")
        r_fin_d = Gtk.Label(label="Pergerakan naik/turun IHSG di sisi kanan notch saat santai", xalign=0)
        r_fin_d.get_style_context().add_class("row-desc")
        r_fin_txt.pack_start(r_fin_t, False, False, 0)
        r_fin_txt.pack_start(r_fin_d, False, False, 0)
        r_fin_box.pack_start(r_fin_txt, True, True, 0)

        self.switch_finance = Gtk.Switch()
        self.switch_finance.set_valign(Gtk.Align.CENTER)
        self.switch_finance.set_active(getattr(self.controller, "standby_finance", True))
        self.switch_finance.connect("notify::active", self._on_finance_switch_toggled)
        r_fin_box.pack_start(self.switch_finance, False, False, 0)
        card1.pack_start(r_fin_box, False, False, 2)

        # Row 5b: Camera & Mic Privacy Indicator Switch
        r_priv_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        r_priv_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        r_priv_t = Gtk.Label(label="Indikator Privasi Kamera & Mikrofon", xalign=0)
        r_priv_t.get_style_context().add_class("row-title")
        r_priv_d = Gtk.Label(label="Titik hijau/oranye saat kamera atau mikrofon aktif dipakai aplikasi", xalign=0)
        r_priv_d.get_style_context().add_class("row-desc")
        r_priv_txt.pack_start(r_priv_t, False, False, 0)
        r_priv_txt.pack_start(r_priv_d, False, False, 0)
        r_priv_box.pack_start(r_priv_txt, True, True, 0)

        self.switch_privacy = Gtk.Switch()
        self.switch_privacy.set_valign(Gtk.Align.CENTER)
        self.switch_privacy.set_active(getattr(self.controller, "standby_privacy", True))
        self.switch_privacy.connect("notify::active", self._on_privacy_switch_toggled)
        r_priv_box.pack_start(self.switch_privacy, False, False, 0)
        card1.pack_start(r_priv_box, False, False, 2)

        # Row 5: Monitor Selection
        r3_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        r3_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        r3_t = Gtk.Label(label="Layar Monitor", xalign=0)
        r3_t.get_style_context().add_class("row-title")
        r3_d = Gtk.Label(label="Pilih layar tempat Pulo Notch ditampilkan", xalign=0)
        r3_d.get_style_context().add_class("row-desc")
        r3_txt.pack_start(r3_t, False, False, 0)
        r3_txt.pack_start(r3_d, False, False, 0)
        r3_box.pack_start(r3_txt, True, True, 0)

        self.btn_mon = Gtk.Button(label=self.controller.monitor_mode_label)
        self.btn_mon.set_valign(Gtk.Align.CENTER)
        self.btn_mon.connect("clicked", self._on_monitor_clicked)
        r3_box.pack_start(self.btn_mon, False, False, 0)
        card1.pack_start(r3_box, False, False, 2)

        main_box.pack_start(card1, False, False, 0)

        # ---------------------------------------------------------
        # Card 2: Lokasi & Wilayah (Sholat & Cuaca)
        # ---------------------------------------------------------
        card2 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card2.get_style_context().add_class("settings-card")

        c2_head = Gtk.Label(xalign=0)
        c2_head.get_style_context().add_class("section-header")
        c2_head.set_text("LOKASI & WILAYAH (SHOLAT & CUACA)")
        card2.pack_start(c2_head, False, False, 0)

        r4_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        r4_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        r4_t = Gtk.Label(label="Pilihan Kota / Kabupaten", xalign=0)
        r4_t.get_style_context().add_class("row-title")
        r4_d = Gtk.Label(label="Lokasi jadwal sholat Kemenag & prakiraan Open-Meteo", xalign=0)
        r4_d.get_style_context().add_class("row-desc")
        r4_txt.pack_start(r4_t, False, False, 0)
        r4_txt.pack_start(r4_d, False, False, 0)
        r4_box.pack_start(r4_txt, True, True, 0)

        self.combo_city = Gtk.ComboBoxText()
        if self.controller.prayer:
            for c in self.controller.prayer.CITIES:
                self.combo_city.append(c["name"], c["name"])
            self.combo_city.set_active_id(self.controller.prayer.current_city["name"])
        self.combo_city.connect("changed", self._on_city_changed)
        r4_box.pack_start(self.combo_city, False, False, 0)
        card2.pack_start(r4_box, False, False, 2)

        # Refresh Weather Button row
        r4b_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.lbl_prayer_info = Gtk.Label(xalign=0)
        self.lbl_prayer_info.get_style_context().add_class("row-desc")
        self._update_prayer_summary()
        r4b_box.pack_start(self.lbl_prayer_info, True, True, 0)

        btn_sync = Gtk.Button(label="Segarkan Cuaca")
        btn_sync.set_valign(Gtk.Align.CENTER)
        btn_sync.connect("clicked", self._on_refresh_weather_clicked)
        r4b_box.pack_start(btn_sync, False, False, 0)
        card2.pack_start(r4b_box, False, False, 2)

        # Row 4c: Adzan Vibration Duration
        r_vib_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        r_vib_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        r_vib_t = Gtk.Label(label="Durasi Getar & Banner Adzan", xalign=0)
        r_vib_t.get_style_context().add_class("row-title")
        r_vib_d = Gtk.Label(label="Lama waktu getaran dan banner penuh saat adzan tiba", xalign=0)
        r_vib_d.get_style_context().add_class("row-desc")
        r_vib_txt.pack_start(r_vib_t, False, False, 0)
        r_vib_txt.pack_start(r_vib_d, False, False, 0)
        r_vib_box.pack_start(r_vib_txt, True, True, 0)

        self.combo_adzan_sec = Gtk.ComboBoxText()
        for sec in [5, 10, 15, 20, 30]:
            lbl = f"{sec} detik (Default)" if sec == 10 else f"{sec} detik"
            self.combo_adzan_sec.append(str(sec), lbl)
        self.combo_adzan_sec.set_active_id(str(self.controller.adzan_vibration_sec))
        self.combo_adzan_sec.connect("changed", self._on_adzan_sec_changed)
        r_vib_box.pack_start(self.combo_adzan_sec, False, False, 0)
        card2.pack_start(r_vib_box, False, False, 2)

        main_box.pack_start(card2, False, False, 0)

        # ---------------------------------------------------------
        # Card Health: Kesehatan Kerja & Ergonomi
        # ---------------------------------------------------------
        card_health = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card_health.get_style_context().add_class("settings-card")

        ch_head = Gtk.Label(xalign=0)
        ch_head.get_style_context().add_class("section-header")
        ch_head.set_text("KESEHATAN & ERGONOMI KERJA")
        card_health.pack_start(ch_head, False, False, 0)

        # Row H1: Eye break (20-20-20)
        rh1_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        rh1_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        rh1_t = Gtk.Label(label="Pengingat Istirahat Mata (Aturan 20-20-20)", xalign=0)
        rh1_t.get_style_context().add_class("row-title")
        rh1_d = Gtk.Label(label="Setiap 20 menit, istirahatkan mata 20 detik menatap jarak 6 meter", xalign=0)
        rh1_d.get_style_context().add_class("row-desc")
        rh1_txt.pack_start(rh1_t, False, False, 0)
        rh1_txt.pack_start(rh1_d, False, False, 0)
        rh1_box.pack_start(rh1_txt, True, True, 0)

        self.switch_health_eye = Gtk.Switch()
        self.switch_health_eye.set_valign(Gtk.Align.CENTER)
        self.switch_health_eye.set_active(getattr(self.controller.health, "eye_enabled", True) if hasattr(self.controller, "health") else True)
        self.switch_health_eye.connect("notify::active", self._on_health_eye_toggled)
        rh1_box.pack_start(self.switch_health_eye, False, False, 0)
        card_health.pack_start(rh1_box, False, False, 2)

        # Row H2: Stretch break (50m)
        rh2_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        rh2_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        rh2_t = Gtk.Label(label="Pengingat Berdiri & Peregangan (50 Menit)", xalign=0)
        rh2_t.get_style_context().add_class("row-title")
        rh2_d = Gtk.Label(label="Cegah duduk terlalu lama dengan berdiri atau berjalan ringan", xalign=0)
        rh2_d.get_style_context().add_class("row-desc")
        rh2_txt.pack_start(rh2_t, False, False, 0)
        rh2_txt.pack_start(rh2_d, False, False, 0)
        rh2_box.pack_start(rh2_txt, True, True, 0)

        self.switch_health_stretch = Gtk.Switch()
        self.switch_health_stretch.set_valign(Gtk.Align.CENTER)
        self.switch_health_stretch.set_active(getattr(self.controller.health, "stretch_enabled", True) if hasattr(self.controller, "health") else True)
        self.switch_health_stretch.connect("notify::active", self._on_health_stretch_toggled)
        rh2_box.pack_start(self.switch_health_stretch, False, False, 0)
        card_health.pack_start(rh2_box, False, False, 2)

        # Row H3: Smart Idle Pause
        rh3_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        rh3_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        rh3_t = Gtk.Label(label="Jeda Otomatis Saat Ditinggal (> 5 Menit)", xalign=0)
        rh3_t.get_style_context().add_class("row-title")
        rh3_d = Gtk.Label(label="Timer otomatis di-reset jika Anda sedang beranjak dari meja kerja", xalign=0)
        rh3_d.get_style_context().add_class("row-desc")
        rh3_txt.pack_start(rh3_t, False, False, 0)
        rh3_txt.pack_start(rh3_d, False, False, 0)
        rh3_box.pack_start(rh3_txt, True, True, 0)

        self.switch_health_idle = Gtk.Switch()
        self.switch_health_idle.set_valign(Gtk.Align.CENTER)
        self.switch_health_idle.set_active(getattr(self.controller.health, "smart_idle_pause", True) if hasattr(self.controller, "health") else True)
        self.switch_health_idle.connect("notify::active", self._on_health_idle_toggled)
        rh3_box.pack_start(self.switch_health_idle, False, False, 0)
        card_health.pack_start(rh3_box, False, False, 2)

        main_box.pack_start(card_health, False, False, 0)

        # ---------------------------------------------------------
        # Card 2c: Kutipan & Motivasi (Quotes)
        # ---------------------------------------------------------
        card_quote = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card_quote.get_style_context().add_class("settings-card")

        cq_head = Gtk.Label(xalign=0)
        cq_head.get_style_context().add_class("section-header")
        cq_head.set_text("KUTIPAN & MOTIVASI (QUOTES)")
        card_quote.pack_start(cq_head, False, False, 0)

        # Row Q1: Morning Quote
        rq1_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        rq1_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        rq1_t = Gtk.Label(label="Quote Pagi Hari", xalign=0)
        rq1_t.get_style_context().add_class("row-title")
        rq1_d = Gtk.Label(label="Tampilkan kutipan inspiratif sekali di pagi hari (05:00 - 11:00)", xalign=0)
        rq1_d.get_style_context().add_class("row-desc")
        rq1_txt.pack_start(rq1_t, False, False, 0)
        rq1_txt.pack_start(rq1_d, False, False, 0)
        rq1_box.pack_start(rq1_txt, True, True, 0)

        self.switch_quote_morning = Gtk.Switch()
        self.switch_quote_morning.set_valign(Gtk.Align.CENTER)
        self.switch_quote_morning.set_active(getattr(self.controller.quote_mgr, "morning_enabled", True) if hasattr(self.controller, "quote_mgr") else True)
        self.switch_quote_morning.connect("notify::active", self._on_quote_morning_toggled)
        rq1_box.pack_start(self.switch_quote_morning, False, False, 0)
        card_quote.pack_start(rq1_box, False, False, 2)

        # Row Q2: Eye Break Quote
        rq2_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        rq2_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        rq2_t = Gtk.Label(label="Quote Setiap Istirahat Mata", xalign=0)
        rq2_t.get_style_context().add_class("row-title")
        rq2_d = Gtk.Label(label="Tampilkan kutipan setiap selesai sesi istirahat mata 20-20-20", xalign=0)
        rq2_d.get_style_context().add_class("row-desc")
        rq2_txt.pack_start(rq2_t, False, False, 0)
        rq2_txt.pack_start(rq2_d, False, False, 0)
        rq2_box.pack_start(rq2_txt, True, True, 0)

        self.switch_quote_eye = Gtk.Switch()
        self.switch_quote_eye.set_valign(Gtk.Align.CENTER)
        self.switch_quote_eye.set_active(getattr(self.controller.quote_mgr, "eye_break_enabled", True) if hasattr(self.controller, "quote_mgr") else True)
        self.switch_quote_eye.connect("notify::active", self._on_quote_eye_toggled)
        rq2_box.pack_start(self.switch_quote_eye, False, False, 0)
        card_quote.pack_start(rq2_box, False, False, 2)

        # Row Q3: Category Selector
        rq3_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        rq3_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        rq3_t = Gtk.Label(label="Kategori Quote", xalign=0)
        rq3_t.get_style_context().add_class("row-title")
        rq3_d = Gtk.Label(label="Pilih tema kutipan atau acak dari semua kategori", xalign=0)
        rq3_d.get_style_context().add_class("row-desc")
        rq3_txt.pack_start(rq3_t, False, False, 0)
        rq3_txt.pack_start(rq3_d, False, False, 0)
        rq3_box.pack_start(rq3_txt, True, True, 0)

        self.combo_quote_cat = Gtk.ComboBoxText()
        self.combo_quote_cat.set_valign(Gtk.Align.CENTER)
        for cat_id, cat_label in QUOTE_CATEGORIES:
            self.combo_quote_cat.append(cat_id, cat_label)
        cur_qcat = getattr(self.controller.quote_mgr, "category", "random") if hasattr(self.controller, "quote_mgr") else "random"
        self.combo_quote_cat.set_active_id(cur_qcat)
        self.combo_quote_cat.connect("changed", self._on_quote_cat_changed)
        rq3_box.pack_start(self.combo_quote_cat, False, False, 0)
        card_quote.pack_start(rq3_box, False, False, 2)

        main_box.pack_start(card_quote, False, False, 0)

        # ---------------------------------------------------------
        # Card 3: Integrasi Sistem & Pop-up
        # ---------------------------------------------------------
        card3 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card3.get_style_context().add_class("settings-card")

        c3_head = Gtk.Label(xalign=0)
        c3_head.get_style_context().add_class("section-header")
        c3_head.set_text("INTEGRASI SISTEM")
        card3.pack_start(c3_head, False, False, 0)

        # Row 5: DND Switch
        r5_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        r5_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        r5_t = Gtk.Label(label="Mode Jangan Ganggu (DND)", xalign=0)
        r5_t.get_style_context().add_class("row-title")
        r5_d = Gtk.Label(label="Sembunyikan pop-up banner notifikasi masuk di Notch", xalign=0)
        r5_d.get_style_context().add_class("row-desc")
        r5_txt.pack_start(r5_t, False, False, 0)
        r5_txt.pack_start(r5_d, False, False, 0)
        r5_box.pack_start(r5_txt, True, True, 0)

        self.switch_dnd = Gtk.Switch()
        self.switch_dnd.set_valign(Gtk.Align.CENTER)
        self.switch_dnd.set_active(self.controller.dnd)
        self.switch_dnd.connect("notify::active", self._on_dnd_switch_toggled)
        r5_box.pack_start(self.switch_dnd, False, False, 0)
        card3.pack_start(r5_box, False, False, 2)

        # Row 6: KDE Native Popup Inhibit Switch
        r6_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        r6_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        r6_t = Gtk.Label(label="Sembunyikan Pop-up Notifikasi Asli KDE", xalign=0)
        r6_t.get_style_context().add_class("row-title")
        r6_d = Gtk.Label(label="Cegah pop-up ganda dari notifikasi bawaan KDE Plasma", xalign=0)
        r6_d.get_style_context().add_class("row-desc")
        r6_txt.pack_start(r6_t, False, False, 0)
        r6_txt.pack_start(r6_d, False, False, 0)
        r6_box.pack_start(r6_txt, True, True, 0)

        self.switch_kde = Gtk.Switch()
        self.switch_kde.set_valign(Gtk.Align.CENTER)
        self.switch_kde.set_active(self.controller.kde_top_autohide)
        self.switch_kde.connect("notify::active", self._on_kde_switch_toggled)
        r6_box.pack_start(self.switch_kde, False, False, 0)
        card3.pack_start(r6_box, False, False, 2)

        # Row 7: Hide on Fullscreen Switch
        r7_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        r7_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        r7_t = Gtk.Label(label="Sembunyikan Saat Mode Fullscreen", xalign=0)
        r7_t.get_style_context().add_class("row-title")
        r7_d = Gtk.Label(label="Otomatis sembunyikan notch saat game atau video diputar layar penuh", xalign=0)
        r7_d.get_style_context().add_class("row-desc")
        r7_txt.pack_start(r7_t, False, False, 0)
        r7_txt.pack_start(r7_d, False, False, 0)
        r7_box.pack_start(r7_txt, True, True, 0)

        self.switch_fullscreen_hide = Gtk.Switch()
        self.switch_fullscreen_hide.set_valign(Gtk.Align.CENTER)
        self.switch_fullscreen_hide.set_active(getattr(self.controller, "hide_on_fullscreen", True))
        self.switch_fullscreen_hide.connect("notify::active", self._on_fullscreen_hide_toggled)
        r7_box.pack_start(self.switch_fullscreen_hide, False, False, 0)
        card3.pack_start(r7_box, False, False, 2)

        # Row 8: Autostart Switch
        r8_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        r8_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        r8_t = Gtk.Label(label="Jalankan Otomatis Saat Login (Autostart)", xalign=0)
        r8_t.get_style_context().add_class("row-title")
        r8_d = Gtk.Label(label="Aktifkan agar Pulo Notch otomatis berjalan saat komputer dinyalakan", xalign=0)
        r8_d.get_style_context().add_class("row-desc")
        r8_txt.pack_start(r8_t, False, False, 0)
        r8_txt.pack_start(r8_d, False, False, 0)
        r8_box.pack_start(r8_txt, True, True, 0)

        self.switch_autostart = Gtk.Switch()
        self.switch_autostart.set_valign(Gtk.Align.CENTER)
        self.switch_autostart.set_active(self.controller.is_autostart_enabled())
        self.switch_autostart.connect("notify::active", self._on_autostart_toggled)
        r8_box.pack_start(self.switch_autostart, False, False, 0)
        card3.pack_start(r8_box, False, False, 2)

        main_box.pack_start(card3, False, False, 0)

        # ---------------------------------------------------------
        # Card 4: Uji Coba & Aksi Cepat
        # ---------------------------------------------------------
        card4 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card4.get_style_context().add_class("settings-card")

        c4_head = Gtk.Label(xalign=0)
        c4_head.get_style_context().add_class("section-header")
        c4_head.set_text("UJI COBA & AKSI")
        card4.pack_start(c4_head, False, False, 0)

        r7_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        btn_test_notif = Gtk.Button(label="🔔 Uji Notif")
        btn_test_notif.connect("clicked", lambda b: self.controller.send_test_notification(open_toast=True))
        r7_box.pack_start(btn_test_notif, True, True, 0)

        btn_test_adzan = Gtk.Button(label="🌙 Uji Adzan")
        btn_test_adzan.connect("clicked", lambda b: self.controller.trigger_test_adzan())
        r7_box.pack_start(btn_test_adzan, True, True, 0)

        btn_test_bt = Gtk.Button(label="🎧 Uji Bluetooth")
        btn_test_bt.connect("clicked", lambda b: self.controller.on_bluetooth_event("EDIFIER X2 Plus", True))
        r7_box.pack_start(btn_test_bt, True, True, 0)

        btn_restart = Gtk.Button(label="🔄 Muat Ulang")
        btn_restart.connect("clicked", self._on_restart_clicked)
        r7_box.pack_start(btn_restart, True, True, 0)

        card4.pack_start(r7_box, False, False, 2)

        btn_quit = Gtk.Button(label="Keluar dari Pulo Notch")
        btn_quit.get_style_context().add_class("danger-btn")
        btn_quit.connect("clicked", lambda b: self.controller.quit())
        card4.pack_start(btn_quit, False, False, 2)

        main_box.pack_start(card4, False, False, 0)

        self.show_all()

    def _on_delete_event(self, widget, event):
        self.hide()
        return True

    def _on_shape_changed(self, combo):
        active_id = combo.get_active_id()
        if active_id and hasattr(self.controller, "set_island_shape"):
            self.controller.set_island_shape(active_id)

    def _on_standby_changed(self, combo):
        active_id = combo.get_active_id()
        if active_id:
            self.controller.standby_display = active_id
            self.controller._save_settings()
            self.controller.trigger_animation()
            self.controller.render_all()

    def _on_weather_switch_toggled(self, switch, gparam):
        self.controller.standby_weather = switch.get_active()
        self.controller._save_settings()
        self.controller.trigger_animation()
        self.controller.render_all()

    def _on_battery_switch_toggled(self, switch, gparam):
        self.controller.set_standby_battery(switch.get_active())

    def _on_lyrics_switch_toggled(self, switch, gparam):
        self.controller.set_standby_lyrics(switch.get_active())

    def _on_finance_switch_toggled(self, switch, gparam):
        self.controller.set_standby_finance(switch.get_active())

    def _on_privacy_switch_toggled(self, switch, gparam):
        self.controller.set_standby_privacy(switch.get_active())

    def _on_monitor_clicked(self, btn):
        self.controller.cycle_monitor_mode()
        btn.set_label(self.controller.monitor_mode_label)

    def _on_city_changed(self, combo):
        city_name = combo.get_active_id()
        if city_name and self.controller.prayer:
            self.controller.prayer.set_city_by_name(city_name)
            self._update_prayer_summary()
            self.controller.render_all()

    def _on_adzan_sec_changed(self, combo):
        active_id = combo.get_active_id()
        if active_id:
            sec = int(active_id)
            self.controller.adzan_vibration_sec = sec
            if self.controller.prayer:
                self.controller.prayer.adzan_vibration_sec = sec
            self.controller._save_settings()

    def _on_health_eye_toggled(self, switch, gparam):
        if hasattr(self.controller, "health") and self.controller.health:
            self.controller.health.eye_enabled = switch.get_active()
            self.controller._save_settings()

    def _on_health_stretch_toggled(self, switch, gparam):
        if hasattr(self.controller, "health") and self.controller.health:
            self.controller.health.stretch_enabled = switch.get_active()
            self.controller._save_settings()

    def _on_health_idle_toggled(self, switch, gparam):
        if hasattr(self.controller, "health") and self.controller.health:
            self.controller.health.smart_idle_pause = switch.get_active()
            self.controller._save_settings()

    def _on_quote_morning_toggled(self, switch, gparam):
        if hasattr(self.controller, "quote_mgr") and self.controller.quote_mgr:
            self.controller.quote_mgr.morning_enabled = switch.get_active()
            self.controller._save_settings()

    def _on_quote_eye_toggled(self, switch, gparam):
        if hasattr(self.controller, "quote_mgr") and self.controller.quote_mgr:
            self.controller.quote_mgr.eye_break_enabled = switch.get_active()
            self.controller._save_settings()

    def _on_quote_cat_changed(self, combo):
        cat_id = combo.get_active_id()
        if cat_id and hasattr(self.controller, "quote_mgr") and self.controller.quote_mgr:
            self.controller.quote_mgr.category = cat_id
            self.controller._save_settings()

    def _on_fullscreen_hide_toggled(self, switch, gparam):
        self.controller.set_hide_on_fullscreen(switch.get_active())

    def _on_autostart_toggled(self, switch, gparam):
        self.controller.set_autostart_enabled(switch.get_active())

    def _update_prayer_summary(self):
        if self.controller.prayer:
            nxt_name, nxt_time, _ = self.controller.prayer.get_next_prayer()
            city = self.controller.prayer.current_city["name"]
            self.lbl_prayer_info.set_text(f"{city}: {nxt_name} pukul {nxt_time}")
        else:
            self.lbl_prayer_info.set_text("Jadwal sholat belum aktif")

    def _on_refresh_weather_clicked(self, btn):
        if self.controller.weather:
            self.controller.weather.fetch_online()
        self._update_prayer_summary()

    def _on_dnd_switch_toggled(self, switch, gparam):
        self.controller.dnd = switch.get_active()
        if self.controller.dnd:
            self.controller.dismiss_toast(silent=True)
        self.controller.render_all()

    def _on_kde_switch_toggled(self, switch, gparam):
        self.controller.set_kde_native_notif_hidden(switch.get_active())
        self.controller.render_all()

    def _on_restart_clicked(self, btn):
        subprocess.Popen(["pulo", "--restart"])


class BackdropWindow(Gtk.Window):
    """Full-screen transparent click-catcher shown when Notch panel is open to close it on outside click."""

    def __init__(self, controller, monitor: Optional[Gdk.Monitor]):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.controller = controller
        self.set_title("Pulo Backdrop")
        self.set_decorated(False)
        self.set_resizable(False)

        screen = self.get_screen()
        rgba = screen.get_rgba_visual()
        if rgba is not None:
            self.set_visual(rgba)

        if HAS_LAYER_SHELL:
            GtkLayerShell.init_for_window(self)
            GtkLayerShell.set_namespace(self, "pulo-backdrop")
            GtkLayerShell.set_layer(self, GtkLayerShell.Layer.TOP)
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.BOTTOM, True)
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.LEFT, True)
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.RIGHT, True)
            GtkLayerShell.set_exclusive_zone(self, -1)
            GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)
            if monitor is not None:
                GtkLayerShell.set_monitor(self, monitor)
        else:
            self.set_type_hint(Gdk.WindowTypeHint.UTILITY)

        self.ev_box = Gtk.EventBox()
        self.ev_box.set_visible_window(False)
        self.ev_box.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.ev_box.connect("button-press-event", self.on_button_press)
        self.add(self.ev_box)

    def on_button_press(self, _widget, _event):
        self.controller.close_all_panels()
        return True


class NotchWindow(Gtk.Window):
    """Top-edge Wayland LayerShell window displaying the 28px-tall MacBook Notch."""

    STANDBY_W, STANDBY_H = 116, 28
    STANDBY_NOTIF_W = 166
    STANDBY_MUSIC_W = 152
    STANDBY_MUSIC_NOTIF_W = 198
    STANDBY_PRAYER_W = 186
    STANDBY_PRAYER_NOTIF_W = 226
    STANDBY_PRAYER_MUSIC_W = 216
    STANDBY_PRAYER_ALL_W = 256
    STANDBY_HOVER_W = 138
    STANDBY_HOVER_NOTIF_W = 188
    HUD_W, HUD_H = 250, 28
    TOAST_W, TOAST_H = 376, 62
    PANEL_W, PANEL_H = 396, 276
    PANEL_MUSIC_H = 158

    def __init__(self, controller, monitor: Optional[Gdk.Monitor], monitor_label: str):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.controller = controller
        self.monitor = monitor
        self.monitor_label = monitor_label

        self.set_title("Pulo Notch")
        self.set_role("pulo")
        self.set_decorated(False)
        self.set_resizable(False)

        screen = self.get_screen()
        rgba = screen.get_rgba_visual()
        if rgba is not None:
            self.set_visual(rgba)

        top_margin = 8 if getattr(controller, "island_shape", "notch") == "pill" else 0
        if HAS_LAYER_SHELL:
            GtkLayerShell.init_for_window(self)
            GtkLayerShell.set_namespace(self, "pulo-notch")
            GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
            GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, top_margin)
            GtkLayerShell.set_exclusive_zone(self, -1)
            GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.ON_DEMAND)
            if monitor is not None:
                GtkLayerShell.set_monitor(self, monitor)
        else:
            self.set_type_hint(Gdk.WindowTypeHint.DOCK)
            self.set_keep_above(True)
            self.stick()

        self.is_hovered = False
        self.is_focused = False
        self.panel_open = False
        self.is_drag_target = False
        self.active_tab = "notifications"  # "notifications", "music", "prayer", "weather", or "localsend"
        self.music_view_mode = "player"    # "player" or "lyrics"
        self.show_remaining_music_time = False
        self.prayer_view_mode = "schedule" # "schedule" or "select_city"
        self.hover_pos = (-1.0, -1.0)
        self.hit_boxes: Dict[str, QRectF] = {}
        self.notif_scroll_y = 0.0
        self.target_notif_scroll_y = 0.0
        self.max_notif_scroll_y = 0.0
        self._last_motion_render_time = 0.0
        self.is_dragging_notif = False
        self.drag_start_y = 0.0
        self.drag_start_scroll_y = 0.0
        self.drag_moved = False
        self.is_dragging_volume = False
        self.is_dragging_brightness = False

        self.cur_w = float(self.STANDBY_W)
        self.cur_h = float(self.STANDBY_H)
        self.target_w = float(self.STANDBY_W)
        self.target_h = float(self.STANDBY_H)

        ev_mask = (
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
            | Gdk.EventMask.KEY_PRESS_MASK
            | Gdk.EventMask.FOCUS_CHANGE_MASK
            | Gdk.EventMask.SCROLL_MASK
            | Gdk.EventMask.SMOOTH_SCROLL_MASK
        )
        self.add_events(ev_mask)
        self.connect("button-press-event", self.on_button_press)
        self.connect("button-release-event", self.on_button_release)
        self.connect("motion-notify-event", self.on_motion)
        self.connect("scroll-event", self.on_scroll)
        self.connect("key-press-event", self.on_key_press)

        self.ev_box = Gtk.EventBox()
        self.ev_box.set_visible_window(False)
        self.ev_box.set_can_focus(True)
        self.ev_box.add_events(ev_mask)
        self.ev_box.connect("button-press-event", self.on_button_press)
        self.ev_box.connect("button-release-event", self.on_button_release)
        self.ev_box.connect("motion-notify-event", self.on_motion)
        self.ev_box.connect("enter-notify-event", self.on_enter)
        self.ev_box.connect("leave-notify-event", self.on_leave)
        self.ev_box.connect("scroll-event", self.on_scroll)
        self.ev_box.connect("key-press-event", self.on_key_press)
        self.ev_box.connect("focus-in-event", self.on_focus_change, True)
        self.ev_box.connect("focus-out-event", self.on_focus_change, False)

        self.image_widget = Gtk.Image()
        self.ev_box.add(self.image_widget)
        self.add(self.ev_box)

        # Drag & Drop Support for LocalSend (Receive files dropped onto Notch)
        dnd_targets = [Gtk.TargetEntry.new("text/uri-list", Gtk.TargetFlags.OTHER_APP, 0)]
        self.drag_dest_set(Gtk.DestDefaults.ALL, dnd_targets, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        self.connect("drag-motion", self.on_drag_motion)
        self.connect("drag-leave", self.on_drag_leave)
        self.connect("drag-data-received", self.on_drag_data_received)

        self.ev_box.drag_dest_set(Gtk.DestDefaults.ALL, dnd_targets, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        self.ev_box.connect("drag-motion", self.on_drag_motion)
        self.ev_box.connect("drag-leave", self.on_drag_leave)
        self.ev_box.connect("drag-data-received", self.on_drag_data_received)

        self.connect("realize", self._on_realize)
        self._sync_target_size()
        self.cur_w = self.target_w
        self.cur_h = self.target_h
        self.render_now()

    @property
    def scale_factor(self) -> float:
        if self.monitor:
            try:
                s = self.monitor.get_scale_factor()
                if s > 0:
                    return float(s)
            except Exception:
                pass
        try:
            s = self.get_scale_factor()
            if s > 0:
                return float(s)
        except Exception:
            pass
        return 1.0

    def _set_cursor(self, cursor_type: Gdk.CursorType):
        win = self.ev_box.get_window() or self.get_window()
        if win is not None:
            disp = win.get_display()
            win.set_cursor(Gdk.Cursor.new_for_display(disp, cursor_type))

    def _on_realize(self, *_args):
        self._set_cursor(Gdk.CursorType.HAND2)
        if not HAS_LAYER_SHELL:
            self._reposition_x11()

    def update_island_shape(self):
        top_margin = 8 if getattr(self.controller, "island_shape", "notch") == "pill" else 0
        if HAS_LAYER_SHELL:
            GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, top_margin)
        else:
            self._reposition_x11()

    def _reposition_x11(self):
        disp = Gdk.Display.get_default()
        mon = self.monitor or disp.get_primary_monitor() or disp.get_monitor(0)
        if mon:
            geom = mon.get_geometry()
            w = int(round(self.cur_w))
            x = geom.x + (geom.width - w) // 2
            top_margin = 8 if getattr(self.controller, "island_shape", "notch") == "pill" else 0
            self.move(x, geom.y + top_margin)

    @property
    def current_mode(self) -> str:
        if self.panel_open:
            return "panel"
        if self.controller.active_hud is not None:
            return "hud"
        if self.controller.active_toast is not None:
            return "toast"
        return "standby"

    def _sync_target_size(self):
        mode = self.current_mode
        notif_count = len(self.controller.notifications)
        has_music = self.controller.mpris.is_playing if self.controller.mpris else False
        has_prayer = (
            self.controller.prayer.check_alert() is not None
            if self.controller.prayer
            else False
        )
        has_weather = (
            self.controller.weather is not None
            and self.controller.weather.data is not None
            and self.controller.standby_weather
        )
        has_battery = (
            self.controller.battery is not None
            and self.controller.standby_battery
            and self.controller.battery.present
        )
        has_finance = (
            getattr(self.controller, "finance", None) is not None
            and self.controller.finance.data is not None
            and self.controller.finance.data.ihsg is not None
            and getattr(self.controller, "standby_finance", True)
        )
        priv_obj = getattr(self.controller, "privacy", None)
        has_privacy = (
            priv_obj is not None
            and getattr(self.controller, "standby_privacy", True)
            and (priv_obj.cam_active or priv_obj.mic_active)
        )
        prayer_alert = self.controller.prayer.check_alert() if self.controller.prayer else None
        is_adzan = prayer_alert is not None and prayer_alert.is_adzan

        if mode == "panel":
            self.target_w = float(self.PANEL_W)
            if self.active_tab == "music":
                has_track = bool(self.controller.mpris and (self.controller.mpris.title or self.controller.mpris.is_playing))
                self.target_h = 216.0 if (has_track and self.music_view_mode == "lyrics") else float(self.PANEL_MUSIC_H)
            elif self.active_tab == "notifications":
                if notif_count == 0:
                    self.target_h = 112.0
                elif notif_count == 1:
                    self.target_h = 132.0
                elif notif_count == 2:
                    self.target_h = 194.0
                else:
                    self.target_h = float(self.PANEL_H)
            elif self.active_tab == "localsend":
                self.target_h = 224.0
            elif self.active_tab == "control":
                self.target_h = 264.0
            else:
                self.target_h = float(self.PANEL_H)
        elif mode == "toast":
            self.target_w = float(self.TOAST_W)
            self.target_h = float(self.TOAST_H)
        elif mode == "hud":
            if self.controller.active_hud and self.controller.active_hud.kind == "prayer":
                txt = self.controller.active_hud.extra_text or "Adzan"
                needed = 14.0 + 8.0 + max(20.0, len(txt) * 7.5) + (11.0 + 16.0) * 2.0
                self.target_w = max(280.0, min(float(self.PANEL_W), float(math.ceil(needed))))
            elif self.controller.active_hud and self.controller.active_hud.kind == "bluetooth":
                self.target_w = 278.0
            elif self.controller.active_hud and self.controller.active_hud.kind == "localsend":
                self.target_w = 280.0
            elif self.controller.active_hud and self.controller.active_hud.kind == "eye_break":
                extra = (self.controller.active_hud.extra_text or "").lstrip()
                if extra.startswith("{"):
                    q_w = 360.0
                    q_text, q_author = "", ""
                    try:
                        q_obj = json.loads(extra)
                        q_text = str(q_obj.get("text", "")).strip()
                        q_author = str(q_obj.get("author", "")).strip()
                    except Exception:
                        pass
                    if q_text:
                        f_q = QFont("Sans", 9, QFont.Weight.DemiBold)
                        f_q.setPixelSize(12)
                        fm_q = QFontMetricsF(f_q)
                        inner_w = q_w - 2.0 * (11.0 + 18.0)
                        br = fm_q.boundingRect(
                            QRectF(0.0, 0.0, max(80.0, inner_w), 300.0),
                            int(Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap),
                            f'"{q_text}"',
                        )
                        author_h = 20.0 if q_author else 0.0
                        calc_h = max(94.0, min(185.0, float(br.height()) + author_h + 50.0))
                        self.target_w = q_w
                        self.target_h = float(math.ceil(calc_h))
                        return
                self.target_w = 296.0
            elif self.controller.active_hud and self.controller.active_hud.kind == "stretch_break":
                self.target_w = 308.0
            elif self.controller.active_hud and self.controller.active_hud.kind == "power_plug":
                self.target_w = 276.0
                self.target_h = float(self.HUD_H)
            elif self.controller.active_hud and self.controller.active_hud.kind == "utility_color":
                self.target_w = 276.0
                self.target_h = float(self.HUD_H)
            elif self.controller.active_hud and self.controller.active_hud.kind == "utility_ocr":
                self.target_w = 320.0
                self.target_h = float(self.HUD_H)
            elif self.controller.active_hud and self.controller.active_hud.kind == "utility_msg":
                self.target_w = 290.0
                self.target_h = float(self.HUD_H)
            elif self.controller.active_hud and self.controller.active_hud.kind == "quote":
                q_w = 360.0
                q_text = ""
                q_author = ""
                try:
                    q_obj = json.loads(self.controller.active_hud.extra_text)
                    q_text = str(q_obj.get("text", "")).strip()
                    q_author = str(q_obj.get("author", "")).strip()
                except Exception:
                    q_text = (self.controller.active_hud.extra_text or "").strip()
                f_q = QFont("Sans", 9, QFont.Weight.DemiBold)
                f_q.setPixelSize(12)
                fm_q = QFontMetricsF(f_q)
                inner_w = q_w - 2.0 * (11.0 + 18.0)
                br = fm_q.boundingRect(
                    QRectF(0.0, 0.0, max(80.0, inner_w), 300.0),
                    int(Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap),
                    f'"{q_text}"' if q_text else "",
                )
                author_h = 22.0 if q_author else 0.0
                calc_h = max(76.0, min(170.0, float(br.height()) + author_h + 28.0))
                self.target_w = q_w
                self.target_h = float(math.ceil(calc_h))
                return
            else:
                self.target_w = float(self.HUD_W)
            self.target_h = float(self.HUD_H)
        elif is_adzan:
            # Full Notch Banner when adzan is active (Compact & Centered)
            p_name = prayer_alert.name if prayer_alert else ""
            txt = f"Telah Memasuki Waktu Adzan {p_name}"
            needed = 14.0 + 8.0 + max(20.0, len(txt) * 7.5) + (11.0 + 16.0) * 2.0
            self.target_w = max(280.0, min(float(self.PANEL_W), float(math.ceil(needed))))
            self.target_h = float(self.STANDBY_H)
        elif getattr(self, "is_drag_target", False):
            # Dynamic drop zone when file is dragged over Notch in standby
            self.target_w = 210.0
            self.target_h = float(self.STANDBY_H)
        elif has_music and getattr(self.controller, "standby_lyrics", True):
            cur_lyric_txt = ""
            if self.controller.mpris:
                cur_lyric_txt, _, _ = self.controller.mpris.get_current_lyric()
                if not cur_lyric_txt and self.controller.mpris.title:
                    cur_lyric_txt = self.controller.mpris.title

            if cur_lyric_txt:
                txt_len = len(cur_lyric_txt)
                lyric_w = max(40.0, min(220.0, txt_len * 7.2))
                left_side_w = 49.0
                right_w = 24.0
                item_gap = 10.0
                if has_battery:
                    right_w += 18.0 + item_gap
                if notif_count > 0:
                    cnt_str = f"{notif_count}" if notif_count <= 99 else "99+"
                    right_w += 12.0 + 4.0 + max(10.0, len(cnt_str) * 7.5) + item_gap

                center_w = max(50.0, lyric_w + 14.0)
                needed = left_side_w + center_w + right_w
                self.target_w = max(float(self.STANDBY_MUSIC_W), min(360.0, float(math.ceil(needed))))
                self.target_h = float(self.STANDBY_H)

                if cur_lyric_txt != getattr(self, "_last_lyric_text", ""):
                    self._last_lyric_text = cur_lyric_txt
                    self.controller.trigger_animation()
                return
        else:
            # Standby mode dynamic width calculation
            hovered = self.is_hovered
            is_pill = (getattr(self.controller, "island_shape", "notch") == "pill")
            left_pad = 16.0 if is_pill else 22.0
            right_pad = 16.0 if is_pill else 22.0
            base_min_w = float(self.STANDBY_W - 14 if is_pill else self.STANDBY_W)
            center_gap = 12.0 if is_pill else 13.0
            item_gap = 8.0

            standby_disp = self.controller.standby_display
            wdata = (
                self.controller.weather.data
                if (self.controller.weather and self.controller.standby_weather)
                else None
            )

            # Precise font metrics measurement
            f_clk = QFont("Sans", 9, QFont.Weight.Bold)
            f_clk.setPixelSize(12)
            fm_clk = QFontMetricsF(f_clk)

            f_item = QFont("Sans", 8, QFont.Weight.Bold)
            f_item.setPixelSize(10)
            fm_item = QFontMetricsF(f_item)

            # 1. Left side element width
            if standby_disp == "eyes":
                left_w = 26.0
            else:
                # Digital clock (accurate font advance)
                clock_str = datetime.datetime.now().strftime("%H:%M:%S" if hovered else "%H:%M")
                left_w = fm_clk.horizontalAdvance(clock_str)

            # 2. Right side elements
            right_items_w = []

            # Camera & Mic privacy dots (far right)
            if has_privacy:
                num_dots = (1 if priv_obj.cam_active else 0) + (1 if priv_obj.mic_active else 0)
                dots_w = num_dots * 8.0 + max(0, num_dots - 1) * 1.5
                lbl_w = 0.0
                if hovered:
                    raw_apps = priv_obj.cam_apps or priv_obj.mic_apps
                    if raw_apps:
                        f_priv = QFont("Sans", 8, QFont.Weight.Bold)
                        f_priv.setPixelSize(10)
                        fm_priv = QFontMetricsF(f_priv)
                        lbl_w = 4.0 + fm_priv.horizontalAdvance(raw_apps[0])
                right_items_w.append(dots_w + lbl_w)

            # Battery donut
            if has_battery:
                right_items_w.append(17.0)

            # Active music equalizer
            if has_music:
                right_items_w.append(16.0)

            # Notification badge
            if notif_count > 0:
                cnt_str = f"{notif_count}" if notif_count <= 99 else "99+"
                cnt_w = fm_item.horizontalAdvance(cnt_str)
                right_items_w.append(12.0 + 4.0 + cnt_w)

            # Weather badge on right
            if wdata is not None:
                temp_txt = f"{round(wdata.temperature):.0f}°"
                if hovered:
                    temp_txt = f"{round(wdata.temperature):.0f}° {wdata.condition_text}"
                txt_w = fm_item.horizontalAdvance(temp_txt)
                right_items_w.append(12.0 + 4.0 + txt_w)

            # Finance / IHSG badge on right
            if has_finance:
                ihsg = self.controller.finance.data.ihsg
                sign = "+" if ihsg.change_pct > 0 else ""
                arrow = "▲" if ihsg.is_up else ("▼" if ihsg.change_pct < 0 else "")
                fin_txt = f"IHSG {ihsg.price:,.0f} {arrow}{sign}{ihsg.change_pct:.1f}%" if hovered else f"{arrow}{sign}{ihsg.change_pct:.1f}%"
                txt_w = fm_item.horizontalAdvance(fin_txt)
                right_items_w.append(11.0 + 3.5 + txt_w)

            # Prayer alert
            if prayer_alert is not None:
                p_name = prayer_alert.name
                p_time = prayer_alert.time_str
                is_adz = prayer_alert.is_adzan
                p_txt = f"Adzan {p_name}" if is_adz else f"{p_name} {p_time}"
                txt_w = fm_item.horizontalAdvance(p_txt)
                right_items_w.append(12.0 + 4.0 + txt_w)

            if not right_items_w:
                self.target_w = max(base_min_w, left_pad + left_w + right_pad + 8.0)
            else:
                total_right_w = sum(right_items_w) + (len(right_items_w) - 1) * item_gap
                needed = left_pad + left_w + center_gap + total_right_w + right_pad
                self.target_w = max(base_min_w, float(math.ceil(needed)))

            self.target_h = float(self.STANDBY_H)

    def set_panel_open(self, open_state: bool, tab: str = "notifications"):
        self.panel_open = open_state
        if open_state:
            self.active_tab = tab
            self.music_view_mode = "player"
            self.controller.dismiss_toast(silent=True)
            self.controller.show_backdrops()
            self.grab_focus()
        else:
            self.music_view_mode = "player"
            self.notif_scroll_y = 0.0
            self.target_notif_scroll_y = 0.0
            self.controller.hide_backdrops()
            trim_memory()
        self.controller.trigger_animation()

    def step_animation(self) -> bool:
        self._sync_target_size()
        dw = self.target_w - self.cur_w
        dh = self.target_h - self.cur_h
        size_moving = False
        if abs(dw) < 0.8 and abs(dh) < 0.8:
            if self.cur_w != self.target_w or self.cur_h != self.target_h:
                self.cur_w = self.target_w
                self.cur_h = self.target_h
                size_moving = True
        else:
            self.cur_w += dw * 0.34
            self.cur_h += dh * 0.34
            size_moving = True

        # Smooth 60fps scrolling interpolation for notifications
        scroll_moving = False
        if self.panel_open and self.active_tab == "notifications":
            target_s = getattr(self, "target_notif_scroll_y", 0.0)
            dscroll = target_s - self.notif_scroll_y
            if abs(dscroll) > 0.4:
                self.notif_scroll_y += dscroll * 0.48
                scroll_moving = True
            elif self.notif_scroll_y != target_s:
                self.notif_scroll_y = target_s
                scroll_moving = True

        if size_moving or scroll_moving:
            self.render_now()
            return True
        return False

    def render_now(self):
        if self.current_mode == "standby":
            self._sync_target_size()
            if abs(self.target_w - self.cur_w) > 0.8:
                self.controller.trigger_animation()

        w = max(60, int(round(self.cur_w)))
        h = max(24, int(round(self.cur_h)))
        scale = max(1, int(round(self.scale_factor)))

        # Compute cursor gaze offset (-2.4 to 2.4, -0.8 to 2.2)
        cur_gx, cur_gy = 0.0, 0.0
        if self.current_mode == "standby" and self.controller.standby_display == "eyes":
            mx, my = getattr(self.controller, "last_cursor_pos", (None, None))
            if mx is not None and my is not None:
                geom = self.monitor.get_geometry() if self.monitor else None
                screen_w = float(geom.width) if geom else 1920.0
                screen_h = float(geom.height) if geom else 1080.0
                notch_x = (float(geom.x) if geom else 0.0) + screen_w / 2.0
                notch_y = (float(geom.y) if geom else 0.0) + 14.0

                dx = float(mx) - notch_x
                dy = float(my) - notch_y
                cur_gx = max(-2.4, min(2.4, (dx / (screen_w / 2.0)) * 2.4))
                cur_gy = max(-0.8, min(2.2, (dy / screen_h) * 2.2))

        qimg = NotchRenderer.render_frame(
            w=w,
            h=h,
            mode=self.current_mode,
            active_tab=self.active_tab,
            bat=self.controller.battery,
            toast=self.controller.active_toast,
            toast_ratio=self.controller.toast_remaining_ratio,
            notifications=self.controller.notifications,
            hovered=self.is_hovered,
            focused=self.is_focused,
            dnd=self.controller.dnd,
            kde_autohide=self.controller.kde_top_autohide,
            monitor_label=self.controller.monitor_mode_label,
            hover_pos=self.hover_pos,
            hit_boxes=self.hit_boxes,
            hud=self.controller.active_hud,
            mpris=self.controller.mpris,
            music_view_mode=self.music_view_mode,
            prayer=self.controller.prayer,
            prayer_view_mode=self.prayer_view_mode,
            standby_display=self.controller.standby_display,
            notif_scroll_y=self.notif_scroll_y,
            scale=float(scale),
            weather=self.controller.weather,
            standby_weather=self.controller.standby_weather,
            idle_sec=self.controller.get_idle_seconds(),
            battery=self.controller.battery,
            standby_battery=self.controller.standby_battery,
            is_drag_target=getattr(self, "is_drag_target", False),
            show_remaining_music_time=self.show_remaining_music_time,
            standby_lyrics=getattr(self.controller, "standby_lyrics", True),
            is_typing=self.controller.is_currently_typing(),
            is_angry=self.controller.is_currently_angry(),
            is_notif=self.controller.is_notification_active(),
            cursor_gaze=(cur_gx, cur_gy),
            system_ctrl=getattr(self.controller, "system_ctrl", None),
            island_shape=getattr(self.controller, "island_shape", "notch"),
            finance=getattr(self.controller, "finance", None),
            standby_finance=getattr(self.controller, "standby_finance", True),
            privacy=getattr(self.controller, "privacy", None),
            standby_privacy=getattr(self.controller, "standby_privacy", True),
            audio_spectrum=self.controller.spectrum.bands if hasattr(self.controller, "spectrum") and self.controller.spectrum else None,
        )

        ptr = qimg.bits()
        nbytes = qimg.sizeInBytes()
        phys_w = qimg.width()
        phys_h = qimg.height()
        stride = qimg.bytesPerLine()

        bufs = getattr(self, "_cairo_bufs", None)
        if bufs is None:
            bufs = [None, None]
            self._cairo_bufs = bufs
            self._cairo_buf_idx = 0
        idx = 1 - self._cairo_buf_idx
        self._cairo_buf_idx = idx
        c_buf = bufs[idx]
        if c_buf is None or len(c_buf) < nbytes:
            c_buf = (ctypes.c_ubyte * nbytes)()
            bufs[idx] = c_buf
        ctypes.memmove(c_buf, int(ptr), nbytes)

        surf = _libcairo.cairo_image_surface_create_for_data(c_buf, 0, phys_w, phys_h, stride)
        if surf:
            if scale > 1:
                _libcairo.cairo_surface_set_device_scale(surf, float(scale), float(scale))
            _libgtk.gtk_image_set_from_surface(hash(self.image_widget), surf)
            _libcairo.cairo_surface_destroy(surf)

        del ptr, qimg

        if getattr(self, "_last_win_size", None) != (w, h):
            self._last_win_size = (w, h)
            self.set_size_request(w, h)
            self.resize(w, h)
            if not HAS_LAYER_SHELL:
                self._reposition_x11()

    def on_scroll(self, _widget, event: Gdk.EventScroll):
        if self.panel_open and self.active_tab == "notifications":
            dy = 0.0
            if event.direction == Gdk.ScrollDirection.UP:
                dy = -60.0
            elif event.direction == Gdk.ScrollDirection.DOWN:
                dy = 60.0
            elif event.direction == Gdk.ScrollDirection.SMOOTH:
                has_delta, _dx, delta_y = event.get_scroll_deltas()
                if has_delta and delta_y != 0.0:
                    dy = delta_y * 48.0
                elif hasattr(event, "delta_y") and event.delta_y != 0.0:
                    dy = event.delta_y * 48.0

            if dy != 0.0:
                card_h = 56.0
                gap = 6.0
                n = len(self.controller.notifications)
                total_content_h = n * card_h + max(0, n - 1) * gap
                visible_list_h = max(20.0, self.cur_h - 80.0)
                max_scroll = max(0.0, total_content_h - visible_list_h)
                self.max_notif_scroll_y = max_scroll
                cur_target = getattr(self, "target_notif_scroll_y", self.notif_scroll_y)
                self.target_notif_scroll_y = max(0.0, min(max_scroll, cur_target + dy))
                self.controller.trigger_animation()
                return True
        return False

    # =========================================================================
    # Mouse & Keyboard Interaction
    # =========================================================================
    def on_enter(self, _widget, event):
        self.controller.register_activity()
        self.is_hovered = True
        self.hover_pos = (event.x, event.y)
        self.controller.set_toast_paused(True)
        self._set_cursor(Gdk.CursorType.HAND2)
        self.controller.trigger_animation()
        self.render_now()
        return False

    def on_leave(self, _widget, _event):
        self.is_hovered = False
        self.hover_pos = (-1.0, -1.0)
        self.controller.set_toast_paused(False)
        self.controller.trigger_animation()
        self.render_now()
        return False

    def on_drag_motion(self, widget, context, x, y, time):
        if not self.is_drag_target:
            self.is_drag_target = True
            self.controller.trigger_animation()
            self.render_now()
        Gdk.drag_status(context, Gdk.DragAction.COPY, time)
        return True

    def on_drag_leave(self, widget, context, time):
        if self.is_drag_target:
            self.is_drag_target = False
            self.controller.trigger_animation()
            self.render_now()
        return True

    def on_drag_data_received(self, widget, context, x, y, selection_data, info, time):
        self.is_drag_target = False
        uris = selection_data.get_uris()
        paths = []
        if uris:
            for uri in uris:
                try:
                    fpath, _ = GLib.filename_from_uri(uri)
                    if os.path.exists(fpath):
                        paths.append(fpath)
                except Exception:
                    pass
        if not paths and selection_data.get_text():
            for line in selection_data.get_text().strip().splitlines():
                line = line.strip()
                if line.startswith("file://"):
                    try:
                        fpath, _ = GLib.filename_from_uri(line)
                        if os.path.exists(fpath):
                            paths.append(fpath)
                    except Exception:
                        pass
                elif os.path.exists(line):
                    paths.append(line)

        success = False
        if paths:
            success = True
            self.controller.send_files_via_localsend(paths)

        context.finish(success, False, time)
        self.controller.trigger_animation()
        self.render_now()
        return True

    def _pick_files_and_send(self):
        try:
            native = Gtk.FileChooserNative.new(
                "Pilih File untuk Dikirim via LocalSend",
                self,
                Gtk.FileChooserAction.OPEN,
                "Kirim",
                "Batal",
            )
            native.set_select_multiple(True)
            res = native.run()
            if res == Gtk.ResponseType.ACCEPT:
                filenames = native.get_filenames()
                if filenames:
                    self.controller.send_files_via_localsend(filenames)
            native.destroy()
        except Exception:
            pass

    def on_motion(self, _widget, event):
        self.controller.register_activity()
        self.hover_pos = (event.x, event.y)
        self._set_cursor(Gdk.CursorType.HAND2)

        # Drag-to-scroll support on notification tab
        if self.is_dragging_notif and self.panel_open and self.active_tab == "notifications":
            diff_y = event.y - self.drag_start_y
            if abs(diff_y) > 2.0:
                self.drag_moved = True
                card_h = 56.0
                gap = 6.0
                n = len(self.controller.notifications)
                total_content_h = n * card_h + max(0, n - 1) * gap
                visible_list_h = max(20.0, self.cur_h - 80.0)
                max_scroll = max(0.0, total_content_h - visible_list_h)
                self.max_notif_scroll_y = max_scroll
                new_s = max(0.0, min(max_scroll, self.drag_start_scroll_y - diff_y))
                self.notif_scroll_y = new_s
                self.target_notif_scroll_y = new_s
                self.render_now()
                return True

        # Slider dragging support on control tab
        if getattr(self, "is_dragging_volume", False) and self.panel_open and self.active_tab == "control":
            rect = self.hit_boxes.get("ctrl_slider_volume")
            if rect and hasattr(self.controller, "system_ctrl") and self.controller.system_ctrl:
                ratio = max(0.0, min(1.0, (event.x - rect.left()) / max(1.0, rect.width())))
                self.controller.system_ctrl.set_volume(int(round(ratio * 100)))
                self.render_now()
                return True

        if getattr(self, "is_dragging_brightness", False) and self.panel_open and self.active_tab == "control":
            rect = self.hit_boxes.get("ctrl_slider_brightness")
            if rect and hasattr(self.controller, "system_ctrl") and self.controller.system_ctrl:
                ratio = max(0.0, min(1.0, (event.x - rect.left()) / max(1.0, rect.width())))
                self.controller.system_ctrl.set_brightness(int(round(ratio * 100)))
                self.render_now()
                return True

        # Throttle hover re-render & skip if 60fps animation loop is already rendering
        if self.controller._anim_source_id is None:
            now = time.time()
            if (now - getattr(self, "_last_motion_render_time", 0.0)) >= 0.025:
                self._last_motion_render_time = now
                if self.panel_open or self.controller.active_toast is not None:
                    self.render_now()
        return False

    def on_button_release(self, _widget, event: Gdk.EventButton):
        if getattr(self, "is_dragging_volume", False) or getattr(self, "is_dragging_brightness", False):
            self.is_dragging_volume = False
            self.is_dragging_brightness = False
            return True

        if self.is_dragging_notif:
            self.is_dragging_notif = False
            if self.drag_moved:
                self.drag_moved = False
                return True
            self.drag_moved = False
            if self.panel_open and self.active_tab == "notifications":
                pt = QPointF(event.x, event.y)
                for key, rect in list(self.hit_boxes.items()):
                    if key.startswith("notif_card_") and rect.contains(pt):
                        nid_str = key[len("notif_card_") :]
                        del_rect = self.hit_boxes.get(f"del_{nid_str}")
                        if del_rect and del_rect.contains(pt):
                            break
                        try:
                            nid = int(nid_str)
                        except ValueError:
                            break
                        target_item = next((n for n in self.controller.notifications if n.id == nid), None)
                        if target_item is not None:
                            self.controller.open_notification_app(target_item)
                            return True
                        break
        return False

    def on_focus_change(self, _widget, _event, focused: bool):
        self.is_focused = focused
        if not focused and self.panel_open:
            self.set_panel_open(False)
        self.render_now()
        return False

    def on_key_press(self, _widget, event: Gdk.EventKey):
        self.controller.register_activity()
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            self.set_panel_open(not self.panel_open)
            return True
        if event.keyval == Gdk.KEY_Tab and self.panel_open:
            order = ["notifications", "music", "prayer", "weather"]
            try:
                idx = order.index(self.active_tab)
                self.active_tab = order[(idx + 1) % len(order)]
            except ValueError:
                self.active_tab = "notifications"
            self.prayer_view_mode = "schedule"
            self.controller.trigger_animation()
            self.render_now()
            return True
        if event.keyval == Gdk.KEY_Escape:
            if self.panel_open:
                self.set_panel_open(False)
                return True
            if self.controller.active_toast is not None:
                self.controller.dismiss_toast()
                return True
        if self.panel_open and self.active_tab == "notifications":
            if event.keyval in (Gdk.KEY_Up, Gdk.KEY_Page_Up):
                dy = -62.0 if event.keyval == Gdk.KEY_Up else -150.0
                card_h = 56.0
                gap = 6.0
                n = len(self.controller.notifications)
                total_content_h = n * card_h + max(0, n - 1) * gap
                visible_list_h = max(20.0, self.cur_h - 80.0)
                max_scroll = max(0.0, total_content_h - visible_list_h)
                self.max_notif_scroll_y = max_scroll
                cur_target = getattr(self, "target_notif_scroll_y", self.notif_scroll_y)
                self.target_notif_scroll_y = max(0.0, min(max_scroll, cur_target + dy))
                self.controller.trigger_animation()
                return True
            elif event.keyval in (Gdk.KEY_Down, Gdk.KEY_Page_Down):
                dy = 62.0 if event.keyval == Gdk.KEY_Down else 150.0
                card_h = 56.0
                gap = 6.0
                n = len(self.controller.notifications)
                total_content_h = n * card_h + max(0, n - 1) * gap
                visible_list_h = max(20.0, self.cur_h - 80.0)
                max_scroll = max(0.0, total_content_h - visible_list_h)
                self.max_notif_scroll_y = max_scroll
                cur_target = getattr(self, "target_notif_scroll_y", self.notif_scroll_y)
                self.target_notif_scroll_y = max(0.0, min(max_scroll, cur_target + dy))
                self.controller.trigger_animation()
                return True
        return False

    def on_button_press(self, _widget, event: Gdk.EventButton):
        self.controller.register_activity()
        # Right click opens dedicated Settings window
        if event.button == 3:
            self.controller.open_settings_window()
            return True

        if event.button != 1:
            return False

        pt = QPointF(event.x, event.y)
        mode = self.current_mode

        if mode == "hud":
            if self.controller.active_hud and self.controller.active_hud.kind in ("eye_break", "stretch_break", "quote"):
                self.controller.dismiss_hud()
                return True

        if mode == "standby":
            if "standby_eyes" in self.hit_boxes and self.hit_boxes["standby_eyes"].contains(pt):
                now = time.time()
                last_poke = getattr(self.controller, "_last_eye_poke", 0.0)
                poke_count = getattr(self.controller, "_eye_poke_count", 0)
                if now - last_poke < 1.8:
                    poke_count += 1
                else:
                    poke_count = 1
                self.controller._last_eye_poke = now
                self.controller._eye_poke_count = poke_count

                if poke_count >= 2:
                    self.controller.trigger_angry(duration=4.5)
                else:
                    self.controller.register_activity()
                self.render_now()
                return True
            if "standby_weather" in self.hit_boxes and self.hit_boxes["standby_weather"].contains(pt):
                self.set_panel_open(True, tab="weather")
                return True
            if "standby_finance" in self.hit_boxes and self.hit_boxes["standby_finance"].contains(pt):
                self.set_panel_open(True, tab="finance")
                return True
            if "standby_battery" in self.hit_boxes and self.hit_boxes["standby_battery"].contains(pt):
                bat = self.controller.battery
                self.controller.on_hud_event(
                    "battery",
                    bat.capacity,
                    False,
                    extra_text=bat.detail_label,
                )
                return True
            default_tab = "music" if (self.controller.mpris and self.controller.mpris.is_playing) else "notifications"
            self.set_panel_open(True, tab=default_tab)
            return True

        if mode == "toast":
            if "toast_close" in self.hit_boxes and self.hit_boxes["toast_close"].contains(pt):
                self.controller.dismiss_toast()
                return True
            default_tab = "music" if (self.controller.mpris and self.controller.mpris.is_playing) else "notifications"
            self.set_panel_open(True, tab=default_tab)
            return True

        if mode == "panel":
            # Enable dragging on notification area
            if self.active_tab == "notifications" and 34.0 <= event.y <= self.cur_h - 28.0:
                self.is_dragging_notif = True
                self.drag_start_y = event.y
                self.drag_start_scroll_y = self.notif_scroll_y
                self.drag_moved = False

            for key, rect in list(self.hit_boxes.items()):
                if key.startswith("notif_card_"):
                    continue
                if rect.contains(pt):
                    if key == "tab_notifications":
                        self.active_tab = "notifications"
                        self.controller.trigger_animation()
                        self.render_now()
                    elif key == "tab_music":
                        self.active_tab = "music"
                        self.music_view_mode = "player"
                        self.controller.trigger_animation()
                        self.render_now()
                    elif key == "tab_prayer":
                        self.active_tab = "prayer"
                        self.prayer_view_mode = "schedule"
                        self.controller.trigger_animation()
                        self.render_now()
                    elif key == "tab_weather":
                        self.active_tab = "weather"
                        self.controller.trigger_animation()
                        self.render_now()
                    elif key == "tab_finance":
                        self.active_tab = "finance"
                        self.controller.trigger_animation()
                        self.render_now()
                    elif key == "btn_refresh_finance":
                        if hasattr(self.controller, "finance") and self.controller.finance:
                            self.controller.finance.fetch_online()
                        self.render_now()
                    elif key == "tab_localsend":
                        self.active_tab = "localsend"
                        self.controller.trigger_animation()
                        self.render_now()
                    elif key == "tab_control":
                        self.active_tab = "control"
                        if hasattr(self.controller, "system_ctrl") and self.controller.system_ctrl:
                            self.controller.system_ctrl.refresh_async()
                        self.controller.trigger_animation()
                        self.render_now()
                    elif key in ("ctrl_wifi_card", "ctrl_wifi_toggle"):
                        if hasattr(self.controller, "system_ctrl") and self.controller.system_ctrl:
                            self.controller.system_ctrl.toggle_wifi()
                            self.render_now()
                    elif key in ("ctrl_bt_card", "ctrl_bt_toggle"):
                        if hasattr(self.controller, "system_ctrl") and self.controller.system_ctrl:
                            self.controller.system_ctrl.toggle_bluetooth()
                            self.render_now()
                    elif key == "ctrl_btn_dnd":
                        self.controller.dnd = not self.controller.dnd
                        if hasattr(self.controller, "system_ctrl") and self.controller.system_ctrl:
                            self.controller.system_ctrl.state.dnd_enabled = self.controller.dnd
                        self.render_now()
                    elif key == "ctrl_btn_mic":
                        if hasattr(self.controller, "system_ctrl") and self.controller.system_ctrl:
                            self.controller.system_ctrl.toggle_mic_mute()
                            self.render_now()
                    elif key == "ctrl_btn_screenshot":
                        self.set_panel_open(False)
                        if hasattr(self.controller, "system_ctrl") and self.controller.system_ctrl:
                            GLib.timeout_add(250, lambda: (self.controller.system_ctrl.take_screenshot(), False)[1])
                    elif key == "ctrl_btn_color_picker":
                        self.set_panel_open(False)
                        if hasattr(self.controller, "system_ctrl") and self.controller.system_ctrl:
                            GLib.timeout_add(250, lambda: (self.controller.system_ctrl.pick_color(), False)[1])
                    elif key == "ctrl_btn_ocr":
                        self.set_panel_open(False)
                        if hasattr(self.controller, "system_ctrl") and self.controller.system_ctrl:
                            GLib.timeout_add(250, lambda: (self.controller.system_ctrl.extract_text_ocr(), False)[1])
                    elif key == "ctrl_btn_lock":
                        self.set_panel_open(False)
                        if hasattr(self.controller, "system_ctrl") and self.controller.system_ctrl:
                            self.controller.system_ctrl.lock_screen()
                    elif key == "ctrl_speaker_icon":
                        if hasattr(self.controller, "system_ctrl") and self.controller.system_ctrl:
                            self.controller.system_ctrl.toggle_volume_mute()
                            self.render_now()
                    elif key == "ctrl_slider_volume":
                        rect = self.hit_boxes.get("ctrl_slider_volume")
                        if rect and hasattr(self.controller, "system_ctrl") and self.controller.system_ctrl:
                            self.is_dragging_volume = True
                            ratio = max(0.0, min(1.0, (pt.x() - rect.left()) / max(1.0, rect.width())))
                            self.controller.system_ctrl.set_volume(int(round(ratio * 100)))
                            self.render_now()
                    elif key == "ctrl_slider_brightness":
                        rect = self.hit_boxes.get("ctrl_slider_brightness")
                        if rect and hasattr(self.controller, "system_ctrl") and self.controller.system_ctrl:
                            self.is_dragging_brightness = True
                            ratio = max(0.0, min(1.0, (pt.x() - rect.left()) / max(1.0, rect.width())))
                            self.controller.system_ctrl.set_brightness(int(round(ratio * 100)))
                            self.render_now()
                    elif key in ("localsend_drop_zone", "btn_localsend_pick"):
                        self._pick_files_and_send()
                    elif key == "btn_localsend_open":
                        self.controller.open_localsend_app()
                    elif key == "btn_refresh_weather":
                        if self.controller.weather:
                            self.controller.weather.fetch_online()
                        self.render_now()
                    elif key in ("tab_settings", "btn_settings"):
                        self.controller.open_settings_window()
                    elif key == "btn_select_city":
                        self.prayer_view_mode = "select_city"
                        self.controller.trigger_animation()
                        self.render_now()
                    elif key == "btn_back_prayer":
                        self.prayer_view_mode = "schedule"
                        self.controller.trigger_animation()
                        self.render_now()
                    elif key.startswith("sel_city_"):
                        cname = key[len("sel_city_") :]
                        if self.controller.prayer:
                            self.controller.prayer.set_city_by_name(cname)
                        self.prayer_view_mode = "schedule"
                        self.controller.trigger_animation()
                        self.render_now()
                    elif key == "music_timeline":
                        if self.controller.mpris:
                            rect = self.hit_boxes.get("music_timeline")
                            if rect:
                                rel_x = max(0.0, min(rect.width(), pt.x() - rect.left()))
                                ratio = rel_x / max(1.0, rect.width())
                                self.controller.mpris.seek_to_ratio(ratio)
                                self.render_now()
                    elif key == "music_time_toggle":
                        self.show_remaining_music_time = not getattr(self, "show_remaining_music_time", False)
                        self.render_now()
                    elif key == "music_prev":
                        if self.controller.mpris:
                            self.controller.mpris.previous()
                            self.render_now()
                    elif key == "music_play_pause":
                        if self.controller.mpris:
                            self.controller.mpris.play_pause()
                            self.render_now()
                    elif key == "music_next":
                        if self.controller.mpris:
                            self.controller.mpris.next()
                            self.render_now()
                    elif key == "music_lyrics_toggle":
                        self.music_view_mode = "player" if self.music_view_mode == "lyrics" else "lyrics"
                        self.controller.trigger_animation()
                        self.render_now()
                    elif key == "header_battery":
                        bat = self.controller.battery
                        volt = f" ({bat.voltage_v:.1f}V)" if bat.voltage_v > 0 else ""
                        self.controller.on_hud_event(
                            "bluetooth" if False else "volume",
                            bat.capacity,
                            False,
                        )
                    elif key == "btn_test":
                        self.controller.send_test_notification(open_toast=True)
                    elif key == "btn_standby_type":
                        self.controller.cycle_standby_display()
                        self.render_now()
                    elif key == "btn_standby_weather":
                        self.controller.toggle_standby_weather()
                        self.render_now()
                    elif key == "btn_dnd":
                        self.controller.toggle_dnd()
                    elif key == "btn_prayer_city":
                        self.active_tab = "prayer"
                        self.prayer_view_mode = "select_city"
                        self.controller.trigger_animation()
                        self.render_now()
                    elif key == "btn_mon":
                        self.controller.cycle_monitor_mode()
                    elif key == "btn_kde_panel":
                        self.controller.toggle_kde_top_panel()
                    elif key == "btn_bt":
                        # Test or toggle bluetooth notification
                        self.controller.on_bluetooth_event("EDIFIER X2 Plus", True)
                    elif key == "btn_clear":
                        self.controller.clear_notifications()
                    elif key == "btn_quit":
                        self.controller.quit()
                    elif key.startswith("del_"):
                        nid = int(key.split("_", 1)[1])
                        self.controller.remove_notification(nid)
                    self.is_dragging_notif = False
                    return True
            # Clicking the top 32px header bar closes the panel back to standby
            if event.y <= 32.0:
                self.set_panel_open(False)
                return True

        return False


class DynamicIslandController:
    """Coordinates battery polling, D-Bus notifications, KDE popup inhibition, and Notch windows."""

    TOAST_DURATION = 5.0

    def __init__(self):
        self._qapp = QGuiApplication.instance() or QGuiApplication(sys.argv)
        self.battery: BatteryState = PowerReader.read()
        self._prev_ac_online = self.battery.ac_online
        self._prev_is_full = self.battery.is_full

        self.notifications: List[NotificationItem] = []
        self._next_id = 1
        self.active_toast: Optional[NotificationItem] = None
        self.active_hud: Optional[HudItem] = None
        self.toast_started_at = 0.0
        self.toast_paused = False
        self.toast_elapsed_before_pause = 0.0
        self.dnd = False
        self.kde_top_autohide = True  # Represents "Hide native KDE notification popups"
        self._session_bus: Optional[Gio.DBusConnection] = None
        self._inhibit_cookie: Optional[int] = None

        self.monitor_mode = 0
        self.windows: List[NotchWindow] = []
        self.backdrops: List[BackdropWindow] = []
        self.settings_window: Optional[SettingsWindow] = None
        self._anim_source_id: Optional[int] = None
        self._last_monitors_signature = None
        self._monitor_change_timer_id = None

        try:
            signal.signal(signal.SIGUSR1, lambda *a: GLib.idle_add(self._on_sigusr1))
            signal.signal(signal.SIGUSR2, lambda *a: GLib.idle_add(self.open_settings_window))
        except Exception:
            pass

        self._ensure_kde_taskbar_visible()
        self.set_kde_native_notif_hidden(True)
        self.set_native_kde_osd_enabled(False)
        self._apply_css()

        self.mpris = MprisWatcher(self._on_mpris_change)
        self.mpris.start()
        self.spectrum = AudioSpectrumAnalyzer()

        self.prayer = PrayerManager(self.render_all, on_city_change=self._on_city_changed)
        self._prev_prayer_alert = None

        self.weather = WeatherManager(self.prayer, self.render_all)
        self.finance = FinanceManager(self.render_all)
        self.privacy = PrivacyMonitor(self.render_all)
        self.privacy.start()

        self.island_shape = "notch"
        self.standby_display = "clock"
        self.standby_weather = True
        self.standby_battery = True
        self.standby_lyrics = True
        self.standby_finance = True
        self.standby_privacy = True
        self.hide_on_fullscreen = True
        self._raw_fullscreen = False
        self.is_fullscreen_hidden = False
        self.adzan_vibration_sec = 10
        self.last_activity_time = time.time()
        self.quote_mgr = QuoteManager(self)
        self._load_settings()

        self.rebuild_windows()
        self._last_monitors_signature = self._get_current_monitors_signature()

        disp = Gdk.Display.get_default()
        if disp:
            try:
                disp.connect("monitor-added", self._on_display_monitors_changed)
                disp.connect("monitor-removed", self._on_display_monitors_changed)
            except Exception:
                pass

        screen = Gdk.Screen.get_default()
        if screen:
            try:
                screen.connect("monitors-changed", self._on_display_monitors_changed)
                screen.connect("size-changed", self._on_display_monitors_changed)
            except Exception:
                pass

        self.dbus_listener = DBusNotificationListener(self.on_notification_received)
        self.dbus_listener.start()

        self.audio_watcher = AudioWatcher(self.on_hud_event)
        self.audio_watcher.start()

        self.osd_listener = DBusOsdListener(self.on_hud_event)
        self.osd_listener.start()

        self.backlight_watcher = BacklightWatcher(self.on_hud_event)
        self.backlight_watcher.start()

        self.bluetooth_watcher = BluetoothWatcher(self.on_bluetooth_event)
        self.bluetooth_watcher.start()

        self.last_cursor_pos: Tuple[int, int] = (960, 540)
        self.last_cursor_move_time: float = 0.0
        self.last_typing_time: float = 0.0
        self.last_notif_time: float = 0.0
        self.angry_until: float = 0.0
        self.forced_eye_state: Optional[str] = None
        self._last_eye_poke: float = 0.0
        self._eye_poke_count: int = 0

        self.cursor_tracker = PuloCursorTracker(
            self.on_cursor_moved,
            fullscreen_callback=self.on_fullscreen_changed,
        )
        self.typing_watcher = TypingWatcher(self.register_typing, self.is_cursor_moving)
        self.typing_watcher.start()
        self.system_ctrl = SystemControlManager(self.render_all, hud_callback=self.show_hud)
        self.health = HealthManager(self)

        GLib.timeout_add(1000, self._on_second_tick)
        GLib.timeout_add(45, self._on_toast_tick)
        trim_memory()

    def on_cursor_moved(self, x: int, y: int):
        now = time.time()
        self.last_cursor_pos = (x, y)
        self.last_cursor_move_time = now
        if (now - self.last_activity_time) > 1.0:
            self.register_activity()
        if self.standby_display == "eyes":
            if (now - getattr(self, "_last_cursor_render_time", 0.0)) >= 0.033:
                self._last_cursor_render_time = now
                for win in self.windows:
                    if not win.panel_open:
                        win.render_now()

    def register_typing(self):
        self.last_typing_time = time.time()
        self.register_activity()
        if self.standby_display == "eyes":
            for win in self.windows:
                if not win.panel_open:
                    win.render_now()

    def is_cursor_moving(self) -> bool:
        return (time.time() - getattr(self, "last_cursor_move_time", 0.0)) < 0.15

    def trigger_angry(self, duration: float = 4.5):
        self.angry_until = time.time() + duration
        self.register_activity()
        if self.standby_display == "eyes":
            for win in self.windows:
                if not win.panel_open:
                    win.render_now()

    def is_currently_typing(self) -> bool:
        if getattr(self, "forced_eye_state", None) == "typing":
            return True
        if getattr(self, "forced_eye_state", None) is not None:
            return False
        return (time.time() - getattr(self, "last_typing_time", 0.0)) < 1.6

    def is_currently_angry(self) -> bool:
        if getattr(self, "forced_eye_state", None) == "angry":
            return True
        return time.time() < getattr(self, "angry_until", 0.0)

    def is_notification_active(self) -> bool:
        if getattr(self, "forced_eye_state", None) == "notif":
            return True
        if getattr(self, "forced_eye_state", None) is not None:
            return False
        if self.active_toast is not None:
            return True
        if len(self.notifications) > 0:
            return True
        return (time.time() - getattr(self, "last_notif_time", 0.0)) < 8.0

    def _ensure_kde_taskbar_visible(self):
        """Ensures the KDE top taskbar remains visible while Pulo Notch runs."""
        script = "panels().forEach(p => { if (p.location === 'top') p.hiding = 'none'; });"
        try:
            subprocess.Popen(
                [
                    "qdbus6",
                    "org.kde.plasmashell",
                    "/PlasmaShell",
                    "org.kde.PlasmaShell.evaluateScript",
                    script,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def set_kde_native_notif_hidden(self, hide_popups: bool):
        """Hides or restores KDE Plasma's native notification popups via org.freedesktop.Notifications.Inhibit."""
        self.kde_top_autohide = hide_popups
        try:
            if self._session_bus is None:
                self._session_bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            if hide_popups and self._inhibit_cookie is None:
                res = self._session_bus.call_sync(
                    "org.freedesktop.Notifications",
                    "/org/freedesktop/Notifications",
                    "org.freedesktop.Notifications",
                    "Inhibit",
                    GLib.Variant("(ssa{sv})", ("pulo", "Ditangani oleh Pulo Notch", {})),
                    GLib.VariantType("(u)"),
                    Gio.DBusCallFlags.NONE,
                    2000,
                    None,
                )
                self._inhibit_cookie = int(res.unpack()[0])
            elif not hide_popups and self._inhibit_cookie is not None:
                self._session_bus.call_sync(
                    "org.freedesktop.Notifications",
                    "/org/freedesktop/Notifications",
                    "org.freedesktop.Notifications",
                    "UnInhibit",
                    GLib.Variant("(u)", (self._inhibit_cookie,)),
                    None,
                    Gio.DBusCallFlags.NONE,
                    2000,
                    None,
                )
                self._inhibit_cookie = None
        except Exception:
            pass

    def toggle_kde_top_panel(self):
        self.set_kde_native_notif_hidden(not self.kde_top_autohide)
        for win in self.windows:
            win.render_now()

    def set_native_kde_osd_enabled(self, enabled: bool):
        val = "true" if enabled else "false"
        try:
            subprocess.run(
                ["kwriteconfig6", "--file", "plasmarc", "--group", "OSD", "--key", "Enabled", val],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def register_activity(self):
        self.last_activity_time = time.time()

    def get_idle_seconds(self) -> float:
        forced = getattr(self, "forced_eye_state", None)
        if forced == "sleep":
            return 100.0
        if forced == "sleepy":
            return 60.0
        if forced == "normal":
            return 0.0
        return max(0.0, time.time() - self.last_activity_time)

    def trigger_test_adzan(self):
        pname = "Ashar"
        if self.prayer:
            nxt_name, _, _ = self.prayer.get_next_prayer()
            pname = nxt_name
            self.prayer.simulated_alert = PrayerAlert(
                name=pname,
                time_str="15:15",
                minutes_left=0,
                is_adzan=True,
            )
        self.on_hud_event(
            "prayer",
            0,
            False,
            extra_text=f"Telah Memasuki Waktu Adzan {pname}",
            duration=float(self.adzan_vibration_sec),
        )
        GLib.timeout_add(int(self.adzan_vibration_sec * 1000), self._clear_simulated_prayer)

    def _clear_simulated_prayer(self):
        if self.prayer and self.prayer.simulated_alert:
            self.prayer.simulated_alert = None
            self.render_all()
        return False

    def on_hud_event(self, kind: str, value: int, is_muted: bool, extra_text: str = "", duration: Optional[float] = None):
        self.register_activity()
        if any(w.panel_open for w in self.windows):
            return

        dur = duration if duration is not None else (2.5 if kind == "bluetooth" else 2.2)
        self.active_hud = HudItem(
            kind=kind,
            value=max(0, min(100, value)),
            is_muted=is_muted,
            extra_text=extra_text,
            started_at=time.time(),
            duration=dur,
        )
        self.trigger_animation()
        for win in self.windows:
            win.render_now()

    def show_hud(self, kind: str, value: int = 100, is_muted: bool = False, extra_text: str = "", duration: Optional[float] = None):
        self.on_hud_event(kind, value, is_muted, extra_text, duration)

    def dismiss_hud(self):
        if self.active_hud is not None:
            self.active_hud = None
            self.trigger_animation()
            for win in self.windows:
                win.render_now()

    def on_bluetooth_event(self, device_name: str, is_connected: bool):
        self.on_hud_event(
            "bluetooth",
            100 if is_connected else 0,
            not is_connected,
            extra_text=device_name,
        )
        summary = f"{device_name} Terhubung" if is_connected else f"{device_name} Terputus"
        body = (
            "Perangkat Bluetooth berhasil tersambung ke komputer."
            if is_connected
            else "Koneksi perangkat Bluetooth telah terputus."
        )
        self.on_notification_received("Bluetooth", summary, body)

    def _apply_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS_RESET)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    @staticmethod
    def get_primary_monitor_index(disp: Gdk.Display) -> int:
        n = disp.get_n_monitors()
        if n <= 1:
            return 0
        prim = disp.get_primary_monitor()
        if prim is not None:
            for i in range(n):
                if disp.get_monitor(i) == prim:
                    return i

        def _match_pos(px: int, py: int) -> Optional[int]:
            for i in range(n):
                g = disp.get_monitor(i).get_geometry()
                if g.x == px and g.y == py:
                    return i
            return None

        # 1. KDE Plasma Wayland / X11: kscreen-doctor -j (priority == 1)
        try:
            out = subprocess.check_output(["kscreen-doctor", "-j"], stderr=subprocess.DEVNULL, timeout=2).decode("utf-8", errors="ignore")
            data = json.loads(out)
            best_out = None
            best_prio = 999999
            for o in data.get("outputs", []):
                if o.get("enabled"):
                    prio = int(o.get("priority", 999999))
                    if prio < best_prio:
                        best_prio = prio
                        best_out = o
            if best_out and "pos" in best_out:
                pos = best_out["pos"]
                idx = _match_pos(int(pos.get("x", 0)), int(pos.get("y", 0)))
                if idx is not None:
                    return idx
        except Exception:
            pass

        # 2. GNOME Wayland (Fedora Workstation): org.gnome.Mutter.DisplayConfig
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            res = bus.call_sync(
                "org.gnome.Mutter.DisplayConfig",
                "/org/gnome/Mutter/DisplayConfig",
                "org.gnome.Mutter.DisplayConfig",
                "GetCurrentState",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                1500,
                None,
            )
            if res is not None:
                logical_monitors = res.unpack()[2]
                for lm in logical_monitors:
                    lx, ly, _scale, _trans, is_prim = int(lm[0]), int(lm[1]), lm[2], lm[3], bool(lm[4])
                    if is_prim:
                        idx = _match_pos(lx, ly)
                        if idx is not None:
                            return idx
        except Exception:
            pass

        # 3. X11 / XWayland fallback: xrandr --query
        try:
            xout = subprocess.check_output(["xrandr", "--query"], stderr=subprocess.DEVNULL, timeout=2).decode("utf-8", errors="ignore")
            m = re.search(r"connected primary \d+x\d+\+(\d+)\+(\d+)", xout)
            if m:
                idx = _match_pos(int(m.group(1)), int(m.group(2)))
                if idx is not None:
                    return idx
        except Exception:
            pass

        for i in range(n):
            geom = disp.get_monitor(i).get_geometry()
            if geom.x == 0 and geom.y == 0:
                return i
        return 0

    @property
    def monitor_mode_label(self) -> str:
        disp = Gdk.Display.get_default()
        n = disp.get_n_monitors()
        if self.monitor_mode == 0:
            return "Layar Utama"
        if self.monitor_mode == 1 or n <= 1:
            return "Semua Layar"
        idx = self.monitor_mode - 2
        if 0 <= idx < n:
            m = disp.get_monitor(idx)
            return m.get_model() or f"Layar {idx + 1}"
        return "Layar Utama"

    def cycle_monitor_mode(self):
        disp = Gdk.Display.get_default()
        n = disp.get_n_monitors()
        self.monitor_mode = (self.monitor_mode + 1) % (2 + n)
        self.rebuild_windows()
        for win in self.windows:
            win.set_panel_open(True, tab="settings")

    def _on_display_monitors_changed(self, *_args):
        if getattr(self, "_monitor_change_timer_id", None) is not None:
            try:
                GLib.source_remove(self._monitor_change_timer_id)
            except Exception:
                pass
        self._monitor_change_timer_id = GLib.timeout_add(350, self._handle_monitors_changed)

    def _handle_monitors_changed(self) -> bool:
        self._monitor_change_timer_id = None
        self._check_and_update_monitors(force=True)
        return False

    def _get_current_monitors_signature(self) -> Tuple:
        disp = Gdk.Display.get_default()
        if not disp:
            return ()
        n = disp.get_n_monitors()
        sig = []
        for i in range(n):
            m = disp.get_monitor(i)
            if m is not None:
                geom = m.get_geometry()
                model = m.get_model() or ""
                sig.append((i, model, geom.x, geom.y, geom.width, geom.height))
        return tuple(sig)

    def _check_and_update_monitors(self, force: bool = False):
        sig = self._get_current_monitors_signature()
        if force or sig != getattr(self, "_last_monitors_signature", None):
            self._last_monitors_signature = sig
            disp = Gdk.Display.get_default()
            n = disp.get_n_monitors() if disp else 1
            if self.monitor_mode >= 2:
                idx = self.monitor_mode - 2
                if idx >= n or idx < 0:
                    self.monitor_mode = 0  # Fallback ke Layar Utama (laptop screen)
            self.rebuild_windows()
            if self.settings_window and hasattr(self.settings_window, "btn_mon"):
                try:
                    self.settings_window.btn_mon.set_label(self.monitor_mode_label)
                except Exception:
                    pass
            self.trigger_animation()
            self.render_all()

    def rebuild_windows(self):
        for w in self.windows:
            try:
                w.hide()
                w.destroy()
            except Exception:
                pass
        self.windows.clear()
        for b in self.backdrops:
            try:
                b.hide()
                b.destroy()
            except Exception:
                pass
        self.backdrops.clear()

        disp = Gdk.Display.get_default()
        if not disp:
            return
        n = disp.get_n_monitors()
        if n == 0:
            return

        for i in range(n):
            mon = disp.get_monitor(i)
            if mon is not None:
                bd = BackdropWindow(self, mon)
                self.backdrops.append(bd)

        if self.monitor_mode == 0:
            prim_idx = self.get_primary_monitor_index(disp)
            mon = disp.get_monitor(prim_idx) if prim_idx < n else disp.get_monitor(0)
            if mon is not None:
                label = mon.get_model() or "Layar Utama"
                win = NotchWindow(self, mon, label)
                if not getattr(self, "is_fullscreen_hidden", False):
                    win.show_all()
                self.windows.append(win)
        elif self.monitor_mode == 1 or self.monitor_mode > (1 + n):
            for i in range(n):
                mon = disp.get_monitor(i)
                if mon is not None:
                    win = NotchWindow(self, mon, mon.get_model() or f"Layar {i + 1}")
                    if not getattr(self, "is_fullscreen_hidden", False):
                        win.show_all()
                    self.windows.append(win)
        else:
            idx = self.monitor_mode - 2
            if idx >= n or idx < 0:
                idx = 0
                self.monitor_mode = 0
            mon = disp.get_monitor(idx)
            if mon is not None:
                win = NotchWindow(self, mon, mon.get_model() or f"Layar {idx + 1}")
                if not getattr(self, "is_fullscreen_hidden", False):
                    win.show_all()
                self.windows.append(win)

    def show_backdrops(self):
        for bd in self.backdrops:
            bd.show_all()

    def hide_backdrops(self):
        for bd in self.backdrops:
            bd.hide()

    def close_all_panels(self):
        for win in self.windows:
            if win.panel_open:
                win.set_panel_open(False)
        self.hide_backdrops()

    def open_settings_window(self):
        if self.settings_window is None:
            self.settings_window = SettingsWindow(self)
        self.settings_window.show_all()
        self.settings_window.present()

    def _on_sigusr1(self):
        cmd_file = os.path.join(get_cache_dir(), "cmd")
        cmd = None
        if os.path.exists(cmd_file):
            try:
                with open(cmd_file, "r") as f:
                    cmd = f.read().strip()
                os.remove(cmd_file)
            except Exception:
                pass

        if cmd and cmd.startswith("tab:"):
            target_tab = cmd.split(":", 1)[1]
            for w in self.windows:
                w.set_panel_open(True, tab=target_tab)
            return False

        if cmd and cmd.startswith("send:"):
            try:
                paths = json.loads(cmd.split(":", 1)[1])
                self.send_files_via_localsend(paths)
            except Exception:
                pass
            return False

        if cmd == "toggle-lyrics":
            self.toggle_standby_lyrics()
            return False

        if cmd == "test-adzan":
            self.trigger_test_adzan()
            return False

        if cmd == "finance:refresh":
            if hasattr(self, "finance") and self.finance:
                self.finance.fetch_online()
            return False

        if cmd == "toggle-panel-lyrics":
            for w in self.windows:
                if w.panel_open and w.active_tab == "music":
                    w.music_view_mode = "player" if w.music_view_mode == "lyrics" else "lyrics"
                    self.trigger_animation()
                    w.render_now()
            return False

        if cmd and cmd.startswith("eye:"):
            state = cmd.split(":", 1)[1]
            if state == "angry":
                self.trigger_angry(5.0)
            elif state in ("typing", "sleep", "sleepy", "normal", "notif"):
                self.forced_eye_state = state
                self.trigger_animation()
                for w in self.windows:
                    w.render_now()
            elif state in ("reset", "auto", "off"):
                self.forced_eye_state = None
                self.angry_until = 0.0
                self.register_activity()
                for w in self.windows:
                    w.render_now()
            return False

        if cmd and cmd.startswith("health:"):
            act = cmd.split(":", 1)[1]
            if hasattr(self, "health") and self.health:
                if act == "eye_break":
                    self.health.trigger_eye_break()
                elif act == "stretch_break":
                    self.health.trigger_stretch_break()
            return False

        if cmd and cmd.startswith("shape:"):
            shape = cmd.split(":", 1)[1]
            if shape in ("notch", "pill"):
                self.set_island_shape(shape)
            return False

        any_open = any(w.panel_open for w in self.windows)
        if any_open:
            self.close_all_panels()
        else:
            for w in self.windows:
                w.set_panel_open(True, tab="notifications")
        return False


    def _on_city_changed(self):
        if self.weather:
            self.weather.fetch_online()

    def _settings_path(self) -> str:
        if self.prayer:
            return self.prayer._config_path()
        d_new = os.path.expanduser("~/.config/pulo")
        d_old = os.path.expanduser("~/.config/pulau")
        if not os.path.isdir(d_new) and os.path.isdir(d_old):
            try:
                import shutil
                shutil.copytree(d_old, d_new)
            except Exception:
                pass
        os.makedirs(d_new, exist_ok=True)
        return os.path.join(d_new, "settings.json")

    def _load_settings(self):
        try:
            p = self._settings_path()
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    self.island_shape = cfg.get("island_shape", "notch")
                    self.standby_display = cfg.get("standby_display", "clock")
                    if self.standby_display not in ("clock", "eyes"):
                        self.standby_display = "clock"
                    self.standby_weather = cfg.get("standby_weather", True)
                    self.standby_battery = cfg.get("standby_battery", True)
                    self.standby_lyrics = cfg.get("standby_lyrics", True)
                    self.standby_finance = cfg.get("standby_finance", True)
                    self.standby_privacy = cfg.get("standby_privacy", True)
                    self.hide_on_fullscreen = cfg.get("hide_on_fullscreen", True)
                    self.adzan_vibration_sec = int(cfg.get("adzan_vibration_sec", 10))
                    if self.prayer:
                        self.prayer.adzan_vibration_sec = self.adzan_vibration_sec
                    if hasattr(self, "health") and self.health:
                        self.health.eye_enabled = cfg.get("health_eye_enabled", True)
                        self.health.eye_interval_sec = float(cfg.get("health_eye_interval_min", 20)) * 60.0
                        self.health.stretch_enabled = cfg.get("health_stretch_enabled", True)
                        self.health.stretch_interval_sec = float(cfg.get("health_stretch_interval_min", 50)) * 60.0
                        self.health.smart_idle_pause = cfg.get("health_smart_idle_pause", True)
                    if hasattr(self, "quote_mgr") and self.quote_mgr:
                        self.quote_mgr.morning_enabled = cfg.get("quote_morning_enabled", True)
                        self.quote_mgr.eye_break_enabled = cfg.get("quote_eye_break_enabled", True)
                        self.quote_mgr.category = cfg.get("quote_category", "random")
                        self.quote_mgr.last_morning_date = cfg.get("quote_last_morning_date", "")
        except Exception:
            pass

    def _save_settings(self):
        try:
            p = self._settings_path()
            cfg = {}
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                except Exception:
                    pass
            cfg["island_shape"] = self.island_shape
            cfg["standby_display"] = self.standby_display
            cfg["standby_weather"] = self.standby_weather
            cfg["standby_battery"] = self.standby_battery
            cfg["standby_lyrics"] = self.standby_lyrics
            cfg["standby_finance"] = self.standby_finance
            cfg["standby_privacy"] = self.standby_privacy
            cfg["hide_on_fullscreen"] = self.hide_on_fullscreen
            cfg["adzan_vibration_sec"] = self.adzan_vibration_sec
            if hasattr(self, "health") and self.health:
                cfg["health_eye_enabled"] = self.health.eye_enabled
                cfg["health_eye_interval_min"] = int(self.health.eye_interval_sec / 60.0)
                cfg["health_stretch_enabled"] = self.health.stretch_enabled
                cfg["health_stretch_interval_min"] = int(self.health.stretch_interval_sec / 60.0)
                cfg["health_smart_idle_pause"] = self.health.smart_idle_pause
            if hasattr(self, "quote_mgr") and self.quote_mgr:
                cfg["quote_morning_enabled"] = self.quote_mgr.morning_enabled
                cfg["quote_eye_break_enabled"] = self.quote_mgr.eye_break_enabled
                cfg["quote_category"] = self.quote_mgr.category
                cfg["quote_last_morning_date"] = self.quote_mgr.last_morning_date
            with open(p, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass

    def set_island_shape(self, shape: str):
        if shape in ("notch", "pill"):
            self.island_shape = shape
            self._save_settings()
            for win in self.windows:
                win.update_island_shape()
                win.render_now()

    def set_standby_lyrics(self, val: bool):
        self.standby_lyrics = bool(val)
        self._save_settings()
        self.trigger_animation()
        self.render_all()

    def toggle_standby_lyrics(self) -> bool:
        return self.set_standby_lyrics(not self.standby_lyrics)

    def set_standby_battery(self, val: bool):
        self.standby_battery = bool(val)
        self._save_settings()
        self.trigger_animation()
        self.render_all()

    def set_standby_finance(self, val: bool):
        self.standby_finance = bool(val)
        self._save_settings()
        self.trigger_animation()
        self.render_all()

    def set_standby_privacy(self, val: bool):
        self.standby_privacy = bool(val)
        self._save_settings()
        self.trigger_animation()
        self.render_all()

    def set_hide_on_fullscreen(self, val: bool):
        self.hide_on_fullscreen = bool(val)
        self._save_settings()
        self._sync_fullscreen_visibility()

    @staticmethod
    def _autostart_desktop_path() -> str:
        return os.path.expanduser("~/.config/autostart/pulo.desktop")

    def is_autostart_enabled(self) -> bool:
        p = self._autostart_desktop_path()
        if not os.path.isfile(p):
            return False
        try:
            with open(p, "r", encoding="utf-8") as f:
                txt = f.read()
            if "Hidden=true" in txt or "X-GNOME-Autostart-enabled=false" in txt:
                return False
            return True
        except Exception:
            return False

    def set_autostart_enabled(self, enabled: bool):
        p = self._autostart_desktop_path()
        if enabled:
            try:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                local_bin = os.path.expanduser("~/.local/bin/pulo")
                exec_cmd = local_bin if os.path.isfile(local_bin) and os.access(local_bin, os.X_OK) else f"{sys.executable} {os.path.abspath(__file__)}"
                content = (
                    "[Desktop Entry]\n"
                    "Type=Application\n"
                    "Name=Pulo\n"
                    "GenericName=Dynamic Island Notch\n"
                    "Comment=MacBook-Style Notch Dynamic Island untuk Linux (Wayland / X11)\n"
                    f"Exec={exec_cmd}\n"
                    "Icon=preferences-desktop-display\n"
                    "Terminal=false\n"
                    "Categories=Utility;TrayIcon;\n"
                    "StartupWMClass=pulo\n"
                    "X-KDE-autostart-after=panel\n"
                    "StartupNotify=false\n"
                    "X-GNOME-Autostart-enabled=true\n"
                )
                with open(p, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception:
                pass
        else:
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except Exception:
                pass

    def on_fullscreen_changed(self, is_fs: bool):
        self._raw_fullscreen = bool(is_fs)
        self._sync_fullscreen_visibility()
        return False

    def _sync_fullscreen_visibility(self):
        should_hide = bool(getattr(self, "_raw_fullscreen", False) and getattr(self, "hide_on_fullscreen", True))
        if should_hide == getattr(self, "is_fullscreen_hidden", False):
            return
        self.is_fullscreen_hidden = should_hide
        if should_hide:
            self.close_all_panels()
            for win in self.windows:
                win.hide()
        else:
            for win in self.windows:
                win.show_all()
                win.render_now()
            self.trigger_animation()

    def cycle_standby_display(self) -> str:
        modes = ["clock", "eyes"]
        try:
            idx = modes.index(self.standby_display)
            self.standby_display = modes[(idx + 1) % len(modes)]
        except ValueError:
            self.standby_display = "clock"
        self._save_settings()
        self.trigger_animation()
        for win in self.windows:
            win.render_now()
        return self.standby_display

    def toggle_standby_display(self) -> str:
        return self.cycle_standby_display()

    def toggle_standby_weather(self) -> bool:
        self.standby_weather = not self.standby_weather
        self._save_settings()
        self.trigger_animation()
        for win in self.windows:
            win.render_now()
        return self.standby_weather

    def send_files_via_localsend(self, file_paths: List[str]):
        if not file_paths:
            return
        bin_path = "/snap/bin/localsend"
        if not os.path.exists(bin_path):
            bin_path = shutil.which("localsend") or "localsend"

        try:
            subprocess.Popen([bin_path, *file_paths])
            cnt = len(file_paths)
            first_name = os.path.basename(file_paths[0])
            txt = f"{first_name}" if cnt == 1 else f"{cnt} file ({first_name}, ...)"
            self.on_hud_event(
                "localsend",
                100,
                False,
                extra_text=f"Membuka LocalSend: {txt}",
            )
        except Exception as e:
            subprocess.Popen([
                "notify-send",
                "-a", "Pulo",
                "Gagal Membuka LocalSend",
                str(e),
            ])

    def open_localsend_app(self):
        bin_path = "/snap/bin/localsend"
        if not os.path.exists(bin_path):
            bin_path = shutil.which("localsend") or "localsend"

        try:
            subprocess.Popen([bin_path])
            self.on_hud_event(
                "localsend",
                100,
                False,
                extra_text="Membuka Aplikasi LocalSend",
            )
        except Exception as e:
            subprocess.Popen([
                "notify-send",
                "-a", "Pulo",
                "Gagal Membuka LocalSend",
                str(e),
            ])

    def trigger_animation(self):
        if self._anim_source_id is None:
            self._anim_source_id = GLib.timeout_add(16, self._step_all_animations)

    def _step_all_animations(self) -> bool:
        still_moving = False
        for win in self.windows:
            if win.step_animation():
                still_moving = True
        if not still_moving:
            self._anim_source_id = None
            return False
        return True

    @property
    def toast_remaining_ratio(self) -> float:
        if self.active_toast is None:
            return 0.0
        elapsed = self.toast_elapsed_before_pause
        if not self.toast_paused:
            elapsed += time.time() - self.toast_started_at
        return max(0.0, 1.0 - (elapsed / self.TOAST_DURATION))

    def set_toast_paused(self, paused: bool):
        if self.active_toast is None or self.toast_paused == paused:
            return
        if paused:
            self.toast_elapsed_before_pause += time.time() - self.toast_started_at
            self.toast_paused = True
        else:
            self.toast_started_at = time.time()
            self.toast_paused = False

    def on_notification_received(self, app_name: str, summary: str, body: str, app_icon: str = "", desktop_entry: str = ""):
        self.register_activity()
        self.last_notif_time = time.time()
        now_str = datetime.datetime.now().strftime("%H:%M")
        item = NotificationItem(
            id=self._next_id,
            app_name=app_name,
            summary=summary,
            body=body,
            timestamp=now_str,
            app_icon=app_icon,
            desktop_entry=desktop_entry,
        )
        self._next_id += 1
        self.notifications.insert(0, item)
        if len(self.notifications) > 50:
            self.notifications = self.notifications[:50]

        if not self.dnd and not any(w.panel_open for w in self.windows):
            self.active_toast = item
            self.toast_started_at = time.time()
            self.toast_elapsed_before_pause = 0.0
            self.toast_paused = any(w.is_hovered for w in self.windows)
            self.trigger_animation()

        for win in self.windows:
            win.render_now()
        return False

    def dismiss_toast(self, silent: bool = False):
        if self.active_toast is not None:
            self.active_toast = None
            if not silent:
                self.trigger_animation()
                for win in self.windows:
                    win.render_now()
            trim_memory()

    def remove_notification(self, notif_id: int):
        self.notifications = [n for n in self.notifications if n.id != notif_id]
        self.trigger_animation()
        for win in self.windows:
            win.render_now()
        trim_memory()

    def open_notification_app(self, item: NotificationItem):
        """Launch or focus the application associated with a notification and dismiss it."""
        self.remove_notification(item.id)
        app_name = (item.app_name or "").strip()
        low_app = app_name.lower()

        if low_app == "localsend":
            for win in self.windows:
                win.set_panel_open(False)
            self.open_localsend_app()
            return
        if low_app == "waktu sholat":
            for win in self.windows:
                win.set_panel_open(True, tab="prayer")
            return
        if low_app == "bluetooth":
            for win in self.windows:
                win.set_panel_open(True, tab="control")
            return

        for win in self.windows:
            win.set_panel_open(False)

        def _launch_bg():
            clean_env = SystemControlManager._clean_gui_env()
            cands: List[str] = []
            for raw in (item.desktop_entry, item.app_icon, app_name):
                s = (raw or "").strip()
                if not s or "/" in s:
                    continue
                if s.endswith(".desktop"):
                    s = s[:-8]
                cands.append(s)
                cands.append(s.lower())
                cands.append(s.lower().replace(" ", "-"))
                cands.append(s.lower().replace(" ", "_"))
                cands.append(s.lower().replace(" ", ""))

            seen = set()
            unique_cands = [c for c in cands if c and not (c in seen or seen.add(c))]

            found_desktop: Optional[str] = None
            for d in IconResolver.DESKTOP_DIRS:
                if not os.path.isdir(d):
                    continue
                for c in unique_cands:
                    p_cand = os.path.join(d, f"{c}.desktop")
                    if os.path.isfile(p_cand):
                        found_desktop = p_cand
                        break
                if found_desktop:
                    break

            if not found_desktop and low_app:
                for d in IconResolver.DESKTOP_DIRS:
                    if not os.path.isdir(d):
                        continue
                    try:
                        for fname in os.listdir(d):
                            if not fname.endswith(".desktop"):
                                continue
                            fpath = os.path.join(d, fname)
                            try:
                                with open(fpath, "r", encoding="utf-8", errors="ignore") as fp:
                                    d_name = ""
                                    wm_class = ""
                                    for ln in fp:
                                        if ln.startswith("Name=") and not d_name:
                                            d_name = ln.split("=", 1)[1].strip().lower()
                                        elif ln.startswith("StartupWMClass=") and not wm_class:
                                            wm_class = ln.split("=", 1)[1].strip().lower()
                                        if d_name and wm_class:
                                            break
                                    if low_app in (d_name, wm_class) or (d_name and low_app in d_name):
                                        found_desktop = fpath
                                        break
                            except Exception:
                                continue
                    except Exception:
                        continue
                    if found_desktop:
                        break

            if found_desktop:
                desktop_id = os.path.basename(found_desktop)
                if shutil.which("gtk-launch"):
                    try:
                        res = subprocess.run(
                            ["gtk-launch", desktop_id],
                            env=clean_env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=4,
                        )
                        if res.returncode == 0:
                            return
                    except Exception:
                        pass
                if shutil.which("gio"):
                    try:
                        subprocess.Popen(
                            ["gio", "launch", found_desktop],
                            env=clean_env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        return
                    except Exception:
                        pass

            for c in unique_cands:
                exe = shutil.which(c)
                if exe:
                    try:
                        subprocess.Popen([exe], env=clean_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        return
                    except Exception:
                        pass

            self.show_hud("utility_msg", value=0, extra_text=f"Aplikasi '{app_name}' tidak ditemukan", duration=3.0)

        threading.Thread(target=_launch_bg, daemon=True).start()

    def clear_notifications(self):
        self.notifications.clear()
        self.trigger_animation()
        for win in self.windows:
            win.render_now()
        trim_memory()

    def toggle_dnd(self):
        self.dnd = not self.dnd
        if self.dnd:
            self.dismiss_toast(silent=True)
        self.trigger_animation()
        for win in self.windows:
            win.render_now()

    def send_test_notification(self, open_toast: bool = True):
        if open_toast:
            for win in self.windows:
                if win.panel_open:
                    win.set_panel_open(False)
        bat = self.battery
        subprocess.Popen(
            [
                "notify-send",
                "-a",
                "Pulo",
                "Notifikasi Notch Aktif",
                f"Baterai {bat.capacity}% ({bat.short_badge}). Klik notch untuk membuka panel.",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def render_all(self):
        needs_anim = False
        for win in self.windows:
            win._sync_target_size()
            if abs(win.target_w - win.cur_w) > 0.8 or abs(win.target_h - win.cur_h) > 0.8:
                needs_anim = True
            else:
                win.render_now()
        if needs_anim:
            self.trigger_animation()

    def _on_second_tick(self) -> bool:
        self.battery = PowerReader.read()

        if self.prayer:
            alert = self.prayer.check_alert()
            if alert != self._prev_prayer_alert:
                prev = self._prev_prayer_alert
                self._prev_prayer_alert = alert
                self.trigger_animation()
                if alert:
                    if alert.is_adzan:
                        self.on_hud_event(
                            "prayer",
                            0,
                            False,
                            extra_text=f"Telah Memasuki Waktu Adzan {alert.name}",
                            duration=float(self.adzan_vibration_sec),
                        )
                    elif not prev or prev.name != alert.name:
                        self.on_hud_event(
                            "prayer",
                            0,
                            False,
                            extra_text=f"5 Menit Menuju Sholat {alert.name} ({alert.time_str})",
                        )

        if self.battery.ac_online != self._prev_ac_online:
            self._prev_ac_online = self.battery.ac_online
            self.on_hud_event(
                "power_plug",
                self.battery.capacity,
                is_muted=(not self.battery.ac_online),
                extra_text=self.battery.detail_label,
                duration=2.8,
            )
        elif self.battery.is_full and not self._prev_is_full:
            self._prev_is_full = True
            self.on_notification_received(
                "Sistem Daya",
                "Baterai Penuh 100%",
                "Pengisian daya selesai, baterai siap digunakan.",
            )
        else:
            self._prev_is_full = self.battery.is_full

        if hasattr(self, "spectrum") and self.spectrum:
            self.spectrum.set_active(bool(self.mpris and self.mpris.is_playing))
        if self.mpris and (self.mpris.is_playing or any(w.panel_open and w.active_tab == "music" for w in self.windows)):
            self.mpris.query_position()

        if self.weather and self.weather.data and self.weather.data.last_updated > 0:
            if time.time() - self.weather.data.last_updated >= 900:
                self.weather.fetch_online()

        if hasattr(self, "finance") and self.finance and self.finance.data and self.finance.data.last_updated > 0:
            if time.time() - self.finance.data.last_updated >= 600:
                self.finance.fetch_online()

        if hasattr(self, "system_ctrl") and self.system_ctrl:
            if any(w.panel_open and w.active_tab == "control" for w in self.windows):
                self.system_ctrl.refresh_async()

        if hasattr(self, "health") and self.health:
            self.health.on_second_tick()
        if hasattr(self, "quote_mgr") and self.quote_mgr:
            self.quote_mgr.on_second_tick()

        self._sec_tick_count = getattr(self, "_sec_tick_count", 0) + 1
        if self._sec_tick_count % 30 == 0:
            trim_memory()

        # Dynamic multi-monitor watchdog (checks if external monitor was plugged or unplugged)
        self._check_and_update_monitors(force=False)

        for win in self.windows:
            win.render_now()
        return True

    def _on_mpris_change(self):
        if hasattr(self, "spectrum") and self.spectrum:
            self.spectrum.set_active(bool(self.mpris and self.mpris.is_playing))
        self.register_activity()
        self.trigger_animation()
        for win in self.windows:
            win.render_now()

    def _is_standby_animating(self) -> bool:
        if self.standby_display != "eyes":
            return False
        if any(w.panel_open for w in self.windows):
            return False
        if self.is_currently_angry():
            return True
        if self.is_currently_typing():
            return True
        if self.is_notification_active():
            return True
        idle = self.get_idle_seconds()
        if idle >= 90.0:  # Floating Z's drift animation
            return True
        now_sec = time.time()
        # Normal lively blink cycle lasts ~0.22s every 3.6s
        if (now_sec % 3.6) < 0.22:
            return True
        return False

    def _on_toast_tick(self) -> bool:
        if self.active_hud is not None:
            if time.time() - self.active_hud.started_at >= self.active_hud.duration:
                self.active_hud = None
                self.trigger_animation()
                for win in self.windows:
                    win.render_now()
            else:
                for win in self.windows:
                    if not win.panel_open:
                        win.render_now()

        if self.active_toast is not None:
            if self.toast_remaining_ratio <= 0.0:
                self.dismiss_toast()
            else:
                for win in self.windows:
                    if not win.panel_open and self.active_hud is None:
                        win.render_now()
        elif (
            (self.mpris and self.mpris.is_playing)
            or self._is_standby_animating()
            or (self.prayer and self.prayer.check_alert() and self.prayer.check_alert().is_adzan)
        ):
            # Animate vibrating crescent moon in adzan, blinking eyes in standby, or bouncing equalizer / music progress
            for win in self.windows:
                if not win.panel_open or (win.active_tab == "music" and self.mpris and self.mpris.is_playing):
                    win.render_now()
        return True

    def quit(self):
        self.set_kde_native_notif_hidden(False)
        self.set_native_kde_osd_enabled(True)
        self.dbus_listener.stop()
        self.audio_watcher.stop()
        self.osd_listener.stop()
        self.backlight_watcher.stop()
        self.bluetooth_watcher.stop()
        self.mpris.stop()
        if hasattr(self, "spectrum") and self.spectrum:
            self.spectrum.stop()
        if hasattr(self, "privacy") and self.privacy:
            self.privacy.stop()
        if hasattr(self, "typing_watcher") and self.typing_watcher:
            self.typing_watcher.stop()
        if hasattr(self, "cursor_tracker") and self.cursor_tracker:
            self.cursor_tracker.stop()
        if self.settings_window:
            try:
                self.settings_window.destroy()
            except Exception:
                pass
            self.settings_window = None
        trim_memory()
        cleanup_pid()
        Gtk.main_quit()


# =============================================================================
# Process Management: Lockfile, Daemonize & Background Service Control
# =============================================================================
_lock_fd = None


def get_cache_dir() -> str:
    path = os.path.expanduser("~/.cache/pulo")
    os.makedirs(path, exist_ok=True)
    return path


def get_running_pid() -> Optional[int]:
    # Check new pulo.pid first, then legacy pulau.pid
    candidates = [
        os.path.join(get_cache_dir(), "pulo.pid"),
        os.path.expanduser("~/.cache/pulau/pulau.pid"),
    ]
    for pid_file in candidates:
        if os.path.isfile(pid_file):
            try:
                with open(pid_file, "r", encoding="utf-8") as f:
                    pid = int(f.read().strip())
                os.kill(pid, 0)
                return pid
            except (ValueError, OSError):
                pass
    return None


def acquire_lock_or_exit():
    global _lock_fd
    cache_dir = get_cache_dir()
    lock_file = os.path.join(cache_dir, "pulo.lock")
    _lock_fd = open(lock_file, "a+", encoding="utf-8")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, BlockingIOError):
        pid = get_running_pid()
        pid_str = f" (PID {pid})" if pid else ""
        print(f"Pulo Notch sudah berjalan di background{pid_str}.")
        print("Perintah yang tersedia:")
        print("  pulo --toggle    Buka / tutup panel Notch")
        print("  pulo --stop      Hentikan proses background")
        print("  pulo --restart   Mulai ulang proses background")
        print("  pulo --status    Cek status proses")
        sys.exit(0)

    pid_file = os.path.join(cache_dir, "pulo.pid")
    try:
        with open(pid_file, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def cleanup_pid():
    for f in [
        os.path.join(get_cache_dir(), "pulo.pid"),
        os.path.expanduser("~/.cache/pulau/pulau.pid"),
    ]:
        try:
            if os.path.isfile(f):
                os.remove(f)
        except Exception:
            pass


def daemonize():
    cache_dir = get_cache_dir()
    log_file = os.path.join(cache_dir, "pulo.log")
    log_fd = open(log_file, "a+", encoding="utf-8")

    cmd = [sys.executable, os.path.realpath(__file__), "--foreground"]
    for arg in sys.argv[1:]:
        if arg not in ("--daemon", "-d", "--background", "--restart"):
            cmd.append(arg)

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=log_fd,
        stderr=log_fd,
        start_new_session=True,
        close_fds=True,
    )
    print(f"Pulo Notch berjalan di background (PID {proc.pid}).")
    print(f"Log aktif di: {log_file}")
    sys.exit(0)


def stop_running():
    pid = get_running_pid()
    if not pid:
        print("Pulo Notch tidak sedang berjalan.")
        cleanup_pid()
        return
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Menghentikan Pulo Notch (PID {pid})...")
        for _ in range(25):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except OSError:
                print("Pulo Notch berhasil dihentikan.")
                cleanup_pid()
                return
        os.kill(pid, signal.SIGKILL)
        print("Pulo Notch dihentikan paksa (SIGKILL).")
        cleanup_pid()
    except OSError as e:
        print(f"Gagal menghentikan: {e}")


def toggle_running():
    pid = get_running_pid()
    if not pid:
        print("Pulo Notch tidak sedang berjalan.")
        return
    try:
        os.kill(pid, signal.SIGUSR1)
        print(f"Toggle panel dikirim ke PID {pid}.")
    except OSError as e:
        print(f"Gagal mengirim sinyal: {e}")


def open_settings_cli():
    pid = get_running_pid()
    if not pid:
        print("Pulo Notch tidak sedang berjalan.")
        return
    try:
        os.kill(pid, signal.SIGUSR2)
        print(f"Sinyal buka pengaturan dikirim ke PID {pid}.")
    except OSError as e:
        print(f"Gagal membuka pengaturan: {e}")


def check_status():
    pid = get_running_pid()
    if pid:
        print(f"Pulo Notch AKTIF di background (PID {pid}).")
        log_file = os.path.join(get_cache_dir(), "pulo.log")
        if os.path.isfile(log_file):
            print(f"Berkas log: {log_file}")
    else:
        print("Pulo Notch TIDAK aktif.")


def main():
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print("Pulo - MacBook-Style Notch Dynamic Island")
        print("Penggunaan:")
        print("  pulo               Jalankan di background")
        print("  pulo --daemon      Jalankan di background (eksplisit)")
        print("  pulo --foreground  Jalankan di terminal (foreground)")
        print("  pulo --stop        Hentikan proses background")
        print("  pulo --restart     Mulai ulang proses background")
        print("  pulo --status      Lihat status proses")
        print("  pulo --toggle      Buka / tutup panel")
        print("  pulo --control     Buka panel Mini Control Center (Pintasan Sistem)")
        print("  pulo --finance     Buka panel Keuangan (IHSG & Kurs Rupiah)")
        print("  pulo --lyrics      Toggle lirik standby (Live Karaoke)")
        print("  pulo --eye <state> Uji ekspresi mata (angry|typing|sleep|sleepy|notif|normal|reset)")
        print("  pulo --eye-break   Uji pengingat istirahat mata 20-20-20 (20s)")
        print("  pulo --stretch-break Uji pengingat berdiri & peregangan (50m)")
        print("  pulo --shape <notch|pill> Ganti bentuk tampilan (MacBook Notch / Floating Pill)")
        print("  pulo --settings    Buka jendela pengaturan")
        print("  pulo --test-adzan  Uji notifikasi banner adzan")
        print("  pulo --test-toast  Uji notifikasi pop-up saat mulai")
        return

    if "--stop" in args or "--kill" in args:
        stop_running()
        return

    if "--status" in args:
        check_status()
        return

    if "--toggle" in args:
        toggle_running()
        return

    if "--control" in args or "--control-center" in args:
        cmd_file = os.path.join(get_cache_dir(), "cmd")
        try:
            with open(cmd_file, "w", encoding="utf-8") as f:
                f.write("tab:control")
        except Exception:
            pass
        toggle_running()
        return

    if "--finance" in args:
        cmd_file = os.path.join(get_cache_dir(), "cmd")
        try:
            with open(cmd_file, "w", encoding="utf-8") as f:
                f.write("tab:finance")
        except Exception:
            pass
        toggle_running()
        return

    if "--localsend" in args or "--tab" in args:
        target_tab = "localsend"
        if "--tab" in args:
            idx = args.index("--tab")
            if idx + 1 < len(args) and not args[idx + 1].startswith("-"):
                target_tab = args[idx + 1]
        cmd_file = os.path.join(get_cache_dir(), "cmd")
        try:
            with open(cmd_file, "w", encoding="utf-8") as f:
                f.write(f"tab:{target_tab}")
        except Exception:
            pass
        toggle_running()
        return

    if "--send" in args:
        idx = args.index("--send")
        files = [os.path.abspath(f) for f in args[idx + 1:] if not f.startswith("-")]
        if files:
            cmd_file = os.path.join(get_cache_dir(), "cmd")
            try:
                with open(cmd_file, "w", encoding="utf-8") as f:
                    f.write("send:" + json.dumps(files))
            except Exception:
                pass
            toggle_running()
            return

    if "--lyrics" in args or "--toggle-lyrics" in args:
        cmd_file = os.path.join(get_cache_dir(), "cmd")
        try:
            with open(cmd_file, "w", encoding="utf-8") as f:
                f.write("toggle-lyrics")
        except Exception:
            pass
        toggle_running()
        return

    if "--toggle-panel-lyrics" in args:
        cmd_file = os.path.join(get_cache_dir(), "cmd")
        try:
            with open(cmd_file, "w", encoding="utf-8") as f:
                f.write("toggle-panel-lyrics")
        except Exception:
            pass
        toggle_running()
        return

    if "--eye-break" in args:
        cmd_file = os.path.join(get_cache_dir(), "cmd")
        try:
            with open(cmd_file, "w", encoding="utf-8") as f:
                f.write("health:eye_break")
        except Exception:
            pass
        toggle_running()
        return

    if "--stretch-break" in args:
        cmd_file = os.path.join(get_cache_dir(), "cmd")
        try:
            with open(cmd_file, "w", encoding="utf-8") as f:
                f.write("health:stretch_break")
        except Exception:
            pass
        toggle_running()
        return

    if "--eye" in args:
        idx = args.index("--eye")
        state = "angry"
        if idx + 1 < len(args) and not args[idx + 1].startswith("-"):
            state = args[idx + 1]
        cmd_file = os.path.join(get_cache_dir(), "cmd")
        try:
            with open(cmd_file, "w", encoding="utf-8") as f:
                f.write(f"eye:{state}")
        except Exception:
            pass
        toggle_running()
        return

    if "--test-adzan" in args:
        cmd_file = os.path.join(get_cache_dir(), "cmd")
        try:
            with open(cmd_file, "w", encoding="utf-8") as f:
                f.write("test-adzan")
        except Exception:
            pass
        toggle_running()
        return

    if "--shape" in args:
        idx = args.index("--shape")
        shape = "pill"
        if idx + 1 < len(args) and not args[idx + 1].startswith("-"):
            shape = args[idx + 1].lower()
        cmd_file = os.path.join(get_cache_dir(), "cmd")
        try:
            with open(cmd_file, "w", encoding="utf-8") as f:
                f.write(f"shape:{shape}")
        except Exception:
            pass
        toggle_running()
        return

    if "--settings" in args or "-s" in args:
        open_settings_cli()
        return

    if "--restart" in args:
        stop_running()
        time.sleep(0.5)

    is_foreground = "--foreground" in args or "-f" in args

    running_pid = get_running_pid()
    if running_pid:
        print(f"Pulo Notch sudah berjalan di background (PID {running_pid}).")
        print("Gunakan 'pulo --toggle' untuk membuka panel, atau 'pulo --stop' untuk menghentikan.")
        return

    if not is_foreground:
        daemonize()

    acquire_lock_or_exit()

    controller = DynamicIslandController()
    signal.signal(signal.SIGINT, lambda *a: GLib.idle_add(controller.quit))
    signal.signal(signal.SIGTERM, lambda *a: GLib.idle_add(controller.quit))

    if "--test-toast" in args:
        GLib.timeout_add(600, lambda: controller.send_test_notification(open_toast=True) or False)

    if "--test-prayer" in args:
        idx = args.index("--test-prayer")
        p_name = (
            args[idx + 1]
            if idx + 1 < len(args) and not args[idx + 1].startswith("-")
            else "Ashar"
        )

        def _sim_prayer():
            controller.prayer.simulated_alert = PrayerAlert(p_name, "15:00", 5, False)
            controller.on_hud_event(
                "prayer",
                0,
                False,
                extra_text=f"5 Menit Menuju Sholat {p_name} (15:00)",
            )
            controller.trigger_animation()
            controller.render_all()
            return False

        GLib.timeout_add(500, _sim_prayer)

    if "--test-bt" in args:
        idx = args.index("--test-bt")
        name = (
            args[idx + 1]
            if idx + 1 < len(args) and not args[idx + 1].startswith("-")
            else "EDIFIER X2 Plus"
        )
        GLib.timeout_add(500, lambda: controller.on_bluetooth_event(name, True) or False)

    if "--test-bt-disconnect" in args:
        idx = args.index("--test-bt-disconnect")
        name = (
            args[idx + 1]
            if idx + 1 < len(args) and not args[idx + 1].startswith("-")
            else "EDIFIER X2 Plus"
        )
        GLib.timeout_add(500, lambda: controller.on_bluetooth_event(name, False) or False)

    if "--test-hud" in args:
        idx = args.index("--test-hud")
        target = args[idx + 1] if idx + 1 < len(args) else "volume"
        if target == "mute":
            GLib.timeout_add(500, lambda: controller.on_hud_event("mute", 0, True) or False)
        elif target == "unmute":
            GLib.timeout_add(500, lambda: controller.on_hud_event("mute", 70, False) or False)
        elif target == "mic-off":
            GLib.timeout_add(500, lambda: controller.on_hud_event("mic", 0, True) or False)
        elif target == "mic-on":
            GLib.timeout_add(500, lambda: controller.on_hud_event("mic", 0, False) or False)
        elif target == "brightness":
            GLib.timeout_add(500, lambda: controller.on_hud_event("brightness", 85, False) or False)
        else:
            GLib.timeout_add(500, lambda: controller.on_hud_event("volume", 65, False) or False)

    try:
        Gtk.main()
    finally:
        cleanup_pid()


if __name__ == "__main__":
    main()
