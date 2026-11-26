#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""d455_studio.py - local web front end for record_d455.py.

    bash studio.sh                  macOS / Linux: starts the app and opens http://127.0.0.1:8455
    studio.bat                      Windows
    python d455_studio.py --simulate    no camera needed: a simulated D455

What it adds: a live view of color, depth and infrared, start/stop recording with the settings in the
browser, and playback of recorded sessions as synchronized video with a timeline.

What it does NOT re-implement: everything that touches the camera or the files comes from record_d455.py.
Stream planning, option handling, the macOS workarounds (sudo, IMU crash guard, USB hold, the start check
that restarts silent streams), encoding, the MCAP writer, the session layout and the verify report are the
recorder's own code. The camera stays open between recordings; a recording is a record_d455.Recorder that
receives the frames of the running streams.

Only the Python standard library is added (http.server), so nothing new has to be installed.
"""
from __future__ import annotations

import argparse
import dataclasses
import io
import json
import mimetypes
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from collections import OrderedDict, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import record_d455 as rd  # noqa: E402  (the recorder is the library)

__version__ = "1.0.0"
BUILT_AGAINST = "1.2.8"
WEB_ROOT = HERE / "studio"
SETTINGS_PATH = HERE / "studio_settings.json"
DEFAULT_PORT = 8455
IDLE_AFTER_S = 60.0  # no live view open and no recording for this long: stop streaming (projector off, no CPU)

LABELS = {"color": "彩色", "depth": "深度", "ir_left": "红外", "ir_right": "右红外"}
VIEW_ORDER = ("color", "depth", "ir_left", "ir_right")
REVERSE_DIR = {value: key for key, value in rd.STREAM_DIR.items()}  # "camera" -> "color"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "streams": ["depth", "color", "ir_left"],
    "depth_size": "848x480",
    "color_size": "1280x720",
    "fps": 30,
    "color_codec": "jpeg",
    "ir_codec": "jpeg",
    "jpeg_quality": 90,
    "split_minutes": 10,
    "duration": 0,
    "scene": {"location": "", "activity": "", "notes": ""},
    "options": {"depth": {}, "color": {}},
}
RESTART_KEYS = ("streams", "depth_size", "color_size", "fps")

# Camera options shown in the browser: (role, option name, label, unit).
CURATED_OPTIONS: Dict[str, List[Tuple[str, str, str]]] = {
    "depth": [
        ("visual_preset", "深度预设", ""),
        ("emitter_enabled", "红外投射器", ""),
        ("laser_power", "激光功率", "mW"),
        ("enable_auto_exposure", "自动曝光", ""),
        ("exposure", "曝光", "µs"),
        ("gain", "增益", ""),
    ],
    "color": [
        ("enable_auto_exposure", "自动曝光", ""),
        ("exposure", "曝光", ""),
        ("gain", "增益", ""),
        ("enable_auto_white_balance", "自动白平衡", ""),
        ("white_balance", "白平衡", "K"),
        ("brightness", "亮度", ""),
        ("power_line_frequency", "防频闪", ""),
    ],
}
# Manual values only make sense while the matching automatic mode is off.
AUTO_PARENT = {"exposure": "enable_auto_exposure", "gain": "enable_auto_exposure",
               "white_balance": "enable_auto_white_balance"}
APPLY_PRIORITY = {"visual_preset": 0, "enable_auto_exposure": 1, "enable_auto_white_balance": 1}
VALUE_NAMES = {
    "power_line_frequency": {0: "关闭", 1: "50 Hz", 2: "60 Hz", 3: "自动"},
    "emitter_enabled": {0: "关", 1: "开", 2: "自动"},
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_size(text: str) -> Tuple[int, int]:
    width, height = rd.parse_size(str(text))
    return int(width), int(height)


# --------------------------------------------------------------------------------------------
# Settings (persisted next to the recorder, owned by the user even when running under sudo)
# --------------------------------------------------------------------------------------------
class Settings:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.data: Dict[str, Any] = json.loads(json.dumps(DEFAULT_SETTINGS))
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            for key in DEFAULT_SETTINGS:
                if key in stored:
                    self.data[key] = stored[key]
        except Exception:
            pass
        self.data["options"] = {role: dict((self.data.get("options") or {}).get(role) or {})
                                for role in ("depth", "color")}

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.data))

    def save(self) -> None:
        with self.lock:
            text = json.dumps(self.data, ensure_ascii=False, indent=2)
        try:
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(text + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
            rd.hand_back_ownership(self.path, recursive=False)
        except OSError:
            pass

    def update(self, values: Dict[str, Any]) -> List[str]:
        """Validate and store; returns the keys that changed."""
        changed: List[str] = []
        with self.lock:
            for key, value in values.items():
                if key not in DEFAULT_SETTINGS or key == "options":
                    continue
                if key == "streams":
                    value = [name for name in VIEW_ORDER if name in set(value)]
                    if not value:
                        raise ValueError("至少要保留一路画面")
                elif key in ("depth_size", "color_size"):
                    value = "{}x{}".format(*parse_size(value))
                elif key in ("fps", "jpeg_quality"):
                    value = int(value)
                    if key == "jpeg_quality" and not 50 <= value <= 100:
                        raise ValueError("JPEG 质量应在 50 到 100 之间")
                elif key in ("split_minutes", "duration"):
                    value = max(0.0, float(value))
                elif key in ("color_codec", "ir_codec"):
                    if value not in ("jpeg", "png"):
                        raise ValueError("编码只能是 jpeg 或 png")
                elif key == "scene":
                    value = {name: str((value or {}).get(name, "")).strip()[:500]
                             for name in ("location", "activity", "notes")}
                if self.data.get(key) != value:
                    self.data[key] = value
                    changed.append(key)
        if changed:
            self.save()
        return changed

    def remember_option(self, role: str, name: str, value: float) -> None:
        with self.lock:
            options = self.data["options"].setdefault(role, {})
            options[name] = value
            if name in AUTO_PARENT:
                options[AUTO_PARENT[name]] = 0.0
            if name in AUTO_PARENT.values() and value:
                for child, parent in AUTO_PARENT.items():
                    if parent == name:
                        options.pop(child, None)
        self.save()

    def forget_options(self, role: str) -> None:
        with self.lock:
            self.data["options"][role] = {}
        self.save()

    def overrides(self) -> Tuple[str, ...]:
        """Remembered options in the form record_d455 --set takes, in a safe order."""
        with self.lock:
            items = [(role, name, value) for role, values in self.data["options"].items()
                     for name, value in values.items()]
        items.sort(key=lambda item: APPLY_PRIORITY.get(item[1], 2))
        return tuple("{}.{}={}".format(role, name, float(value)) for role, name, value in items)


# --------------------------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------------------------
class DepthPalette:
    """Raw Z16 value -> BGR lookup table, so a frame is colorized with one gather."""

    COLORMAPS = {"turbo": "COLORMAP_TURBO", "jet": "COLORMAP_JET", "viridis": "COLORMAP_VIRIDIS",
                 "inferno": "COLORMAP_INFERNO"}

    def __init__(self) -> None:
        self.cache: "OrderedDict[Tuple, Any]" = OrderedDict()
        self.lock = threading.Lock()

    def lut(self, scale: float, near: float, far: float, cmap: str) -> Any:
        import cv2
        import numpy as np

        key = (round(scale, 9), round(near, 3), round(far, 3), cmap)
        with self.lock:
            table = self.cache.get(key)
            if table is not None:
                self.cache.move_to_end(key)
                return table
        metres = np.arange(65536, dtype=np.float32) * float(scale)
        span = max(far - near, 1e-3)
        t = np.clip((metres - near) / span, 0.0, 1.0)
        if cmap == "gray":
            level = (255 - np.round(t * 235)).astype(np.uint8)  # near = bright
            table = np.stack([level, level, level], axis=-1)
        else:
            index = np.round(t * 255).astype(np.uint8).reshape(-1, 1)
            code = getattr(cv2, self.COLORMAPS.get(cmap, "COLORMAP_TURBO"), cv2.COLORMAP_JET)
            table = cv2.applyColorMap(index, code).reshape(-1, 3)
        table = np.ascontiguousarray(table)
        table[0] = (16, 14, 13)  # no measurement
        with self.lock:
            self.cache[key] = table
            while len(self.cache) > 12:
                self.cache.popitem(last=False)
        return table

    def colorize(self, raw: Any, scale: float, near: float, far: float, cmap: str) -> Any:
        return self.lut(scale, near, far, cmap)[raw]


PALETTE = DepthPalette()


def depth_params(query: Dict[str, str]) -> Tuple[float, float, str]:
    try:
        near = float(query.get("near", "0.2"))
        far = float(query.get("far", "6"))
    except ValueError:
        near, far = 0.2, 6.0
    near = min(max(near, 0.0), 60.0)
    far = min(max(far, near + 0.05), 65.0)
    cmap = query.get("cmap", "turbo")
    if cmap not in DepthPalette.COLORMAPS and cmap != "gray":
        cmap = "turbo"
    return near, far, cmap


def encode_jpeg(image: Any, quality: int = 85) -> bytes:
    import cv2
    import numpy as np

    ok, data = cv2.imencode(".jpg", np.ascontiguousarray(image), [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return data.tobytes()


def sample_depth(raw: Any, x: float, y: float) -> Dict[str, Any]:
    height, width = raw.shape[:2]
    column = min(max(int(x * width), 0), width - 1)
    row = min(max(int(y * height), 0), height - 1)
    return {"raw": int(raw[row, column]), "px": [column, row], "size": [width, height]}


# --------------------------------------------------------------------------------------------
# Live camera: streams continuously, recordings attach to the running streams
# --------------------------------------------------------------------------------------------
class StreamHost(rd.Recorder):
    """The recorder's stream planning, option handling and sensor start/stop, with every frame handed to
    the studio's dispatcher. It never writes files."""

    def __init__(self, studio: "LiveCamera", device: Any, config: rd.RecordConfig,
                 is_present: Optional[Callable[[], bool]] = None) -> None:
        super().__init__(studio.rs, device, config, studio.shutdown, is_present=is_present)
        self.studio = studio

    def _on_frame(self, frame: Any) -> None:
        self.studio.dispatch(frame)

    def note(self, level: str, message: str) -> None:
        super().note(level, message)
        self.studio.event(level, message, source="camera")


class StudioRecorder(rd.Recorder):
    """One recording: the unchanged record_d455 writing path (encoders, MCAP sink, frame index, manifest,
    verify report), fed by the streams the live view already runs. It does not open or stop sensors."""

    def __init__(self, studio: "LiveCamera", host: StreamHost, config: rd.RecordConfig,
                 stop_event: threading.Event) -> None:
        super().__init__(studio.rs, host.device, config, stop_event, is_present=host.is_present)
        self.studio = studio
        self.host = host
        self.stop_requested = False

    def plan_streams(self) -> None:
        host, config = self.host, self.config
        self.sensors = host.sensors
        self.sensor_roles = dict(host.sensor_roles)
        self.sensor_profiles = host.sensor_profiles
        self.plans = []
        for source in host.plans:
            plan = dataclasses.replace(source)
            if plan.kind == "video":  # codec settings may have changed since the streams started
                plan.encoding = {"color": config.color_codec, "ir_left": config.ir_codec,
                                 "ir_right": config.ir_codec}.get(plan.name, "png")
                plan.topic = rd.image_topic(self.device_id, plan.name, plan.encoding)
                plan.info_topic = rd.info_topic(self.device_id, plan.name)
            self.plans.append(plan)
        self.by_key = {plan.key: plan for plan in self.plans}
        self.stats = {plan.name: rd.StreamStats() for plan in self.plans}
        self.anchor = host.anchor
        self.depth_units = host.depth_units or self.studio.depth_units
        self._metadata_values = host._metadata_values
        self._motion_metadata_values = host._motion_metadata_values

    def apply_options(self) -> None:
        for item in self.host.events:  # what the live view set when it opened the camera
            self.events.append(dict(item, message=item["message"] + " [live view start]"))

    def run(self) -> Dict[str, Any]:
        """record_d455.Recorder.run without its StartWatchdog: this start opens no sensors (the live view
        already streams them), and the live view's own start uses the process's only faulthandler timer."""
        reason = "user"
        try:
            self.start()
            reason = self._loop()
        except Exception as error:
            reason = "error: {}".format(error)
            self.note("error", "ERROR: {}".format(error))
        return self.finalize(reason)

    def start_streams(self, after_start: Optional[Callable[[], None]] = None) -> None:
        """The sensors already stream: only check that every video stream reaches this recorder."""
        self._first_frames = set()
        try:
            silent = self._wait_for_first_frames(rd.START_CHECK_S)
        finally:
            self._first_frames = None
        if silent and not self.stop_event.is_set():
            raise RuntimeError("no live frames from {} within {:.0f} s".format(", ".join(silent), rd.START_CHECK_S))

    def _start_sensors(self) -> None:
        pass  # already streaming

    def _stop_device(self, timeout: float = 5.0) -> bool:
        return True  # the live view keeps streaming after the recording

    def _render_preview(self) -> None:
        pass

    def note(self, level: str, message: str) -> None:
        super().note(level, message)
        self.studio.event(level, message, source="recorder")


class LiveFrame:
    __slots__ = ("seq", "pixels", "number", "timestamp_ms", "count", "last_monotonic", "rate", "mark_count",
                 "mark_time", "encoded")

    def __init__(self) -> None:
        self.seq = 0
        self.pixels: Any = None
        self.number = 0
        self.timestamp_ms = 0.0
        self.count = 0
        self.last_monotonic = 0.0
        self.rate = 0.0
        self.mark_count = 0
        self.mark_time = time.monotonic()
        self.encoded: Optional[Tuple[Tuple, bytes]] = None


class LiveCamera:
    """Owns the camera. States: starting, waiting, opening, streaming, idle, error, stopped."""

    def __init__(self, rs: Any, settings: Settings, out_root: Path, serial: str = "", simulate: bool = False,
                 log_dir: Optional[Path] = None, idle_after: float = IDLE_AFTER_S) -> None:
        self.rs = rs
        self.idle_after = idle_after if idle_after > 0 else float("inf")
        self.service = False  # started by launchd (install_service.sh): the page may restart the process
        self.log_dir = log_dir or HERE / "logs"
        self.settings = settings
        self.out_root = out_root
        self.serial = serial
        self.simulate = simulate
        self.shutdown = threading.Event()
        self.restart_request = threading.Event()
        self.reset_request = threading.Event()
        self.lock = threading.RLock()
        self.cond = threading.Condition()
        self.state = "starting"
        self.message = "正在启动"
        self.device_info: Dict[str, str] = {}
        self.host: Optional[StreamHost] = None
        self.recorder: Optional[StudioRecorder] = None
        self.record_thread: Optional[threading.Thread] = None
        self.record_phase = ""
        self.record_started = 0.0
        self.last_result: Optional[Dict[str, Any]] = None
        self.live: Dict[str, LiveFrame] = {}
        self.depth_units: Optional[float] = None
        self.frame_seq = 0
        self.last_viewer = 0.0
        self.last_client = time.monotonic()  # a live view (or the settings panel) asked for the camera
        self.wake = threading.Event()
        self.context = None
        self.events: deque = deque(maxlen=200)
        self.event_id = 0
        self.thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._write_mark: Tuple[float, int] = (time.monotonic(), 0)
        self._write_rate = 0.0
        self.library: Optional["Library"] = None

    # ---- events ---------------------------------------------------------------------------
    def event(self, level: str, message: str, source: str = "studio") -> None:
        with self.lock:
            self.event_id += 1
            self.events.append({"id": self.event_id, "time": now_iso(), "level": level, "message": message,
                                "source": source})
        if source == "studio":
            rd.log("[studio] " + message)

    def _set_state(self, state: str, message: str = "") -> None:
        with self.lock:
            changed = state != self.state or message != self.message
            self.state, self.message = state, message
        if changed and message:
            rd.log("[studio] {}: {}".format(state, message))

    def touch(self) -> None:
        """Someone wants the camera (live page visible, settings open): keep streaming, or wake up."""
        self.last_client = time.monotonic()
        if self.state == "idle":
            self.wake.set()

    def _sleep_until_wanted(self, is_present: Optional[Callable[[], bool]]) -> bool:
        """Camera closed until a client asks for it. False if the camera was unplugged meanwhile."""
        self._set_state("idle", "相机待机中（打开「实时」页会自动唤醒）")
        self.wake.clear()
        checked = time.monotonic()
        while not self.shutdown.is_set():
            if self.wake.wait(2.0) or time.monotonic() - self.last_client < self.idle_after:
                self.restart_request.clear()
                return True
            if is_present is not None and time.monotonic() - checked > 5.0:
                checked = time.monotonic()
                if not is_present():
                    self.event("warning", "相机已断开，等待重新接入")
                    return False
        return True

    # ---- frames (SDK threads) -------------------------------------------------------------
    def dispatch(self, frame: Any) -> None:
        recorder = self.recorder
        if recorder is not None:
            recorder._on_frame(frame)  # record_d455's capture callback, unchanged
        host = self.host
        if host is None:
            return
        host._mark_first_frame(frame)  # for StreamHost.start_streams (a no-op once streams are up)
        try:
            profile = frame.get_profile()
            plan = host.by_key.get((int(profile.stream_type()), int(profile.stream_index())))
            if plan is None:
                return
            now = time.monotonic()
            live = self.live.get(plan.name)
            if live is None:
                return
            live.count += 1
            live.last_monotonic = now
            if plan.kind != "video":
                return
            if plan.name == "depth" and self.depth_units is None:
                try:
                    self.depth_units = float(frame.as_depth_frame().get_units()) or None
                except Exception:
                    pass
            if now - self.last_viewer > 4.0:
                return  # nobody is watching: no copies
            import numpy as np
            pixels = np.asanyarray(frame.get_data()).copy()
            with self.cond:
                live.pixels = pixels
                live.number = int(frame.get_frame_number())
                live.timestamp_ms = float(frame.get_timestamp())
                self.frame_seq += 1  # one counter for all streams and restarts: a client's ?after never goes stale
                live.seq = self.frame_seq
                self.cond.notify_all()
        except Exception:
            pass

    # ---- camera thread --------------------------------------------------------------------
    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        while not self.shutdown.is_set():
            try:
                if self.simulate:
                    self._stream_loop(None, None)
                else:
                    self._find_and_stream()
            except Exception as error:
                traceback.print_exc()
                self._set_state("error", "相机出错：{}".format(error))
                self.event("error", "相机出错：{}".format(error))
                self.shutdown.wait(3.0)
        self._set_state("stopped", "已退出")

    def _find_and_stream(self) -> None:
        rs = self.rs
        self._set_state("waiting", "等待相机接入")
        if not rd.wait_for_macos_usb(self.shutdown):
            return
        rd.take_macos_camera(self.shutdown)
        if self.context is None:
            self.context = rs.context()
        found = rd.wait_for_device(rs, self.context, self.serial, self.shutdown)
        if not found:
            return
        device, name, serial = found
        rd.describe_device(rs, device, name, serial)
        context = self.context
        self._stream_loop(device, lambda: rd.find_device(rs, context, serial, quiet=True) is not None)

    def _config(self) -> rd.RecordConfig:
        values = self.settings.snapshot()
        return rd.RecordConfig(
            out_root=self.out_root, streams=tuple(values["streams"]), depth_size=parse_size(values["depth_size"]),
            color_size=parse_size(values["color_size"]), fps=int(values["fps"]), jpeg_quality=int(values["jpeg_quality"]),
            color_codec=values["color_codec"], ir_codec=values["ir_codec"], split_minutes=float(values["split_minutes"]),
            option_overrides=self.settings.overrides(), quiet=True, preview=False)

    def _make_simulator(self, fps: int) -> Any:
        simulator = rd.make_simulator(self.rs, "python", fps=fps, imu=False)
        rs = self.rs
        ranges = {  # extra options so the settings panel can be tried without a camera
            "depth": {"visual_preset": (0, 5, 1, 0), "laser_power": (0, 360, 30, 150), "enable_auto_exposure": (0, 1, 1, 1),
                      "exposure": (1, 200000, 1, 8500), "gain": (16, 248, 1, 16), "emitter_enabled": (0, 1, 1, 1)},
            "color": {"enable_auto_exposure": (0, 1, 1, 1), "exposure": (1, 10000, 1, 156), "gain": (0, 128, 1, 64),
                      "enable_auto_white_balance": (0, 1, 1, 1), "white_balance": (2800, 6500, 10, 4600),
                      "brightness": (-64, 64, 1, 0), "power_line_frequency": (0, 3, 1, 3)},
        }
        presets = {0: "Custom", 1: "Default", 2: "Hand", 3: "High Accuracy", 4: "High Density", 5: "Medium Density"}
        for sensor, role in ((simulator.depth_sensor, "depth"), (simulator.color_sensor, "color")):
            table = {}
            for name, (low, high, step, default) in ranges[role].items():
                option = getattr(rs.option, name, None)
                if option is None:
                    continue
                sensor.options.setdefault(int(option), float(default))
                table[int(option)] = (low, high, step, default)

            def get_range(option: Any, table: Dict[int, Tuple] = table) -> Any:
                low, high, step, default = table[int(option)]
                return type("Range", (), {"min": low, "max": high, "step": step, "default": default})()

            def describe(option: Any, value: float) -> str:
                return presets.get(int(value), str(value)) if int(option) == int(rs.option.visual_preset) else ""

            sensor.get_option_range = get_range
            sensor.get_option_value_description = describe
        return simulator

    def _stream_loop(self, device: Any, is_present: Optional[Callable[[], bool]]) -> None:
        failures = 0
        while not self.shutdown.is_set():
            config = self._config()
            simulator = None
            if self.simulate:
                simulator = self._make_simulator(config.fps)
                device = simulator.device
            host = StreamHost(self, device, config, is_present=is_present)
            self._set_state("opening", "正在打开相机")
            try:
                self._open(host, simulator.start if simulator else None)
            except Exception as error:
                self.host = None
                host._stop_device()
                if simulator:
                    simulator.stop()
                failures += 1
                self.event("warning", "相机没有正常出图（{}），第 {} 次重试".format(error, failures))
                if self.simulate:
                    self.shutdown.wait(2.0)
                    continue
                if failures >= 2:
                    rd.reset_device(device)
                    self._set_state("waiting", "相机复位中，几秒后重新连接")
                    self.shutdown.wait(6.0)
                    return  # find the camera again after it re-enumerates
                self.shutdown.wait(2.0)
                continue
            failures = 0
            self.device_info = dict(host.info)
            if self.simulate:
                self.device_info.setdefault("name", "Simulated RealSense D455")
            self._set_state("streaming", "")
            reason = self._watch(host)
            self._finish_recording(wait=True)
            self.host = None
            host._stop_device()
            if simulator:
                simulator.stop()
            with self.cond:
                for live in self.live.values():
                    live.pixels = None
                self.cond.notify_all()
            if reason == "disconnected":
                self.event("warning", "相机已断开，等待重新接入")
                return
            if reason == "idle":
                if not self._sleep_until_wanted(is_present):
                    return
                continue
            if reason == "stalled":
                self.event("warning", "画面停了 5 秒，正在重新打开相机")
                failures += 1
                if failures >= 2 and not self.simulate:
                    rd.reset_device(device)
                    self.shutdown.wait(6.0)
                    return
            if reason == "restart":
                self._set_state("opening", "正在按新设置重新打开相机")
            if reason == "reset":
                if self.simulate:
                    continue
                self._set_state("waiting", "相机复位中，几秒后重新连接")
                rd.reset_device(device)  # the camera leaves the USB bus and comes back, like a replug
                self.shutdown.wait(6.0)
                return

    def _open(self, host: StreamHost, after_start: Optional[Callable[[], None]]) -> None:
        """record_d455's start: plan, apply options, start the sensors and wait for a first frame of every
        video stream (restarting them if one stays silent). The start watchdog ends the process if an SDK call
        never returns, because nothing in this process can recover from that; launchd then starts the
        service again."""
        host.plan_streams()
        host.apply_options()
        with self.cond:
            self.live = {plan.name: LiveFrame() for plan in host.plans}
        self.host = host
        watchdog = None if self.simulate else rd.StartWatchdog(self.log_dir)
        try:
            host.start_streams(after_start)
        finally:
            if watchdog is not None:
                watchdog.cancel()

    def _watch(self, host: StreamHost) -> str:
        started = time.monotonic()
        while not self.shutdown.is_set():
            self.shutdown.wait(0.25)
            now = time.monotonic()
            for live in list(self.live.values()):
                span = now - live.mark_time
                if span >= 1.0:
                    live.rate = (live.count - live.mark_count) / span
                    live.mark_count, live.mark_time = live.count, now
            recorder = self.recorder
            if recorder is not None:
                continue  # the recorder runs its own stall and disconnect checks
            if self.restart_request.is_set():
                self.restart_request.clear()
                return "restart"
            if self.reset_request.is_set():
                self.reset_request.clear()
                return "reset"
            if now - self.last_client > self.idle_after:
                return "idle"
            video = [self.live[plan.name] for plan in host.plans if plan.kind == "video" and plan.name in self.live]
            newest = max((live.last_monotonic for live in video), default=0.0)
            quiet = now - (newest or started)
            if quiet > 5.0:
                return "stalled" if host._device_connected() else "disconnected"
        return "shutdown"

    # ---- recording --------------------------------------------------------------------------
    def start_recording(self, scene: Dict[str, str], duration: float) -> Dict[str, Any]:
        with self.lock:
            if self.recorder is not None:
                raise RuntimeError("已经在录制")
            host = self.host
            if self.state != "streaming" or host is None:
                raise RuntimeError("相机还没有出图，不能开始录制")
            values = self.settings.snapshot()
            config = rd.RecordConfig(
                out_root=self.out_root, streams=tuple(values["streams"]), depth_size=host.config.depth_size,
                color_size=host.config.color_size, fps=host.config.fps, jpeg_quality=int(values["jpeg_quality"]),
                color_codec=values["color_codec"], ir_codec=values["ir_codec"],
                split_minutes=float(values["split_minutes"]), duration=max(0.0, float(duration or 0)),
                scene={key: value for key, value in scene.items() if value}, quiet=True, preview=False)
            self.out_root.mkdir(parents=True, exist_ok=True)
            rd.hand_back_ownership(self.out_root, recursive=False)
            recorder = StudioRecorder(self, host, config, threading.Event())
            self.recorder = recorder
            self.record_phase = "starting"
            self.record_started = time.monotonic()
            self.last_result = None
            self._write_mark = (time.monotonic(), 0)
            self._write_rate = 0.0
            self.record_thread = threading.Thread(target=self._record, args=(recorder,), name="recording", daemon=True)
            self.record_thread.start()
        self.event("info", "开始录制 {}".format(recorder.session_id))
        return {"session_id": recorder.session_id}

    def _record(self, recorder: StudioRecorder) -> None:
        release = rd.keep_awake()
        report: Dict[str, Any] = {}
        try:
            report = recorder.run()
        except Exception as error:
            traceback.print_exc()
            report = {"ok": False, "stop_reason": "error: {}".format(error), "empty": True,
                      "session_dir": str(recorder.session_dir)}
        finally:
            release()
        with self.lock:
            self.recorder = None
            self.record_phase = ""
        session_dir = Path(report.get("session_dir") or recorder.session_dir)
        result = {"session_id": session_dir.name, "ok": bool(report.get("ok")), "empty": bool(report.get("empty")),
                  "stop_reason": report.get("stop_reason"), "result": report.get("result"),
                  "problems": report.get("problems", []), "warnings": report.get("warnings", []),
                  "duration_s": None, "bytes": None, "time": now_iso()}
        if not result["empty"]:
            try:
                manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
                result["duration_s"] = manifest.get("duration_s")
                result["bytes"] = manifest.get("bytes_written")
            except Exception:
                pass
            self.event("info" if result["result"] == "PASS" else "warning", "已保存 {}（体检 {}）".format(
                session_dir.name, result["result"] or "?"))
        else:
            self.event("error", "这次没有录到数据：{}".format(report.get("stop_reason")))
        self.last_result = result
        if self.library is not None:
            self.library.forget(session_dir.name)
        rd.print_report(report) if report.get("streams") else None

    def stop_recording(self) -> None:
        with self.lock:
            recorder = self.recorder
            if recorder is None:
                return
            recorder.stop_requested = True
            self.record_phase = "saving"
            recorder.stop_event.set()

    def _finish_recording(self, wait: bool) -> None:
        self.stop_recording()
        thread = self.record_thread
        if wait and thread is not None and thread.is_alive():
            thread.join(600)

    def request_reset(self) -> None:
        if self.recorder is not None:
            raise RuntimeError("录制中不能复位相机，先停止录制")
        if self.state != "streaming":
            raise RuntimeError("相机没有在出图，现在不能复位")
        self.event("info", "正在给相机发硬件复位，几秒后自动重新连接")
        self.reset_request.set()

    def request_restart(self) -> None:
        if self.recorder is not None:
            raise RuntimeError("录制中不能改视频流设置，先停止录制")
        self.restart_request.set()

    # ---- options ----------------------------------------------------------------------------
    def _sensor(self, role: str) -> Any:
        host = self.host
        if host is None or self.state != "streaming":
            raise RuntimeError("相机还没有出图")
        sensor = host.sensor_roles.get(role)
        if sensor is None:
            raise KeyError("没有 {} 传感器".format(role))
        return sensor

    def options(self) -> Dict[str, Any]:
        rs = self.rs
        result: Dict[str, Any] = {}
        for role, items in CURATED_OPTIONS.items():
            try:
                sensor = self._sensor(role)
            except Exception:
                continue
            entries = []
            for name, label, unit in items:
                option = getattr(rs.option, name, None)
                if option is None:
                    continue
                try:
                    if not sensor.supports(option):
                        continue
                    value = float(sensor.get_option(option))
                except Exception:
                    continue
                entry: Dict[str, Any] = {"name": name, "label": label, "unit": unit, "value": value}
                try:
                    span = sensor.get_option_range(option)
                    entry.update({"min": float(span.min), "max": float(span.max), "step": float(span.step) or 1.0,
                                  "default": float(span.default)})
                except Exception:
                    entry.update({"min": 0.0, "max": 1.0, "step": 1.0, "default": value})
                if entry["min"] == 0 and entry["max"] == 1 and entry["step"] == 1:
                    entry["kind"] = "toggle"
                elif entry["max"] - entry["min"] <= 8 and entry["step"] == 1:
                    entry["kind"] = "select"
                    choices = []
                    for number in range(int(entry["min"]), int(entry["max"]) + 1):
                        text = VALUE_NAMES.get(name, {}).get(number, "")
                        if not text:
                            try:
                                text = str(sensor.get_option_value_description(option, float(number)) or "")
                            except Exception:
                                text = ""
                        choices.append({"value": number, "label": text or str(number)})
                    entry["choices"] = choices
                else:
                    entry["kind"] = "slider"
                parent = AUTO_PARENT.get(name)
                if parent:
                    entry["auto_parent"] = parent
                entries.append(entry)
            result[role] = entries
        return result

    def set_option(self, role: str, name: str, value: float) -> None:
        if role not in CURATED_OPTIONS or name not in {item[0] for item in CURATED_OPTIONS[role]}:
            raise KeyError("不支持的参数 {}.{}".format(role, name))
        sensor = self._sensor(role)
        rs = self.rs
        parent = AUTO_PARENT.get(name)
        if parent is not None:  # a manual value turns the automatic mode off first
            parent_option = getattr(rs.option, parent, None)
            try:
                if parent_option is not None and sensor.supports(parent_option) and sensor.get_option(parent_option):
                    sensor.set_option(parent_option, 0.0)
            except Exception:
                pass
        option = getattr(rs.option, name)
        sensor.set_option(option, float(value))
        self.settings.remember_option(role, name, float(value))
        message = "set {}.{} = {} (studio)".format(role, name, value)
        recorder = self.recorder
        if recorder is not None:
            recorder.events.append({"time": rd.local_iso(), "level": "info", "message": message + " during recording"})
        host = self.host
        if host is not None:
            host.events.append({"time": rd.local_iso(), "level": "info", "message": message})

    def reset_options(self, role: str) -> None:
        sensor = self._sensor(role)
        rs = self.rs
        for name, _label, _unit in sorted(CURATED_OPTIONS.get(role, []), key=lambda item: APPLY_PRIORITY.get(item[0], 2)):
            if name in AUTO_PARENT:
                continue
            option = getattr(rs.option, name, None)
            try:
                if option is not None and sensor.supports(option):
                    sensor.set_option(option, float(sensor.get_option_range(option).default))
            except Exception:
                pass
        self.settings.forget_options(role)

    # ---- profiles ---------------------------------------------------------------------------
    def profiles(self) -> Dict[str, Any]:
        host = self.host
        result: Dict[str, Dict[str, List[int]]] = {"depth": {}, "color": {}}
        if host is None:
            return result
        rs = self.rs
        for profiles in host.sensor_profiles:
            for profile in profiles:
                try:
                    stream = profile.stream_type()
                    fmt = profile.format().name
                    video = profile.as_video_stream_profile()
                    size = "{}x{}".format(video.width(), video.height())
                    fps = int(profile.fps())
                except Exception:
                    continue
                if stream == rs.stream.depth and fmt == "z16":
                    result["depth"].setdefault(size, []).append(fps)
                elif stream == rs.stream.color and fmt in ("bgr8", "rgb8", "yuyv"):
                    result["color"].setdefault(size, []).append(fps)
        for kind in result:
            result[kind] = {size: sorted(set(rates)) for size, rates in sorted(
                result[kind].items(), key=lambda item: -int(item[0].split("x")[0]) * int(item[0].split("x")[1]))}
        return result

    # ---- live images --------------------------------------------------------------------------
    def live_image(self, stream: str, after: int, query: Dict[str, str], timeout: float = 2.0) -> Optional[Tuple[bytes, Dict[str, str]]]:
        import cv2

        self.last_viewer = time.monotonic()
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                live = self.live.get(stream)
                if live is not None and live.pixels is not None and live.seq > after:
                    pixels, seq, number, stamp = live.pixels, live.seq, live.number, live.timestamp_ms
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.shutdown.is_set():
                    return None
                self.cond.wait(remaining)
        host = self.host
        plan = None if host is None else next((item for item in host.plans if item.name == stream), None)
        if stream == "depth":
            near, far, cmap = depth_params(query)
            key: Tuple = (seq, near, far, cmap)
        else:
            width = int(query.get("w", "0") or 0)
            key = (seq, width)
        cached = live.encoded
        if cached is not None and cached[0] == key:
            data = cached[1]
        else:
            if stream == "depth":
                image = PALETTE.colorize(pixels, self.depth_units or 0.001, near, far, cmap)
                data = encode_jpeg(image, 88)
            elif stream == "color":
                image = host._to_bgr(pixels, plan.fmt) if (host is not None and plan is not None) else pixels
                if 0 < width < image.shape[1]:
                    image = cv2.resize(image, (width, int(round(image.shape[0] * width / image.shape[1]))),
                                       interpolation=cv2.INTER_AREA)
                data = encode_jpeg(image, 82)
            else:
                data = encode_jpeg(pixels, 88)
            live.encoded = (key, data)
        return data, {"X-Seq": str(seq), "X-Frame": str(number), "X-Timestamp": "{:.3f}".format(stamp)}

    def live_depth_at(self, x: float, y: float) -> Dict[str, Any]:
        self.last_viewer = time.monotonic()
        live = self.live.get("depth")
        if live is None or live.pixels is None:
            raise RuntimeError("还没有深度画面")
        result = sample_depth(live.pixels, x, y)
        result["m"] = result["raw"] * (self.depth_units or 0.001)
        return result

    # ---- state ------------------------------------------------------------------------------
    def snapshot(self, since_event: int = 0) -> Dict[str, Any]:
        host = self.host
        streams = []
        if host is not None:
            for plan in sorted(host.plans, key=lambda item: VIEW_ORDER.index(item.name) if item.name in VIEW_ORDER else 9):
                live = self.live.get(plan.name)
                streams.append({"name": plan.name, "label": LABELS.get(plan.name, plan.name), "kind": plan.kind,
                                "width": plan.width, "height": plan.height, "fps": plan.fps, "format": plan.fmt,
                                "exact": plan.exact, "rate": round(live.rate, 1) if live else 0.0})
        info = self.device_info
        camera = {
            "state": self.state, "message": self.message, "simulated": self.simulate, "service": self.service,
            "name": info.get("name", ""), "serial": info.get("serial_number", ""),
            "firmware": info.get("firmware_version", ""), "usb": info.get("usb_type_descriptor", ""),
            "recommended_firmware": info.get("recommended_firmware_version", ""),
            "depth_scale": self.depth_units,
        }
        if sys.platform == "darwin" and not self.simulate:
            camera["macos"] = {"hid_guard": rd.describe_hid_guard(), "usb_hold": rd.USB_HOLD.describe()}
        try:
            self.out_root.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(str(self.out_root)).free
        except OSError:
            free = None
        recording = None
        recorder = self.recorder
        if recorder is not None:
            now = time.monotonic()
            accepting = recorder._accepting
            elapsed = now - recorder._started_monotonic if accepting and recorder._started_monotonic else 0.0
            written = recorder._sink.bytes_written if recorder._sink is not None else 0
            mark_time, mark_bytes = self._write_mark
            if now - mark_time >= 1.0:
                self._write_rate = (written - mark_bytes) / (now - mark_time)
                self._write_mark = (now, written)
            gaps = sum(stats.device_gaps for stats in recorder.stats.values())
            drops = sum(stats.recorder_drops + stats.encode_errors for stats in recorder.stats.values())
            phase = self.record_phase
            if phase == "starting" and accepting:
                phase = self.record_phase = "recording"
            delta = recorder._live_delta() if accepting else None
            recording = {
                "session_id": recorder.session_id, "phase": phase, "elapsed": round(elapsed, 2),
                "duration": recorder.config.duration, "bytes": written, "rate_bps": round(self._write_rate),
                "frames": dict(recorder._written_frames), "device_gaps": gaps, "recorder_drops": drops,
                "color_depth_ms": None if delta is None else round(delta, 2),
                "buffer_mb": round(recorder._buffered / 1e6, 1),
                "remaining_s": (free / self._write_rate) if (free and self._write_rate > 1e5) else None,
                "scene": recorder.config.scene,
            }
        with self.lock:
            events = [item for item in self.events if item["id"] > since_event]
        return {"version": __version__, "recorder_version": rd.__version__, "camera": camera, "streams": streams,
                "recording": recording, "last_result": self.last_result, "events": events[-50:],
                "event_id": self.event_id, "settings": self.settings.snapshot(),
                "disk": {"free": free, "path": str(self.out_root.resolve())}}

    def stop(self) -> None:
        self._finish_recording(wait=True)
        self.shutdown.set()
        with self.cond:
            self.cond.notify_all()


# --------------------------------------------------------------------------------------------
# Reading sessions: an offset index into the MCAP files for random access by frame
# --------------------------------------------------------------------------------------------
MCAP_MAGIC = b"\x89MCAP0\r\n"
OP_SCHEMA, OP_CHANNEL, OP_MESSAGE, OP_CHUNK, OP_MESSAGE_INDEX = 0x03, 0x04, 0x05, 0x06, 0x07


def _read_string(buffer: bytes, position: int) -> Tuple[str, int]:
    (length,) = struct.unpack_from("<I", buffer, position)
    position += 4
    return buffer[position:position + length].decode("utf-8", "replace"), position + length


def _classify(topic: str, schema: str) -> Optional[str]:
    """Image topic -> stream name; FrameInfo topic -> 'name#info'."""
    parts = topic.strip("/").split("/")
    if len(parts) != 3 or parts[1] not in REVERSE_DIR:
        return None
    name = REVERSE_DIR[parts[1]]
    if schema == "foxglove.CompressedImage":
        return name
    if schema == "realsense.FrameInfo":
        return name + "#info"
    return None


def _decompress(compression: str, data: bytes, size: int) -> bytes:
    if compression == "zstd":
        import zstandard
        return zstandard.ZstdDecompressor().decompress(data, max_output_size=size)
    if compression == "lz4":
        import lz4.frame
        return lz4.frame.decompress(data)
    raise ValueError("unsupported MCAP chunk compression '{}'".format(compression))


class McapFile:
    """Where every image / frame-info message of one MCAP file is. Finalized files are indexed from their
    message index records (no payload is read); files that were never finalized are scanned record by record
    up to the last complete chunk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.size = path.stat().st_size
        # stream -> list of (log_time_ns, ref); ref = absolute message record offset, or (chunk offset, inner offset)
        self.entries: Dict[str, List[Tuple[int, Any]]] = {}
        self.chunks: Dict[int, Tuple[str, int, int, int]] = {}  # compressed chunks: offset -> (compression, start, length, size)
        self.indexed_from = ""
        with path.open("rb") as handle:
            if handle.read(8) != MCAP_MAGIC:
                raise ValueError("{} is not an MCAP file".format(path.name))
            if not self._from_summary(handle):
                self._scan(handle)

    def _add(self, name: Optional[str], log_time: int, ref: Any) -> None:
        if name:
            self.entries.setdefault(name, []).append((log_time, ref))

    def _from_summary(self, handle: Any) -> bool:
        try:
            from mcap.reader import make_reader
            handle.seek(0)
            summary = make_reader(handle).get_summary()
        except Exception:
            return False
        if summary is None or not summary.chunk_indexes:
            return False
        wanted = {}
        for channel_id, channel in summary.channels.items():
            schema = summary.schemas.get(channel.schema_id)
            name = _classify(channel.topic, schema.name if schema else "")
            if name:
                wanted[channel_id] = name
        for chunk in summary.chunk_indexes:
            compression = chunk.compression or ""
            records_start = chunk.chunk_start_offset + 1 + 8 + 8 + 8 + 8 + 4 + 4 + len(compression.encode()) + 8
            if compression:
                self.chunks[chunk.chunk_start_offset] = (compression, records_start,
                                                         chunk.compressed_size, chunk.uncompressed_size)
            for channel_id, offset in chunk.message_index_offsets.items():
                name = wanted.get(channel_id)
                if not name:
                    continue
                handle.seek(offset)
                head = handle.read(15)
                opcode, _length, _channel, count = struct.unpack("<BQHI", head)
                if opcode != OP_MESSAGE_INDEX:
                    return False
                body = handle.read(count)
                for log_time, inner in struct.iter_unpack("<QQ", body):
                    ref = records_start + inner if not compression else (chunk.chunk_start_offset, inner)
                    self._add(name, log_time, ref)
        self.indexed_from = "message index"
        return True

    def _scan(self, handle: Any) -> None:
        self.entries.clear()
        channels: Dict[int, str] = {}
        schemas: Dict[int, str] = {}
        position = 8
        while position + 9 <= self.size:
            handle.seek(position)
            opcode, length = struct.unpack("<BQ", handle.read(9))
            body_start = position + 9
            end = body_start + length
            if end > self.size:
                break  # torn tail: the recorder was killed while writing this record
            if opcode == OP_CHUNK:
                head = handle.read(8 + 8 + 8 + 4 + 4)
                _start_time, _end_time, uncompressed, _crc, name_length = struct.unpack("<QQQII", head)
                compression = handle.read(name_length).decode()
                (records_length,) = struct.unpack("<Q", handle.read(8))
                records_start = handle.tell()
                if not compression:
                    self._scan_records(handle, records_start, records_start + records_length, channels, schemas, None)
                else:
                    self.chunks[position] = (compression, records_start, records_length, uncompressed)
                    data = _decompress(compression, handle.read(records_length), uncompressed)
                    self._scan_buffer(data, channels, schemas, position)
            elif opcode in (OP_SCHEMA, OP_CHANNEL, OP_MESSAGE):
                self._scan_records(handle, position, end, channels, schemas, None)
            position = end
        self.indexed_from = "scan"

    def _parse_meta(self, opcode: int, body: bytes, channels: Dict[int, str], schemas: Dict[int, str]) -> None:
        if opcode == OP_SCHEMA:
            (schema_id,) = struct.unpack_from("<H", body, 0)
            name, _ = _read_string(body, 2)
            schemas[schema_id] = name
        elif opcode == OP_CHANNEL:
            channel_id, schema_id = struct.unpack_from("<HH", body, 0)
            topic, _ = _read_string(body, 4)
            name = _classify(topic, schemas.get(schema_id, ""))
            if name:
                channels[channel_id] = name

    def _scan_records(self, handle: Any, start: int, end: int, channels: Dict[int, str], schemas: Dict[int, str],
                      _unused: Any) -> None:
        position = start
        while position + 9 <= end:
            handle.seek(position)
            opcode, length = struct.unpack("<BQ", handle.read(9))
            if position + 9 + length > end:
                break
            if opcode == OP_MESSAGE:
                channel_id, _sequence, log_time = struct.unpack("<HIQ", handle.read(14))
                self._add(channels.get(channel_id), log_time, position)
            elif opcode in (OP_SCHEMA, OP_CHANNEL):
                self._parse_meta(opcode, handle.read(length), channels, schemas)
            position += 9 + length

    def _scan_buffer(self, data: bytes, channels: Dict[int, str], schemas: Dict[int, str], chunk_offset: int) -> None:
        position = 0
        while position + 9 <= len(data):
            opcode, length = struct.unpack_from("<BQ", data, position)
            if opcode == OP_MESSAGE:
                channel_id, _sequence, log_time = struct.unpack_from("<HIQ", data, position + 9)
                self._add(channels.get(channel_id), log_time, (chunk_offset, position))
            elif opcode in (OP_SCHEMA, OP_CHANNEL):
                self._parse_meta(opcode, data[position + 9:position + 9 + length], channels, schemas)
            position += 9 + length

    def read(self, handle: Any, ref: Any, chunk_cache: "OrderedDict[Tuple, bytes]") -> Tuple[int, int, bytes]:
        """-> (sequence, log_time, payload) of the message at ref."""
        if isinstance(ref, int):
            handle.seek(ref)
            head = handle.read(31)
            opcode, length, _channel, sequence, log_time, _publish = struct.unpack("<BQHIQQ", head)
            if opcode != OP_MESSAGE:
                raise ValueError("index points at a non-message record")
            return sequence, log_time, handle.read(length - 22)
        chunk_offset, inner = ref
        key = (str(self.path), chunk_offset)
        data = chunk_cache.get(key)
        if data is None:
            compression, start, length, size = self.chunks[chunk_offset]
            handle.seek(start)
            data = _decompress(compression, handle.read(length), size)
            chunk_cache[key] = data
            while len(chunk_cache) > 6:
                chunk_cache.popitem(last=False)
        opcode, length, _channel, sequence, log_time, _publish = struct.unpack_from("<BQHIQQ", data, inner)
        return sequence, log_time, data[inner + 31:inner + 9 + length]


class SessionIndex:
    """All recordings of one session, merged per stream and sorted by log_time."""

    def __init__(self, directory: Path) -> None:
        import numpy as np

        self.directory = directory
        self.session_id = directory.name
        self.manifest: Dict[str, Any] = {}
        try:
            self.manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        self.files = [McapFile(path) for path in sorted((directory / "recordings").glob("*.mcap"))
                      if path.stat().st_size > 8]
        if not self.files:
            raise FileNotFoundError("这个 session 里还没有可读的 .mcap 文件")
        merged: Dict[str, List[Tuple[int, int, Any]]] = {}
        for number, item in enumerate(self.files):
            for name, entries in item.entries.items():
                merged.setdefault(name, []).extend((log_time, number, ref) for log_time, ref in entries)
        self.streams: Dict[str, Dict[str, Any]] = {}
        for name, entries in merged.items():
            entries.sort(key=lambda entry: entry[0])
            self.streams[name] = {
                "times": np.array([entry[0] for entry in entries], dtype=np.int64),
                "refs": [(entry[1], entry[2]) for entry in entries],
            }
        video = [name for name in VIEW_ORDER if name in self.streams]
        if not video:
            raise ValueError("这个 session 里没有图像数据")
        self.video = video
        self.anchor = next(name for name in rd.ANCHOR_PRIORITY if name in self.streams)
        self.t0 = int(min(int(self.streams[name]["times"][0]) for name in video))
        self.t_end = int(max(int(self.streams[name]["times"][-1]) for name in video))
        self.depth_scale = float(((self.manifest.get("streams") or {}).get("depth") or {}).get("depth_scale_m") or 0.001)
        self.lock = threading.Lock()
        self.chunk_cache: "OrderedDict[Tuple, bytes]" = OrderedDict()
        self.handles: Dict[int, Any] = {}
        self.depth_cache: "OrderedDict[int, Any]" = OrderedDict()
        self.opened = time.time()

    def timeline(self) -> Dict[str, Any]:
        streams = {}
        manifest_streams = self.manifest.get("streams") or {}
        for name in self.video:
            times = (self.streams[name]["times"] - self.t0) / 1e6
            entry = dict(manifest_streams.get(name) or {})
            streams[name] = {
                "label": LABELS.get(name, name), "t": [round(float(value), 3) for value in times],
                "width": entry.get("width"), "height": entry.get("height"), "fps": entry.get("fps"),
                "encoding": entry.get("encoding"), "has_info": (name + "#info") in self.streams,
            }
        return {"session_id": self.session_id, "t0_ns": self.t0, "duration_ms": round((self.t_end - self.t0) / 1e6, 3),
                "anchor": self.anchor, "streams": streams, "depth_scale": self.depth_scale,
                "files": [{"name": item.path.name, "bytes": item.size, "indexed_from": item.indexed_from}
                          for item in self.files]}

    def _message(self, name: str, index: int) -> Tuple[int, int, bytes]:
        stream = self.streams[name]
        if not 0 <= index < len(stream["refs"]):
            raise IndexError("frame {} out of range".format(index))
        number, ref = stream["refs"][index]
        with self.lock:
            handle = self.handles.get(number)
            if handle is None:
                handle = self.handles[number] = self.files[number].path.open("rb")
            return self.files[number].read(handle, ref, self.chunk_cache)

    def image_bytes(self, name: str, index: int) -> Tuple[bytes, str, int, int]:
        """-> (encoded image as recorded, its format, frame number, log_time)."""
        sequence, log_time, payload = self._message(name, index)
        message = rd.Schemas.get().CompressedImage.FromString(payload)
        return bytes(message.data), message.format or "jpeg", sequence, log_time

    def depth_raw(self, index: int) -> Any:
        import cv2
        import numpy as np

        with self.lock:
            cached = self.depth_cache.get(index)
            if cached is not None:
                self.depth_cache.move_to_end(index)
                return cached
        data, _fmt, _sequence, _log_time = self.image_bytes("depth", index)
        raw = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if raw is None or raw.dtype != np.uint16:
            raise ValueError("depth frame {} is not a 16-bit image".format(index))
        with self.lock:
            self.depth_cache[index] = raw
            while len(self.depth_cache) > 6:
                self.depth_cache.popitem(last=False)
        return raw

    def frame_info(self, name: str, index: int) -> Dict[str, Any]:
        times = self.streams[name]["times"]
        target = int(times[index])
        info = self.streams.get(name + "#info")
        result: Dict[str, Any] = {"stream": name, "index": index, "log_time_ns": target,
                                  "t_ms": round((target - self.t0) / 1e6, 3)}
        if info is None:
            return result
        import numpy as np
        position = int(np.searchsorted(info["times"], target))
        if position >= len(info["times"]) or int(info["times"][position]) != target:
            return result
        _sequence, _log_time, payload = self._message(name + "#info", position)
        message = rd.Schemas.get().FrameInfo.FromString(payload)
        for field in ("frame_number", "timestamp_ms", "timestamp_domain", "hw_timestamp_us", "sensor_timestamp_us",
                      "exposure_us", "gain", "width", "height", "pixel_format", "encoding", "payload_bytes",
                      "host_receive_epoch_ns"):
            result[field] = getattr(message, field)
        try:
            result["metadata"] = json.loads(message.metadata_json or "{}")
        except ValueError:
            result["metadata"] = {}
        return result

    def close(self) -> None:
        with self.lock:
            for handle in self.handles.values():
                try:
                    handle.close()
                except Exception:
                    pass
            self.handles.clear()


def first_image(directory: Path, prefer: Tuple[str, ...] = ("color", "ir_left", "depth")) -> Optional[Tuple[str, bytes]]:
    """The first recorded image of a session, read from the start of its first file (for thumbnails)."""
    recordings = sorted((directory / "recordings").glob("*.mcap"))
    if not recordings:
        return None
    path = recordings[0]
    found: Dict[str, bytes] = {}
    channels: Dict[int, str] = {}
    schemas: Dict[int, str] = {}
    size = path.stat().st_size
    with path.open("rb") as handle:
        if handle.read(8) != MCAP_MAGIC:
            return None
        position = 8
        chunks = 0
        while position + 9 <= size and chunks < 8:
            handle.seek(position)
            opcode, length = struct.unpack("<BQ", handle.read(9))
            end = position + 9 + length
            if end > size:
                break
            if opcode == OP_CHUNK:
                chunks += 1
                head = handle.read(32)
                _a, _b, uncompressed, _crc, name_length = struct.unpack("<QQQII", head)
                compression = handle.read(name_length).decode()
                (records_length,) = struct.unpack("<Q", handle.read(8))
                data = handle.read(records_length)
                if compression:
                    data = _decompress(compression, data, uncompressed)
                inner = 0
                while inner + 9 <= len(data):
                    code, record_length = struct.unpack_from("<BQ", data, inner)
                    body = data[inner + 9:inner + 9 + record_length]
                    if code == OP_SCHEMA:
                        (schema_id,) = struct.unpack_from("<H", body, 0)
                        schemas[schema_id] = _read_string(body, 2)[0]
                    elif code == OP_CHANNEL:
                        channel_id, schema_id = struct.unpack_from("<HH", body, 0)
                        name = _classify(_read_string(body, 4)[0], schemas.get(schema_id, ""))
                        if name and "#" not in name:
                            channels[channel_id] = name
                    elif code == OP_MESSAGE:
                        (channel_id,) = struct.unpack_from("<H", body, 0)
                        name = channels.get(channel_id)
                        if name and name not in found:
                            found[name] = rd.Schemas.get().CompressedImage.FromString(body[22:]).data
                            if name == prefer[0]:
                                return name, found[name]
                    inner += 9 + record_length
            position = end
    for name in prefer:
        if name in found:
            return name, found[name]
    return None


class Library:
    """The session folder: listing, thumbnails, opened indexes."""

    def __init__(self, root: Path, camera: Optional[LiveCamera]) -> None:
        self.root = root
        self.camera = camera
        self.lock = threading.Lock()
        self.indexes: "OrderedDict[str, SessionIndex]" = OrderedDict()
        self.thumbs: Dict[str, Tuple[float, bytes]] = {}
        self.summaries: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    def directory(self, session_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9._\-]+", session_id or "") or session_id.startswith("."):
            raise KeyError("bad session id")
        path = self.root / session_id
        if not path.is_dir():
            raise KeyError("session not found")
        return path

    def live_session(self) -> str:
        recorder = self.camera.recorder if self.camera else None
        return recorder.session_id if recorder is not None else ""

    def summary(self, path: Path) -> Optional[Dict[str, Any]]:
        manifest_path = path / "manifest.json"
        recordings = sorted((path / "recordings").glob("*.mcap"))
        if not manifest_path.is_file() and not recordings:
            return None
        stamp = max([manifest_path.stat().st_mtime if manifest_path.is_file() else 0.0] +
                    [item.stat().st_mtime for item in recordings])
        cached = self.summaries.get(path.name)
        if cached and cached[0] == stamp:
            item = dict(cached[1])
        else:
            manifest: Dict[str, Any] = {}
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                pass
            size = sum(item.stat().st_size for item in recordings)
            streams = manifest.get("streams") or {}
            verify = manifest.get("verify") or {}
            duration = manifest.get("duration_s")
            if duration is None and manifest.get("status") == "recovered":  # repair does not write duration_s
                try:
                    report = json.loads((path / "derived" / "verify_report.json").read_text(encoding="utf-8"))
                    duration = max(entry.get("duration_s") or 0 for entry in report.get("streams", {}).values()) or None
                except Exception:
                    duration = None
            device = manifest.get("device") or {}
            created = manifest.get("created_local") or datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()
            item = {
                "id": path.name, "created": created, "status": manifest.get("status", "unknown"),
                "duration_s": duration, "bytes": size, "files": len(recordings),
                "device": {"name": device.get("name", ""), "serial": device.get("serial_number", "")},
                "streams": {name: {"width": entry.get("width"), "height": entry.get("height"), "fps": entry.get("fps")}
                            for name, entry in streams.items() if name in VIEW_ORDER},
                "result": verify.get("result"), "problems": verify.get("problems") or [],
                "warnings": verify.get("warnings") or [], "scene": manifest.get("scene") or {},
                "stop_reason": manifest.get("stop_reason"), "frames": manifest.get("total_frames"),
            }
            self.summaries[path.name] = (stamp, dict(item))
        live = item["id"] == self.live_session()
        item["live"] = live
        item["needs_repair"] = item["status"] == "recording" and not live and not rd.session_is_live(path)
        return item

    def list(self) -> List[Dict[str, Any]]:
        items = []
        try:
            folders = [path for path in self.root.iterdir() if path.is_dir() and not path.name.startswith(".")]
        except OSError:
            folders = []
        for path in folders:
            try:
                item = self.summary(path)
            except Exception:
                item = None
            if item is not None:
                items.append(item)
        items.sort(key=lambda item: item["created"], reverse=True)
        return items

    def index(self, session_id: str) -> SessionIndex:
        with self.lock:
            existing = self.indexes.get(session_id)
            if existing is not None:
                self.indexes.move_to_end(session_id)
                return existing
        if session_id == self.live_session():
            raise RuntimeError("这个 session 正在录制，停止后才能回放")
        index = SessionIndex(self.directory(session_id))
        with self.lock:
            self.indexes[session_id] = index
            while len(self.indexes) > 3:
                _key, old = self.indexes.popitem(last=False)
                old.close()
        return index

    def forget(self, session_id: str) -> None:
        with self.lock:
            old = self.indexes.pop(session_id, None)
            self.thumbs.pop(session_id, None)
            self.summaries.pop(session_id, None)
        if old is not None:
            old.close()

    def thumbnail(self, session_id: str) -> Optional[bytes]:
        import cv2
        import numpy as np

        path = self.directory(session_id)
        recordings = sorted((path / "recordings").glob("*.mcap"))
        stamp = recordings[0].stat().st_mtime if recordings else 0.0
        cached = self.thumbs.get(session_id)
        if cached and cached[0] == stamp:
            return cached[1]
        found = first_image(path)
        if found is None:
            return None
        name, data = found
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if image is None:
            return None
        if name == "depth":
            image = PALETTE.colorize(image, 0.001, 0.2, 6.0, "turbo")
        width = 320
        image = cv2.resize(image, (width, int(round(image.shape[0] * width / image.shape[1]))), interpolation=cv2.INTER_AREA)
        thumb = encode_jpeg(image, 78)
        self.thumbs[session_id] = (stamp, thumb)
        return thumb

    def report(self, session_id: str) -> Dict[str, Any]:
        path = self.directory(session_id)
        result: Dict[str, Any] = {"id": session_id, "path": str(path.resolve())}
        for key, name in (("manifest", "manifest.json"), ("verify", "derived/verify_report.json")):
            try:
                result[key] = json.loads((path / name).read_text(encoding="utf-8"))
            except Exception:
                result[key] = None
        if result["manifest"]:
            result["manifest"].pop("events", None)
            result["events"] = [item for item in json.loads((path / "manifest.json").read_text(encoding="utf-8"))
                                .get("events", []) if item.get("level") != "info"][-30:]
        return result


# --------------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def reveal_in_file_manager(path: Path, select: bool = True) -> None:
    """Show a file or folder in Finder / Explorer / the desktop's file manager, as the logged-in user."""
    if sys.platform == "darwin":
        command = ["open", "-R", str(path)] if select else ["open", str(path)]
    elif sys.platform == "win32":
        command = ["explorer", "/select,", str(path)] if select else ["explorer", str(path)]
    else:
        command = ["xdg-open", str(path if not select else path.parent)]
    uid = os.environ.get("SUDO_UID", "")
    if sys.platform != "win32" and os.geteuid() == 0:
        if not uid.isdigit():
            raise ApiError(409, "以管理员身份运行时不知道该用哪个用户打开文件夹")
        command = ["sudo", "-u", "#" + uid] + command  # never open a file manager as root
        if sys.platform == "darwin":
            # A launchd service lives outside the user's login session; asuser runs open inside it.
            command = ["launchctl", "asuser", uid] + command
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], camera: LiveCamera, library: Library, allowed_hosts: set) -> None:
        super().__init__(address, StudioHandler)
        self.camera = camera
        self.library = library
        self.allowed_hosts = allowed_hosts
        self.on_restart: Optional[Callable[[], None]] = None

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return  # the browser closed a connection (tab closed, reload): not worth a traceback
        super().handle_error(request, client_address)


class StudioHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: StudioServer

    def log_message(self, format: str, *args: Any) -> None:  # quiet: the terminal shows the recorder's log
        pass

    # ---- plumbing -----------------------------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str, headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data: Any, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False, allow_nan=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise ApiError(413, "request too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ApiError(400, "invalid JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "expected a JSON object")
        return data

    def _check_origin(self, write: bool) -> None:
        host = (self.headers.get("Host") or "").lower()
        if host not in self.server.allowed_hosts:
            raise ApiError(403, "unexpected Host header")  # DNS rebinding: this server runs as root on macOS
        if write:
            origin = self.headers.get("Origin")
            if origin and urllib.parse.urlparse(origin).netloc.lower() not in self.server.allowed_hosts:
                raise ApiError(403, "cross-origin request refused")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        self._dispatch(write=False)

    def do_POST(self) -> None:
        self._dispatch(write=True)

    def _dispatch(self, write: bool) -> None:
        url = urllib.parse.urlparse(self.path)
        query = {key: values[-1] for key, values in urllib.parse.parse_qs(url.query).items()}
        try:
            self._check_origin(write)
            if url.path.startswith("/api/"):
                handler = self._post if write else self._get
                handler(url.path, query)
            elif not write:
                self._static(url.path)
            else:
                raise ApiError(404, "not found")
        except ApiError as error:
            self._json({"error": str(error)}, error.status)
        except (KeyError, FileNotFoundError) as error:
            self._json({"error": str(error).strip("'\"")}, 404)
        except (ValueError, RuntimeError, IndexError) as error:
            self._json({"error": str(error)}, 409)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:  # keep the server alive, show the reason
            traceback.print_exc()
            try:
                self._json({"error": "{}: {}".format(type(error).__name__, error)}, 500)
            except Exception:
                pass

    def _static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        target = (WEB_ROOT / path.lstrip("/")).resolve()
        if WEB_ROOT.resolve() not in target.parents or not target.is_file():
            raise ApiError(404, "not found")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript",):
            content_type += "; charset=utf-8"
        self._send(200, target.read_bytes(), content_type)

    # ---- GET --------------------------------------------------------------------------------
    def _get(self, path: str, query: Dict[str, str]) -> None:
        camera, library = self.server.camera, self.server.library
        parts = [urllib.parse.unquote(part) for part in path.strip("/").split("/")][1:]  # drop "api"
        if parts == ["ping"]:
            return self._json({"ok": True, "version": __version__})
        if parts == ["state"]:
            if query.get("want") == "1":  # the live page is visible, or the settings panel is open
                camera.touch()
            return self._json(camera.snapshot(int(query.get("since", "0") or 0)))
        if parts == ["options"]:
            camera.touch()
            return self._json(camera.options())
        if parts == ["profiles"]:
            return self._json(camera.profiles())
        if len(parts) == 2 and parts[0] == "live":
            if parts[1] == "depth_at":
                return self._json(camera.live_depth_at(float(query.get("x", 0.5)), float(query.get("y", 0.5))))
            camera.touch()
            result = camera.live_image(parts[1], int(query.get("after", "0") or 0), query)
            if result is None:
                return self._send(204, b"", "text/plain")
            data, headers = result
            return self._send(200, data, "image/jpeg", headers)
        if parts == ["sessions"]:
            return self._json({"root": str(library.root.resolve()), "sessions": library.list()})
        if len(parts) >= 3 and parts[0] == "sessions":
            session_id, action = parts[1], parts[2]
            if action == "thumb":
                data = library.thumbnail(session_id)
                if data is None:
                    raise ApiError(404, "no image")
                return self._send(200, data, "image/jpeg")
            if action == "report":
                return self._json(library.report(session_id))
            if action == "timeline":
                return self._json(library.index(session_id).timeline())
            index = library.index(session_id)
            if action == "frame" and len(parts) == 5:
                name, number = parts[3], int(parts[4])
                if name not in index.video:
                    raise ApiError(404, "no stream " + name)
                if name == "depth":
                    near, far, cmap = depth_params(query)
                    raw = index.depth_raw(number)
                    data = encode_jpeg(PALETTE.colorize(raw, index.depth_scale, near, far, cmap), 88)
                    content_type = "image/jpeg"
                    sequence, log_time = "", int(index.streams["depth"]["times"][number])
                else:
                    data, fmt, sequence, log_time = index.image_bytes(name, number)
                    content_type = "image/png" if fmt == "png" else "image/jpeg"
                return self._send(200, data, content_type, {"X-Frame": str(sequence), "X-Log-Time": str(log_time),
                                                            "Cache-Control": "private, max-age=3600"})
            if action == "info" and len(parts) == 5:
                return self._json(index.frame_info(parts[3], int(parts[4])))
            if action == "depth_at":
                raw = index.depth_raw(int(query.get("i", 0)))
                result = sample_depth(raw, float(query.get("x", 0.5)), float(query.get("y", 0.5)))
                result["m"] = result["raw"] * index.depth_scale
                return self._json(result)
        raise ApiError(404, "not found")

    # ---- POST -------------------------------------------------------------------------------
    def _post(self, path: str, query: Dict[str, str]) -> None:
        camera, library = self.server.camera, self.server.library
        parts = [urllib.parse.unquote(part) for part in path.strip("/").split("/")][1:]
        body = self._body()
        if parts == ["record", "start"]:
            settings = camera.settings
            changes = {key: body[key] for key in ("scene", "duration") if key in body}
            if changes:
                settings.update(changes)
            values = settings.snapshot()
            return self._json(camera.start_recording(values["scene"], values["duration"]))
        if parts == ["record", "stop"]:
            camera.stop_recording()
            return self._json({"ok": True})
        if parts == ["options"]:
            camera.set_option(str(body["role"]), str(body["name"]), float(body["value"]))
            return self._json(camera.options())
        if parts == ["options", "reset"]:
            camera.reset_options(str(body["role"]))
            return self._json(camera.options())
        if parts == ["settings"]:
            current = camera.settings.snapshot()
            if camera.recorder is not None and any(key in body and body[key] != current.get(key) for key in RESTART_KEYS):
                raise ApiError(409, "录制中不能改视频流设置，先停止录制")
            changed = camera.settings.update(body)
            restart = any(key in changed for key in RESTART_KEYS)
            if restart:
                camera.request_restart()
            return self._json({"changed": changed, "restart": restart, "settings": camera.settings.snapshot()})
        if parts == ["camera", "restart"]:
            camera.request_restart()
            return self._json({"ok": True})
        if parts == ["camera", "reset"]:
            camera.request_reset()
            return self._json({"ok": True})
        if parts == ["service", "restart"]:
            if self.server.on_restart is None:
                raise ApiError(409, "只有装成后台服务时才能从网页重启")
            if camera.recorder is not None:
                raise ApiError(409, "录制中不能重启服务，先停止录制")
            self._json({"ok": True})
            threading.Timer(0.3, self.server.on_restart).start()  # launchd starts it again (KeepAlive)
            return None
        if parts == ["reveal-root"]:
            library.root.mkdir(parents=True, exist_ok=True)
            reveal_in_file_manager(library.root.resolve(), select=False)
            return self._json({"ok": True})
        if len(parts) == 3 and parts[0] == "sessions":
            session_id, action = parts[1], parts[2]
            directory = library.directory(session_id)
            if action == "reveal":
                reveal_in_file_manager(directory)
                return self._json({"ok": True})
            if action == "repair":
                if session_id == library.live_session():
                    raise ApiError(409, "这个 session 正在录制")
                library.forget(session_id)
                output = io.StringIO()
                original = sys.stdout
                try:
                    sys.stdout = output
                    code = rd.command_repair(argparse.Namespace(session=[str(directory)], force=False))
                finally:
                    sys.stdout = original
                library.forget(session_id)
                return self._json({"code": code, "log": output.getvalue()[-4000:]})
        raise ApiError(404, "not found")


# --------------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="d455_studio.py", description="Web front end for record_d455.py")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP port (default {})".format(DEFAULT_PORT))
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to listen on (default 127.0.0.1, this computer only)")
    parser.add_argument("--sessions", default="sessions", help="session folder (default ./sessions, same as run.sh)")
    parser.add_argument("--serial", default="", help="only use the camera with this serial number")
    parser.add_argument("--simulate", action="store_true", help="simulated D455, no camera needed")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--settings", default="", help="settings file (default ./studio_settings.json)")
    parser.add_argument("--idle-after", type=float, default=IDLE_AFTER_S,
                        help="pause the camera after this many seconds without a live view (0: never; default %(default)s)")
    parser.add_argument("--log-dir", default="logs",
                        help="folder for start-hang reports (default ./logs, same as record_d455.py)")
    parser.add_argument("--service", action="store_true", help=argparse.SUPPRESS)  # set by install_service.sh
    parser.add_argument("--no-sudo", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-usb-hold", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version="d455_studio.py {} (record_d455.py {})".format(
        __version__, rd.__version__))
    return parser


def open_browser(url: str) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return  # studio.sh opens it as the normal user; a browser must never run as root
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    os.chdir(str(HERE))
    rd.check_dependencies()
    rs = rd.import_realsense()
    if rd.__version__ != BUILT_AGAINST:
        rd.log("Note: d455_studio {} was written against record_d455.py {}, found {}.".format(
            __version__, BUILT_AGAINST, rd.__version__))
    if not args.simulate:
        rd.require_admin_on_macos(args)  # macOS: re-runs this program through sudo and with the IMU crash guard
        rd.start_macos_usb_hold(args)
    out_root = Path(args.sessions).expanduser()
    if not out_root.is_absolute():
        out_root = HERE / out_root
    out_root.mkdir(parents=True, exist_ok=True)
    rd.hand_back_ownership(out_root, recursive=False)
    for leftover in rd.unfinished_sessions(out_root):
        rd.log("Note: session {} was never closed; the studio can repair it from the session list.".format(leftover.name))
    settings = Settings(Path(args.settings).expanduser() if args.settings else SETTINGS_PATH)
    log_dir = Path(args.log_dir)
    camera = LiveCamera(rs, settings, out_root, serial=args.serial, simulate=args.simulate,
                        log_dir=log_dir if log_dir.is_absolute() else HERE / log_dir, idle_after=args.idle_after)
    library = Library(out_root, camera)
    camera.library = library
    camera.service = bool(args.service)
    port = args.port
    allowed = {"127.0.0.1:{}".format(port), "localhost:{}".format(port), "[::1]:{}".format(port)}
    if args.host not in ("127.0.0.1", "localhost"):
        allowed |= {"{}:{}".format(args.host, port)}
        try:
            import socket
            allowed |= {"{}:{}".format(socket.gethostname().lower(), port)}
            allowed |= {"{}:{}".format(address, port) for address in socket.gethostbyname_ex(socket.gethostname())[2]}
        except Exception:
            pass
        if args.host == "0.0.0.0":
            rd.log("WARNING: listening on all network interfaces; anyone on this network can control the camera.")
            allowed = None  # type: ignore[assignment]
    try:
        server = StudioServer((args.host, port), camera, library, allowed or _AnyHost())
    except OSError as error:
        rd.log("Cannot listen on port {} ({}). Is D455 Studio already running? Open http://127.0.0.1:{}/".format(
            port, error, port))
        return 1
    url = "http://127.0.0.1:{}/".format(port)
    camera.start()
    stopping = threading.Event()

    def handle_signal(_signum: int, _frame: Any) -> None:
        if stopping.is_set():
            rd.log("Second interrupt: exiting immediately.")
            os._exit(130)
        stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    def restart_service() -> None:
        """Exit so launchd's KeepAlive starts a fresh process; also the way out of an SDK call that never
        returns, hence the hard deadline."""
        rd.log("Restart requested from the page.")
        deadline = threading.Timer(10.0, os._exit, args=(0,))
        deadline.daemon = True
        deadline.start()
        handle_signal(0, None)

    if args.service:
        server.on_restart = restart_service

    for name in ("SIGINT", "SIGTERM"):
        try:
            signal.signal(getattr(signal, name), handle_signal)
        except (ValueError, OSError, AttributeError):
            pass
    rd.log("")
    rd.log("D455 Studio {}  (record_d455.py {}){}".format(__version__, rd.__version__,
                                                          "  [simulated camera]" if args.simulate else ""))
    rd.log("Open {} in the browser. Sessions: {}".format(url, out_root.resolve()))
    rd.log("Press Ctrl+C here to quit (a running recording is stopped and saved first).")
    rd.log("")
    if not args.no_browser:
        threading.Timer(0.6, open_browser, args=(url,)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        rd.log("Stopping: closing the recording (if any) and the camera...")
        camera.stop()
        server.server_close()
    if sys.platform == "darwin":
        # Same as record_d455: skip the SDK teardown, which is known to abort on macOS.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
    return 0


class _AnyHost(set):
    def __contains__(self, item: object) -> bool:
        return True


if __name__ == "__main__":
    sys.exit(main())
