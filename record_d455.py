#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""record_d455.py - plug-and-play recorder for ONE Intel RealSense D455 at a time.

Plug a D455 into a USB 3 port and run (macOS: through run.sh, which adds the required sudo):

    python record_d455.py                 # wait for the camera, record until Ctrl+C
    python record_d455.py --duration 600  # record 10 minutes
    python record_d455.py --preview       # with a live color + depth window
    python record_d455.py selftest        # no camera needed: simulated D455, end-to-end check
    python record_d455.py verify  sessions/<session_id>
    python record_d455.py export  sessions/<session_id>
    python record_d455.py repair  sessions/<session_id>   # after a crash or power loss

Default streams: depth (848x480 Z16) + color (1280x720) + left infrared (848x480 Y8), all at 30 fps.
The IMU is optional: --streams depth,color,ir_left,imu

What is recorded (one MCAP session directory per run, same layout as the ToF/IRA sessions):

    sessions/<YYYY-MM-DD_HH-MM-SS>_d455_<serial>/
      manifest.json                 device, stream profiles, intrinsics, extrinsics, options, statistics
      recordings/<session>_000.mcap raw truth: every frame of every stream, never resampled or dropped to "align"
      derived/frame_index.csv       one row per video frame with all timestamps (rebuildable from the MCAP)
      derived/alignment.csv         depth-anchored nearest-frame table with time differences
      derived/verify_report.json    drop / jitter / alignment statistics
      annotations/                  empty, reserved for labels

Timestamps (the part that matters for cross-modality alignment):

  * All D455 streams (depth, IR, color, accel, gyro) are stamped by the same camera hardware clock.
  * The SDK option "global time" converts that clock to host epoch milliseconds with a continuously
    fitted linear model. That value is stored as MCAP log_time (ns) and as timestamp_ms.
  * The raw hardware timestamp (frame_timestamp, usec, wraps every ~71.6 min) and the mid-exposure
    timestamp (sensor_timestamp) are stored per frame, together with the host arrival time
    (MCAP publish_time, host_receive_epoch_ns).
  * Depth and color are streamed at the same FPS and RGB "auto exposure priority" is switched off,
    which are the conditions RealSense gives for depth/RGB sync on a single D455. The recorder does
    not trust that blindly: it measures the depth-color time difference on the raw device clock and
    reports it after every recording.

Capture follows the official low-level pattern used by the SDK tool rs-data-collect (sensor.open +
sensor.start(callback)), so no frame passes through a syncer that could drop or duplicate it.

Reading the data back:

    from record_d455 import Session
    session = Session("sessions/<session_id>")
    for item in session.aligned_frames():          # one item per depth frame
        depth_m = item["depth"] * session.depth_scale
        color, ir = item["color"], item["ir_left"]  # None if no frame within half a frame period

Dependencies: pyrealsense2 (pyrealsense2-macosx on macOS), numpy, opencv-python, mcap, protobuf.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import platform
import queue
import re
import shutil
import signal
import sys
import tempfile
import threading
import types
import time
from array import array
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

__version__ = "1.2.6"
FORMAT_NAME = "realsense-mcap-session"
FORMAT_VERSION = 1
HW_CLOCK_WRAP_US = 1 << 32  # the D400 device clock is a 32-bit microsecond counter (~71.6 min)

# name -> (pyrealsense2 stream attribute, required stream index or None, kind)
STREAM_DEFS: Dict[str, Tuple[str, Optional[int], str]] = {
    "depth": ("depth", None, "video"),
    "color": ("color", None, "video"),
    "ir_left": ("infrared", 1, "video"),
    "ir_right": ("infrared", 2, "video"),
    "accel": ("accel", None, "motion"),
    "gyro": ("gyro", None, "motion"),
}
VIDEO_STREAMS = ("depth", "color", "ir_left", "ir_right")
MOTION_STREAMS = ("accel", "gyro")
DEFAULT_STREAMS = ("depth", "color", "ir_left")  # IMU is opt-in: --streams depth,color,ir_left,imu
ANCHOR_PRIORITY = ("depth", "color", "ir_left", "ir_right")
STREAM_DIR = {"depth": "depth", "color": "camera", "ir_left": "ir_left", "ir_right": "ir_right"}
IMU_TOPIC = {"accel": "imu/accel", "gyro": "imu/gyro"}

METADATA_CANDIDATES = (
    "frame_counter", "frame_timestamp", "sensor_timestamp", "actual_exposure", "gain_level",
    "auto_exposure", "white_balance", "time_of_arrival", "temperature", "backend_timestamp",
    "actual_fps", "frame_laser_power", "frame_laser_power_mode", "frame_emitter_mode",
    "frame_led_power", "exposure_priority", "exposure_roi_left", "exposure_roi_right",
    "exposure_roi_top", "exposure_roi_bottom", "brightness", "contrast", "saturation", "sharpness",
    "auto_white_balance_temperature", "backlight_compensation", "hue", "gamma",
    "manual_white_balance", "power_line_frequency", "low_light_compensation", "raw_frame_size",
    "gpio_input_data", "sequence_name", "sequence_id", "sequence_size", "trigger", "preset",
    "input_width", "input_height", "sub_preset_info", "calib_info", "crc",
)
MOTION_METADATA_CANDIDATES = ("frame_timestamp", "time_of_arrival", "backend_timestamp")

INDEX_COLUMNS = (
    "stream", "frame_number", "timestamp_ms", "timestamp_domain", "log_time_ns",
    "host_receive_epoch_ns", "host_receive_monotonic_ns", "hw_timestamp_us",
    "sensor_timestamp_us", "exposure_us", "gain", "payload_bytes", "recording",
)


def sdk_version(rs: Any) -> str:
    version = getattr(rs, "__version__", None)
    if version:
        return str(version)
    try:
        from importlib import metadata
        for package in ("pyrealsense2", "pyrealsense2-macosx", "pyrealsense2-beta"):
            try:
                return metadata.version(package)
            except metadata.PackageNotFoundError:
                continue
    except Exception:
        pass
    return "unknown"


def log(message: str = "") -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print("WARNING: " + message, flush=True)


# --------------------------------------------------------------------------------------------
# Message schemas. Built at runtime from descriptors, so no protoc and no generated *_pb2 files.
# foxglove.CompressedImage / foxglove.CameraCalibration use the published Foxglove field numbers,
# so Foxglove Studio and the existing labeling tool decode them from the schema embedded in the file.
# --------------------------------------------------------------------------------------------
class Schemas:
    _instance: Optional["Schemas"] = None

    @classmethod
    def get(cls) -> "Schemas":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        from google.protobuf import descriptor_pb2, descriptor_pool, message_factory, timestamp_pb2

        fd = descriptor_pb2.FieldDescriptorProto
        opt, rep = fd.LABEL_OPTIONAL, fd.LABEL_REPEATED
        timestamp_file = descriptor_pb2.FileDescriptorProto()
        timestamp_pb2.DESCRIPTOR.CopyToProto(timestamp_file)
        ts_type = ".google.protobuf.Timestamp"

        def make_file(name: str, package: str, deps: List[str], messages: List[Tuple[str, list]]):
            proto = descriptor_pb2.FileDescriptorProto(name=name, package=package, syntax="proto3")
            proto.dependency.extend(deps)
            for message_name, fields in messages:
                message = proto.message_type.add(name=message_name)
                for field_name, number, field_type, label, type_name in fields:
                    item = message.field.add(name=field_name, number=number, type=field_type, label=label)
                    if type_name:
                        item.type_name = type_name
            return proto

        image_file = make_file(
            "foxglove/CompressedImage.proto", "foxglove", [timestamp_file.name],
            [("CompressedImage", [
                ("timestamp", 1, fd.TYPE_MESSAGE, opt, ts_type),
                ("frame_id", 4, fd.TYPE_STRING, opt, ""),
                ("data", 2, fd.TYPE_BYTES, opt, ""),
                ("format", 3, fd.TYPE_STRING, opt, ""),
            ])])
        calibration_file = make_file(
            "foxglove/CameraCalibration.proto", "foxglove", [timestamp_file.name],
            [("CameraCalibration", [
                ("timestamp", 1, fd.TYPE_MESSAGE, opt, ts_type),
                ("frame_id", 9, fd.TYPE_STRING, opt, ""),
                ("width", 2, fd.TYPE_FIXED32, opt, ""),
                ("height", 3, fd.TYPE_FIXED32, opt, ""),
                ("distortion_model", 4, fd.TYPE_STRING, opt, ""),
                ("D", 5, fd.TYPE_DOUBLE, rep, ""),
                ("K", 6, fd.TYPE_DOUBLE, rep, ""),
                ("R", 7, fd.TYPE_DOUBLE, rep, ""),
                ("P", 8, fd.TYPE_DOUBLE, rep, ""),
            ])])
        realsense_file = make_file(
            "realsense/recording.proto", "realsense", [],
            [("FrameInfo", [
                ("stream", 1, fd.TYPE_STRING, opt, ""),
                ("frame_number", 2, fd.TYPE_UINT64, opt, ""),
                ("timestamp_ms", 3, fd.TYPE_DOUBLE, opt, ""),
                ("timestamp_domain", 4, fd.TYPE_STRING, opt, ""),
                ("host_receive_epoch_ns", 5, fd.TYPE_INT64, opt, ""),
                ("host_receive_monotonic_ns", 6, fd.TYPE_INT64, opt, ""),
                ("hw_timestamp_us", 7, fd.TYPE_INT64, opt, ""),
                ("sensor_timestamp_us", 8, fd.TYPE_INT64, opt, ""),
                ("exposure_us", 9, fd.TYPE_INT64, opt, ""),
                ("gain", 10, fd.TYPE_INT64, opt, ""),
                ("width", 11, fd.TYPE_UINT32, opt, ""),
                ("height", 12, fd.TYPE_UINT32, opt, ""),
                ("pixel_format", 13, fd.TYPE_STRING, opt, ""),
                ("encoding", 14, fd.TYPE_STRING, opt, ""),
                ("payload_bytes", 15, fd.TYPE_UINT32, opt, ""),
                ("metadata_json", 16, fd.TYPE_STRING, opt, ""),
            ]), ("ImuSample", [
                ("stream", 1, fd.TYPE_STRING, opt, ""),
                ("frame_number", 2, fd.TYPE_UINT64, opt, ""),
                ("timestamp_ms", 3, fd.TYPE_DOUBLE, opt, ""),
                ("timestamp_domain", 4, fd.TYPE_STRING, opt, ""),
                ("host_receive_epoch_ns", 5, fd.TYPE_INT64, opt, ""),
                ("host_receive_monotonic_ns", 6, fd.TYPE_INT64, opt, ""),
                ("hw_timestamp_us", 7, fd.TYPE_INT64, opt, ""),
                ("x", 8, fd.TYPE_DOUBLE, opt, ""),
                ("y", 9, fd.TYPE_DOUBLE, opt, ""),
                ("z", 10, fd.TYPE_DOUBLE, opt, ""),
            ])])

        pool = descriptor_pool.DescriptorPool()
        for proto in (timestamp_file, image_file, calibration_file, realsense_file):
            pool.AddSerializedFile(proto.SerializeToString())
        if hasattr(message_factory, "GetMessageClass"):
            make_class = message_factory.GetMessageClass
        else:  # protobuf < 4.22
            make_class = message_factory.MessageFactory(pool).GetPrototype

        def descriptor_set(*protos) -> bytes:
            return descriptor_pb2.FileDescriptorSet(file=list(protos)).SerializeToString()

        self.CompressedImage = make_class(pool.FindMessageTypeByName("foxglove.CompressedImage"))
        self.CameraCalibration = make_class(pool.FindMessageTypeByName("foxglove.CameraCalibration"))
        self.FrameInfo = make_class(pool.FindMessageTypeByName("realsense.FrameInfo"))
        self.ImuSample = make_class(pool.FindMessageTypeByName("realsense.ImuSample"))
        frame_meta_schema = {
            "title": "sense.FrameMeta", "type": "object",
            "properties": {
                "frame_id": {"type": "integer"}, "device_timestamp_ms": {"type": "integer"},
                "anchor": {"type": "string"}, "timestamp_ms": {"type": "number"},
                "timestamp_domain": {"type": "string"}, "hw_timestamp_us": {"type": "integer"},
            },
        }
        # key -> (schema name, schema encoding, schema data, message encoding)
        self.defs: Dict[str, Tuple[str, str, bytes, str]] = {
            "image": ("foxglove.CompressedImage", "protobuf", descriptor_set(timestamp_file, image_file), "protobuf"),
            "calibration": ("foxglove.CameraCalibration", "protobuf",
                            descriptor_set(timestamp_file, calibration_file), "protobuf"),
            "info": ("realsense.FrameInfo", "protobuf", descriptor_set(realsense_file), "protobuf"),
            "imu": ("realsense.ImuSample", "protobuf", descriptor_set(realsense_file), "protobuf"),
            "meta": ("sense.FrameMeta", "jsonschema", json.dumps(frame_meta_schema).encode("utf-8"), "json"),
        }


# --------------------------------------------------------------------------------------------
# Session layout helpers
# --------------------------------------------------------------------------------------------
def image_topic(device_id: str, stream: str, encoding: str) -> str:
    return "/{}/{}/{}".format(device_id, STREAM_DIR[stream], encoding)


def info_topic(device_id: str, stream: str) -> str:
    return "/{}/{}/info".format(device_id, STREAM_DIR[stream])


def calibration_topic(device_id: str, stream: str) -> str:
    return "/{}/{}/calibration".format(device_id, STREAM_DIR[stream])


def imu_topic(device_id: str, stream: str) -> str:
    return "/{}/{}".format(device_id, IMU_TOPIC[stream])


def meta_topic(device_id: str) -> str:
    return "/{}/meta".format(device_id)


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.replace(temporary, path)


def hand_back_ownership(path: Path, recursive: bool = True) -> None:
    """Under sudo (needed on macOS) files would belong to root; give them to the user who ran sudo."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if not uid or not hasattr(os, "chown") or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    try:
        owner, group = int(uid), int(gid) if gid else -1
        os.chown(str(path), owner, group)
        if recursive:
            for root, directories, files in os.walk(str(path)):
                for name in directories + files:
                    os.chown(os.path.join(root, name), owner, group)
    except Exception:
        pass


def keep_awake() -> Callable[[], None]:
    """Keep the computer from idle-sleeping during a recording. Returns a function that undoes it."""
    if sys.platform == "darwin":
        try:
            import subprocess
            helper = subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())], stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return helper.terminate
        except Exception:
            return lambda: None
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # CONTINUOUS | SYSTEM_REQUIRED
            return lambda: ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        except Exception:
            return lambda: None
    return lambda: None


def local_iso(epoch_seconds: Optional[float] = None) -> str:
    moment = datetime.fromtimestamp(epoch_seconds) if epoch_seconds is not None else datetime.now()
    return moment.astimezone().isoformat(timespec="milliseconds")


def to_log_time_ns(timestamp_ms: float, domain: str, host_receive_epoch_ns: int) -> int:
    """MCAP log_time: SDK epoch time when the domain is epoch based, host arrival time otherwise."""
    if domain in ("global_time", "system_time") and timestamp_ms > 0:
        return int(round(timestamp_ms * 1e6))
    return int(host_receive_epoch_ns)


def write_file_header(writer: Any, session_id: str, index: int, static_info: Dict[str, Any]) -> None:
    """Every MCAP file carries the session description, so a single file is usable on its own."""
    now = time.time_ns()
    writer.add_metadata("session", {
        "session_id": session_id, "device_id": str(static_info.get("device_id", "")),
        "format": FORMAT_NAME, "format_version": str(FORMAT_VERSION), "recording_index": str(index),
    })
    writer.add_attachment(create_time=now, log_time=now, name="session_info.json", media_type="application/json",
                          data=json.dumps(static_info, ensure_ascii=False).encode("utf-8"))


STATIC_INFO_KEYS = ("format", "format_version", "session_id", "device_id", "device", "streams", "extrinsics",
                    "conventions", "anchor_stream")


class McapSink:
    """Owns the MCAP files of one session. Used from the writer thread only."""

    def __init__(self, session_dir: Path, session_id: str, split_seconds: float,
                 static_info: Dict[str, Any], calibration_messages: List[Tuple[str, bytes]],
                 on_new_file: Callable[[str], None]) -> None:
        self._dir = session_dir / "recordings"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._session_dir = session_dir
        self._session_id = session_id
        self._split_seconds = split_seconds
        self._static_info = static_info
        self._calibration_messages = calibration_messages
        self._on_new_file = on_new_file
        self._schemas = Schemas.get()
        self._index = -1
        self._handle = None
        self._writer = None
        self._schema_ids: Dict[str, int] = {}
        self._channel_ids: Dict[str, int] = {}
        self._opened_at = 0.0
        self.bytes_written = 0
        self.message_counts: Dict[str, int] = {}
        self.topic_schema: Dict[str, str] = {}
        self._open_next()

    @property
    def recording_index(self) -> int:
        return self._index

    def _open_next(self) -> None:
        from mcap.writer import CompressionType, Writer

        self._close_current()
        self._index += 1
        name = "{}_{:03d}.mcap".format(self._session_id, self._index)
        path = self._dir / name
        self._handle = path.open("wb", buffering=1024 * 1024)
        # Payloads are already PNG/JPEG, so chunk compression would only burn CPU.
        self._writer = Writer(self._handle, chunk_size=4 * 1024 * 1024, compression=CompressionType.NONE)
        self._writer.start(profile="", library="record_d455.py " + __version__)
        self._schema_ids, self._channel_ids = {}, {}
        self._opened_at = time.monotonic()
        now = time.time_ns()
        write_file_header(self._writer, self._session_id, self._index, self._static_info)
        for topic, payload in self._calibration_messages:
            self.write(topic, "calibration", payload, now, now, 0, count=False)
        self._on_new_file("recordings/" + name)

    def _channel(self, topic: str, schema_key: str) -> int:
        channel_id = self._channel_ids.get(topic)
        if channel_id is None:
            name, encoding, data, message_encoding = self._schemas.defs[schema_key]
            schema_id = self._schema_ids.get(schema_key)
            if schema_id is None:
                schema_id = self._writer.register_schema(name=name, encoding=encoding, data=data)
                self._schema_ids[schema_key] = schema_id
            channel_id = self._writer.register_channel(topic=topic, message_encoding=message_encoding,
                                                       schema_id=schema_id)
            self._channel_ids[topic] = channel_id
            self.topic_schema[topic] = name
        return channel_id

    def write(self, topic: str, schema_key: str, data: bytes, log_time: int, publish_time: int,
              sequence: int, count: bool = True) -> None:
        if count and self._split_seconds > 0 and time.monotonic() - self._opened_at >= self._split_seconds:
            self._open_next()
        self._writer.add_message(channel_id=self._channel(topic, schema_key), log_time=max(0, int(log_time)),
                                 data=data, publish_time=max(0, int(publish_time)),
                                 sequence=int(sequence) & 0xFFFFFFFF)
        self.bytes_written += len(data)
        self.message_counts[topic] = self.message_counts.get(topic, 0) + 1

    def flush(self, sync: bool = False) -> None:
        if self._handle is not None:
            self._handle.flush()
            if sync:  # survive a power cut, not only a crash of this process
                try:
                    os.fsync(self._handle.fileno())
                except OSError:
                    pass

    def _close_current(self) -> None:
        if self._writer is not None:
            try:
                self._writer.finish()
            finally:
                self._writer = None
                if self._handle is not None:
                    self._handle.close()
                    self._handle = None

    def close(self) -> None:
        self._close_current()


# --------------------------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------------------------
START_CHECK_S = 5.0    # every video stream must deliver a first frame this soon after starting
START_CHECK_TRIES = 3  # starts in one attempt before the recorder gives up and retries from scratch
START_HANG_S = 40.0    # a start that takes longer is stuck inside the SDK: dump stacks, explain
START_GIVE_UP_S = 150.0  # then exit, because Ctrl+C cannot reach a thread blocked inside the SDK


@dataclass
class RecordConfig:
    out_root: Path = Path("sessions")
    streams: Tuple[str, ...] = DEFAULT_STREAMS
    depth_size: Tuple[int, int] = (848, 480)
    color_size: Tuple[int, int] = (1280, 720)
    fps: int = 30
    accel_fps: int = 0  # 0 = automatic
    gyro_fps: int = 0
    jpeg_quality: int = 90
    color_codec: str = "jpeg"
    ir_codec: str = "jpeg"
    png_level: int = 1
    duration: float = 0.0
    split_minutes: float = 10.0
    max_buffer_mb: int = 1024
    min_free_gb: float = 2.0
    emitter: str = "keep"  # keep | on | off
    option_overrides: Tuple[str, ...] = ()
    require_imu: bool = False
    preview: bool = False
    scene: Dict[str, Any] = field(default_factory=dict)
    first_frame_timeout: float = 15.0
    stall_timeout: float = 5.0
    quiet: bool = False


@dataclass
class StreamPlan:
    name: str
    kind: str
    profile: Any
    sensor_index: int
    stream_index: int
    key: Tuple[int, int]
    fps: int
    fmt: str
    width: int = 0
    height: int = 0
    exact: bool = True
    encoding: str = ""
    topic: str = ""
    info_topic: str = ""


class StreamStats:
    def __init__(self) -> None:
        self.received = 0
        self.previous_received = 0
        self.device_gaps = 0
        self.recorder_drops = 0
        self.encode_errors = 0
        self.last_frame_number: Optional[int] = None
        self.first_timestamp_ms: Optional[float] = None
        self.last_timestamp_ms: Optional[float] = None
        self.last_receive_monotonic = 0.0
        self.domains: Dict[str, int] = {}
        self.recent: deque = deque(maxlen=8)
        self.metadata: List[Tuple[str, Any]] = []
        self.has_frame_counter = False
        self.max_gap_ms = 0.0
        self.warmup_discarded = 0


def parse_size(text: str) -> Tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*[xX*]\s*(\d+)\s*", text)
    if not match:
        raise argparse.ArgumentTypeError("expected WIDTHxHEIGHT, for example 848x480")
    return int(match.group(1)), int(match.group(2))


def parse_streams(text: str, require_video: bool = True) -> Tuple[str, ...]:
    names: List[str] = []
    for token in text.replace(" ", "").lower().split(","):
        if not token:
            continue
        expanded = {"imu": ["accel", "gyro"], "ir": ["ir_left", "ir_right"], "rgb": ["color"],
                    "infrared": ["ir_left"]}.get(token, [token])
        for name in expanded:
            if name not in STREAM_DEFS:
                raise argparse.ArgumentTypeError(
                    "unknown stream '{}'; choose from depth,color,ir_left,ir_right,ir,accel,gyro,imu".format(token))
            if name not in names:
                names.append(name)
    if require_video and not any(name in VIDEO_STREAMS for name in names):
        raise argparse.ArgumentTypeError("at least one video stream is required")
    return tuple(names)


def device_summary(rs: Any, device: Any) -> Dict[str, str]:
    summary: Dict[str, str] = {}
    for key in ("name", "serial_number", "firmware_version", "recommended_firmware_version",
                "usb_type_descriptor", "product_id", "product_line", "physical_port", "imu_type",
                "connection_type", "asic_serial_number", "camera_locked", "advanced_mode"):
        item = getattr(rs.camera_info, key, None)
        if item is None:
            continue
        try:
            if device.supports(item):
                summary[key] = str(device.get_info(item))
        except Exception:
            pass
    return summary


def model_tag(name: str) -> str:
    match = re.search(r"\b([DL]\d{3}[A-Za-z]{0,2})\b", name or "")
    return match.group(1).lower() if match else "realsense"


def sensor_role(rs: Any, profiles: List[Any]) -> str:
    kinds = set()
    for profile in profiles:
        try:
            kinds.add(profile.stream_type())
        except Exception:
            pass
    if rs.stream.depth in kinds:
        return "depth"
    if rs.stream.color in kinds:
        return "color"
    if rs.stream.accel in kinds or rs.stream.gyro in kinds:
        return "motion"
    return "other"


def intrinsics_dict(intrinsics: Any) -> Dict[str, Any]:
    return {
        "width": int(intrinsics.width), "height": int(intrinsics.height),
        "fx": float(intrinsics.fx), "fy": float(intrinsics.fy),
        "ppx": float(intrinsics.ppx), "ppy": float(intrinsics.ppy),
        "model": getattr(intrinsics.model, "name", str(intrinsics.model)),
        "coeffs": [float(value) for value in intrinsics.coeffs],
    }


class Recorder:
    """Records one session from one device (a real rs.device or the simulated one)."""

    def __init__(self, rs: Any, device: Any, config: RecordConfig, stop_event: threading.Event,
                 after_start: Optional[Callable[[], None]] = None,
                 is_present: Optional[Callable[[], bool]] = None) -> None:
        import numpy

        self.rs = rs
        self.np = numpy
        self.device = device
        self.config = config
        self.stop_event = stop_event
        self.after_start = after_start
        self.is_present = is_present
        self.device_released = True
        self.events: List[Dict[str, Any]] = []
        self.info = device_summary(rs, device)
        serial = re.sub(r"[^A-Za-z0-9]", "", self.info.get("serial_number", "")) or "unknown"
        self.device_id = "{}_{}".format(model_tag(self.info.get("name", "")), serial)
        self.session_id = "{}_{}".format(datetime.now().strftime("%Y-%m-%d_%H-%M-%S"), self.device_id)
        self.session_dir = self._unique_dir(Path(config.out_root) / self.session_id)
        self.session_id = self.session_dir.name
        self.plans: List[StreamPlan] = []
        self.by_key: Dict[Tuple[int, int], StreamPlan] = {}
        self.stats: Dict[str, StreamStats] = {}
        self.sensors: List[Any] = []
        self.sensor_roles: Dict[str, Any] = {}
        self.sensor_profiles: List[List[Any]] = []
        self.active_sensors: List[Any] = []
        self.manifest: Dict[str, Any] = {}
        self.recordings: List[str] = []
        self.anchor = ""
        self.depth_units: Optional[float] = None
        self._accepting = False
        self._first_frames: Optional[set] = None  # stream names seen while the start check runs
        self._buffered = 0
        self._buffer_lock = threading.Lock()
        self._buffer_limit = int(config.max_buffer_mb) * 1024 * 1024
        self._encode_queues: Dict[str, "queue.Queue"] = {}
        self._write_queue: "queue.Queue" = queue.Queue()
        self._threads: List[threading.Thread] = []
        self._writer_thread: Optional[threading.Thread] = None
        self._sink: Optional[McapSink] = None
        self._fatal: Optional[str] = None
        self._callback_errors = 0
        self._last_callback_error = ""
        self._latest: Dict[str, Any] = {}
        self._imu_stats: Dict[str, Dict[str, Any]] = {}
        self._metadata_values: List[Tuple[str, Any]] = []
        self._motion_metadata_values: List[Tuple[str, Any]] = []
        self._started_monotonic = 0.0
        self._started_epoch = 0.0
        self._preview_ok = bool(config.preview)
        self._index_handle = None
        self._index_writer = None
        self._written_frames: Dict[str, int] = {}
        self._written_bytes: Dict[str, int] = {}

    # ---- setup ----------------------------------------------------------------------------
    @staticmethod
    def _unique_dir(path: Path) -> Path:
        candidate, suffix = path, 2
        while candidate.exists():
            candidate = path.with_name("{}_{}".format(path.name, suffix))
            suffix += 1
        return candidate

    def note(self, level: str, message: str) -> None:
        self.events.append({"time": local_iso(), "level": level, "message": message})
        if level == "warning":
            warn(message)
        elif not self.config.quiet or level == "error":
            log(message)

    def _pick_video(self, candidates: List[Tuple[int, Any]], formats: Tuple[str, ...], width: int, height: int,
                    fps: int, strict: bool = False) -> Optional[Tuple[int, Any, bool]]:
        best: Optional[Tuple[Tuple, int, Any, bool]] = None
        for sensor_index, profile in candidates:
            fmt = profile.format().name
            if fmt not in formats:
                continue
            video = profile.as_video_stream_profile()
            w, h, f = video.width(), video.height(), profile.fps()
            exact = (w, h, f) == (width, height, fps)
            if strict and not exact:
                continue
            score = (not exact, f != fps, abs(w * h - width * height), abs(f - fps), formats.index(fmt))
            if best is None or score < best[0]:
                best = (score, sensor_index, profile, exact)
        return None if best is None else (best[1], best[2], best[3])

    def plan_streams(self) -> None:
        rs, config = self.rs, self.config
        self.sensors = list(self.device.query_sensors())
        catalog: Dict[str, List[Tuple[int, Any]]] = {name: [] for name in STREAM_DEFS}
        wants_motion = any(name in MOTION_STREAMS for name in config.streams)
        for sensor_index, sensor in enumerate(self.sensors):
            try:
                label = str(sensor.get_info(rs.camera_info.name)) if sensor.supports(rs.camera_info.name) else ""
            except Exception:
                label = ""
            if "motion" in label.lower() and not wants_motion:
                self.sensor_profiles.append([])  # leave the IMU untouched (it is the fragile part on macOS)
                continue
            try:
                profiles = list(sensor.get_stream_profiles())
            except Exception as error:
                self.note("warning", "cannot list profiles of sensor {}: {}".format(sensor_index, error))
                profiles = []
            self.sensor_profiles.append(profiles)
            role = sensor_role(rs, profiles)
            self.sensor_roles.setdefault(role, sensor)
            for profile in profiles:
                for name, (attribute, index, _kind) in STREAM_DEFS.items():
                    if profile.stream_type() != getattr(rs.stream, attribute):
                        continue
                    if index is not None and profile.stream_index() != index:
                        continue
                    catalog[name].append((sensor_index, profile))

        chosen: Dict[str, Tuple[int, Any, bool]] = {}
        depth_w, depth_h = config.depth_size
        for name in config.streams:
            if name == "depth":
                pick = self._pick_video(catalog[name], ("z16",), depth_w, depth_h, config.fps)
            elif name == "color":
                pick = self._pick_video(catalog[name], ("bgr8", "rgb8", "yuyv"), config.color_size[0],
                                        config.color_size[1], config.fps)
            elif name in ("ir_left", "ir_right"):
                if "depth" in chosen:  # IR shares the stereo sensor: it must match depth exactly
                    video = chosen["depth"][1].as_video_stream_profile()
                    pick = self._pick_video(catalog[name], ("y8",), video.width(), video.height(),
                                            chosen["depth"][1].fps(), strict=True)
                else:
                    pick = self._pick_video(catalog[name], ("y8",), depth_w, depth_h, config.fps)
            else:
                wanted = config.accel_fps if name == "accel" else config.gyro_fps
                target = wanted or 200
                options = [(abs(profile.fps() - target), -profile.fps(), sensor_index, profile)
                           for sensor_index, profile in catalog[name]
                           if profile.format().name == "motion_xyz32f"]
                options.sort(key=lambda item: item[:2])
                pick = None if not options else (options[0][2], options[0][3],
                                                 not wanted or options[0][3].fps() == wanted)
            if pick is None:
                message = "stream '{}' is not available on this device with a usable profile".format(name)
                if name in MOTION_STREAMS and not config.require_imu:
                    self.note("warning", message + "; continuing without it")
                    continue
                if name in ("ir_left", "ir_right"):
                    self.note("warning", message + " matching the depth profile; continuing without it")
                    continue
                raise RuntimeError(message)
            chosen[name] = pick

        for name, (sensor_index, profile, exact) in chosen.items():
            kind = STREAM_DEFS[name][2]
            plan = StreamPlan(name=name, kind=kind, profile=profile, sensor_index=sensor_index,
                              stream_index=profile.stream_index(),
                              key=(int(profile.stream_type()), int(profile.stream_index())),
                              fps=int(profile.fps()), fmt=profile.format().name, exact=bool(exact))
            if kind == "video":
                video = profile.as_video_stream_profile()
                plan.width, plan.height = int(video.width()), int(video.height())
                plan.encoding = {"color": config.color_codec, "ir_left": config.ir_codec,
                                 "ir_right": config.ir_codec}.get(name, "png")  # depth is always lossless
                plan.topic = image_topic(self.device_id, name, plan.encoding)
                plan.info_topic = info_topic(self.device_id, name)
            else:
                plan.topic = imu_topic(self.device_id, name)
            if not exact:
                requested = ("{}x{}@{}".format(*(config.color_size if name == "color" else config.depth_size),
                                                config.fps) if kind == "video" else "auto")
                actual = ("{}x{}@{}".format(plan.width, plan.height, plan.fps) if kind == "video"
                          else "{} Hz".format(plan.fps))
                self.note("warning", "{}: requested {} is not offered on this connection, using {} {}".format(
                    name, requested, actual, plan.fmt))
            self.plans.append(plan)
            self.by_key[plan.key] = plan
            self.stats[name] = StreamStats()
        video_plans = [plan for plan in self.plans if plan.kind == "video"]
        self.anchor = next(name for name in ANCHOR_PRIORITY if any(plan.name == name for plan in video_plans))
        rates = {plan.name: plan.fps for plan in video_plans}
        if len(set(rates.values())) > 1:
            self.note("warning", "video streams run at different frame rates {}: the depth/RGB sync "
                                 "conditions are not met".format(rates))
        self._metadata_values = [(name, getattr(rs.frame_metadata_value, name)) for name in METADATA_CANDIDATES
                                 if hasattr(rs.frame_metadata_value, name)]
        self._motion_metadata_values = [(name, value) for name, value in self._metadata_values
                                        if name in MOTION_METADATA_CANDIDATES]

    def _set_option(self, role: str, option_name: str, value: float, reason: str) -> bool:
        sensor = self.sensor_roles.get(role)
        option = getattr(self.rs.option, option_name, None)
        if sensor is None or option is None:
            return False
        try:
            if not sensor.supports(option):
                return False
            sensor.set_option(option, float(value))
            self.events.append({"time": local_iso(), "level": "info",
                                "message": "set {}.{} = {} ({})".format(role, option_name, value, reason)})
            return True
        except Exception as error:
            self.note("warning", "could not set {}.{} = {}: {}".format(role, option_name, value, error))
            return False

    def apply_options(self) -> None:
        for role in ("depth", "color", "motion"):
            self._set_option(role, "global_time_enabled", 1, "epoch timestamps from the device clock")
        if any(plan.name == "color" for plan in self.plans):
            # Constant RGB frame rate; together with equal FPS this is the stated depth/RGB sync condition.
            self._set_option("color", "auto_exposure_priority", 0, "keep RGB at a constant frame rate")
        if self.config.emitter in ("on", "off"):
            self._set_option("depth", "emitter_enabled", 1 if self.config.emitter == "on" else 0, "--emitter")
        for override in self.config.option_overrides:
            match = re.fullmatch(r"(depth|color|motion)\.([A-Za-z0-9_]+)=(-?[0-9.]+)", override.strip())
            if not match:
                self.note("warning", "ignored --set '{}': expected ROLE.OPTION=VALUE, e.g. "
                                     "depth.laser_power=150".format(override))
                continue
            if not self._set_option(match.group(1), match.group(2), float(match.group(3)), "--set"):
                self.note("warning", "--set {}: option not supported by this sensor".format(override))

    def snapshot_options(self) -> Dict[str, Dict[str, float]]:
        snapshot: Dict[str, Dict[str, float]] = {}
        for role, sensor in self.sensor_roles.items():
            values: Dict[str, float] = {}
            try:
                options = list(sensor.get_supported_options())
            except Exception:
                options = []
            for option in options:
                try:
                    values[getattr(option, "name", str(option))] = float(sensor.get_option(option))
                except Exception:
                    pass
            snapshot[role] = values
        return snapshot

    def calibration(self) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        streams: Dict[str, Any] = {}
        for plan in self.plans:
            entry: Dict[str, Any] = {"fps": plan.fps, "format": plan.fmt, "stream_index": plan.stream_index,
                                     "topic": plan.topic, "exact_profile": plan.exact}
            if plan.kind == "video":
                entry.update({"width": plan.width, "height": plan.height, "encoding": plan.encoding,
                              "info_topic": plan.info_topic})
                try:
                    entry["intrinsics"] = intrinsics_dict(plan.profile.as_video_stream_profile().get_intrinsics())
                except Exception as error:
                    entry["intrinsics_error"] = str(error)
            else:
                entry["units"] = "m/s^2" if plan.name == "accel" else "rad/s"
                try:
                    motion = plan.profile.as_motion_stream_profile().get_motion_intrinsics()
                    entry["intrinsics"] = {
                        "data": [[float(value) for value in row] for row in motion.data],
                        "noise_variances": [float(value) for value in motion.noise_variances],
                        "bias_variances": [float(value) for value in motion.bias_variances],
                    }
                except Exception as error:
                    entry["intrinsics_error"] = str(error)
            streams[plan.name] = entry
        extrinsics: Dict[str, Any] = {}
        for source in self.plans:
            for target in self.plans:
                if source.name == target.name:
                    continue
                try:
                    value = source.profile.get_extrinsics_to(target.profile)
                    extrinsics["{}_to_{}".format(source.name, target.name)] = {
                        "rotation": [float(item) for item in value.rotation],
                        "translation": [float(item) for item in value.translation],
                    }
                except Exception:
                    pass
        return streams, extrinsics

    def _calibration_messages(self, streams: Dict[str, Any]) -> List[Tuple[str, bytes]]:
        schemas = Schemas.get()
        messages: List[Tuple[str, bytes]] = []
        for plan in self.plans:
            intrinsics = streams.get(plan.name, {}).get("intrinsics")
            if plan.kind != "video" or not intrinsics:
                continue
            message = schemas.CameraCalibration()
            message.frame_id = "{}_{}".format(self.device_id, plan.name)
            message.width, message.height = intrinsics["width"], intrinsics["height"]
            coeffs = intrinsics["coeffs"]
            message.distortion_model = "plumb_bob" if not any(coeffs) else intrinsics["model"]
            message.D.extend(coeffs)
            fx, fy, cx, cy = intrinsics["fx"], intrinsics["fy"], intrinsics["ppx"], intrinsics["ppy"]
            message.K.extend([fx, 0, cx, 0, fy, cy, 0, 0, 1])
            message.R.extend([1, 0, 0, 0, 1, 0, 0, 0, 1])
            message.P.extend([fx, 0, cx, 0, 0, fy, cy, 0, 0, 0, 1, 0])
            messages.append((calibration_topic(self.device_id, plan.name), message.SerializeToString()))
        return messages

    # ---- capture callback (SDK threads) ---------------------------------------------------
    def _read_metadata(self, frame: Any, stats: StreamStats, candidates: List[Tuple[str, Any]]) -> Dict[str, int]:
        count = stats.received
        if count <= 5 or count % 300 == 0:  # firmware does not attach every attribute to every frame
            known = {name for name, _value in stats.metadata}
            for name, value in candidates:
                if name not in known:
                    try:
                        if frame.supports_frame_metadata(value):
                            stats.metadata.append((name, value))
                    except Exception:
                        pass
            stats.has_frame_counter = any(name == "frame_counter" for name, _value in stats.metadata)
        result: Dict[str, int] = {}
        for name, value in stats.metadata:
            try:
                result[name] = int(frame.get_frame_metadata(value))
            except Exception:
                pass
        return result

    def _on_frame(self, frame: Any) -> None:
        receive_ns = time.time_ns()
        monotonic_ns = time.monotonic_ns()
        if not self._accepting:
            seen = self._first_frames
            if seen is not None:
                try:
                    profile = frame.get_profile()
                    plan = self.by_key.get((int(profile.stream_type()), int(profile.stream_index())))
                    if plan is not None:
                        seen.add(plan.name)
                except Exception:
                    pass
            return
        try:
            profile = frame.get_profile()
            plan = self.by_key.get((int(profile.stream_type()), int(profile.stream_index())))
            if plan is None:
                return
            stats = self.stats[plan.name]
            number = int(frame.get_frame_number())
            timestamp = float(frame.get_timestamp())
            domain = frame.get_frame_timestamp_domain().name
            if domain == "hardware_clock" and stats.received == 0 and stats.warmup_discarded < 2 * plan.fps:
                # The SDK has not fitted the device clock to host time yet (normally a few frames at most).
                stats.warmup_discarded += 1
                stats.last_receive_monotonic = monotonic_ns * 1e-9
                return
            stats.received += 1
            stats.domains[domain] = stats.domains.get(domain, 0) + 1
            if stats.last_frame_number is not None and number > stats.last_frame_number + 1:
                stats.device_gaps += number - stats.last_frame_number - 1
            if stats.last_timestamp_ms is not None:
                stats.max_gap_ms = max(stats.max_gap_ms, timestamp - stats.last_timestamp_ms)
            stats.last_frame_number = number
            if stats.first_timestamp_ms is None:
                stats.first_timestamp_ms = timestamp
            stats.last_timestamp_ms = timestamp
            stats.last_receive_monotonic = monotonic_ns * 1e-9
            if plan.kind == "motion":
                metadata = self._read_metadata(frame, stats, self._motion_metadata_values)
                data = frame.as_motion_frame().get_motion_data()
                self._write_queue.put(("imu", plan.name, plan.topic, number, timestamp, domain, receive_ns,
                                       monotonic_ns, metadata.get("frame_timestamp", -1),
                                       float(data.x), float(data.y), float(data.z)))
                return
            stats.recent.append(timestamp)
            view = self.np.asanyarray(frame.get_data())
            size = int(view.nbytes)
            with self._buffer_lock:
                if self._buffered + size > self._buffer_limit:
                    stats.recorder_drops += 1
                    return
                self._buffered += size
            pixels = view.copy()  # the SDK frame goes back to its pool as soon as this callback returns
            metadata = self._read_metadata(frame, stats, self._metadata_values)
            if plan.name == "depth" and self.depth_units is None:
                try:
                    self.depth_units = float(frame.as_depth_frame().get_units())
                except Exception:
                    self.depth_units = 0.0
            if self._preview_ok:
                self._latest[plan.name] = pixels
            self._encode_queues[plan.name].put((plan, number, timestamp, domain, receive_ns, monotonic_ns,
                                                pixels, metadata, size))
        except Exception as error:  # never let an exception escape into the SDK thread
            self._callback_errors += 1
            self._last_callback_error = repr(error)

    # ---- worker threads -------------------------------------------------------------------
    def _to_bgr(self, pixels: Any, fmt: str) -> Any:
        import cv2

        if fmt == "rgb8":
            return pixels[:, :, ::-1]
        if fmt == "yuyv":
            packed = pixels.view(self.np.uint8).reshape(pixels.shape[0], pixels.shape[1], 2)
            return cv2.cvtColor(packed, cv2.COLOR_YUV2BGR_YUY2)
        return pixels

    def _encode_loop(self, name: str) -> None:
        import cv2

        schemas = Schemas.get()
        source = self._encode_queues[name]
        frame_id = "{}_{}".format(self.device_id, name)
        is_anchor = name == self.anchor
        while True:
            item = source.get()
            if item is None:
                return
            plan, number, timestamp, domain, receive_ns, monotonic_ns, pixels, metadata, size = item
            try:
                image = self._to_bgr(pixels, plan.fmt) if name == "color" else pixels
                if plan.encoding == "jpeg":
                    ok, encoded = cv2.imencode(".jpg", self.np.ascontiguousarray(image),
                                               [cv2.IMWRITE_JPEG_QUALITY, int(self.config.jpeg_quality)])
                else:
                    ok, encoded = cv2.imencode(".png", self.np.ascontiguousarray(image),
                                               [cv2.IMWRITE_PNG_COMPRESSION, int(self.config.png_level)])
                if not ok:
                    raise RuntimeError("image encoder returned failure")
                payload = encoded.tobytes()
                log_time = to_log_time_ns(timestamp, domain, receive_ns)
                message = schemas.CompressedImage()
                message.timestamp.seconds = log_time // 1_000_000_000
                message.timestamp.nanos = log_time % 1_000_000_000
                message.frame_id = frame_id
                message.data = payload
                message.format = plan.encoding
                hardware = metadata.get("frame_timestamp", -1)
                sensor_time = metadata.get("sensor_timestamp", -1)
                exposure = metadata.get("actual_exposure", -1)
                gain = metadata.get("gain_level", -1)
                info = schemas.FrameInfo(
                    stream=name, frame_number=number, timestamp_ms=timestamp, timestamp_domain=domain,
                    host_receive_epoch_ns=receive_ns, host_receive_monotonic_ns=monotonic_ns,
                    hw_timestamp_us=hardware, sensor_timestamp_us=sensor_time, exposure_us=exposure, gain=gain,
                    width=plan.width, height=plan.height, pixel_format=plan.fmt, encoding=plan.encoding,
                    payload_bytes=len(payload), metadata_json=json.dumps(metadata, separators=(",", ":")))
                meta = None
                if is_anchor:
                    meta = json.dumps({
                        "frame_id": number, "device_timestamp_ms": log_time // 1_000_000, "anchor": name,
                        "timestamp_ms": timestamp, "timestamp_domain": domain, "hw_timestamp_us": hardware,
                    }, separators=(",", ":")).encode("utf-8")
                row = (name, number, timestamp, domain, log_time, receive_ns, monotonic_ns, hardware,
                       sensor_time, exposure, gain, len(payload))
                encoded_size = len(payload)
                with self._buffer_lock:
                    self._buffered += encoded_size - size
                size = 0
                self._write_queue.put(("frame", plan.topic, message.SerializeToString(), plan.info_topic,
                                       info.SerializeToString(), meta, log_time, receive_ns, number, row,
                                       encoded_size))
            except Exception as error:
                self.stats[name].encode_errors += 1
                self._last_callback_error = "encode {}: {!r}".format(name, error)
            finally:
                if size:
                    with self._buffer_lock:
                        self._buffered -= size

    def _write_loop(self) -> None:
        schemas = Schemas.get()
        sink = self._sink
        last_flush = time.monotonic()
        flushes = 0
        meta = meta_topic(self.device_id)
        try:
            while True:
                item = self._write_queue.get()
                if item is None:
                    break
                if self._fatal:
                    continue
                kind = item[0]
                if kind == "frame":
                    (_kind, topic, image, info_name, info, meta_payload, log_time, receive_ns, number, row,
                     encoded_size) = item
                    try:
                        sink.write(topic, "image", image, log_time, receive_ns, number)
                        sink.write(info_name, "info", info, log_time, receive_ns, number)
                        if meta_payload is not None:
                            sink.write(meta, "meta", meta_payload, log_time, receive_ns, number)
                        self._index_writer.writerow(
                            (row[0], row[1], "{:.4f}".format(row[2])) + row[3:] + (sink.recording_index,))
                        self._written_frames[row[0]] = self._written_frames.get(row[0], 0) + 1
                        self._written_bytes[row[0]] = self._written_bytes.get(row[0], 0) + encoded_size
                    finally:
                        with self._buffer_lock:
                            self._buffered -= encoded_size
                else:
                    (_kind, name, topic, number, timestamp, domain, receive_ns, monotonic_ns, hardware,
                     x, y, z) = item
                    log_time = to_log_time_ns(timestamp, domain, receive_ns)
                    sample = schemas.ImuSample(
                        stream=name, frame_number=number, timestamp_ms=timestamp, timestamp_domain=domain,
                        host_receive_epoch_ns=receive_ns, host_receive_monotonic_ns=monotonic_ns,
                        hw_timestamp_us=hardware, x=x, y=y, z=z)
                    sink.write(topic, "imu", sample.SerializeToString(), log_time, receive_ns, number)
                    entry = self._imu_stats.setdefault(name, {"count": 0, "first_ms": timestamp, "last_ms": timestamp,
                                                             "max_gap_ms": 0.0, "sum": [0.0, 0.0, 0.0]})
                    if entry["count"]:
                        entry["max_gap_ms"] = max(entry["max_gap_ms"], timestamp - entry["last_ms"])
                    entry["count"] += 1
                    entry["last_ms"] = timestamp
                    entry["sum"][0] += x
                    entry["sum"][1] += y
                    entry["sum"][2] += z
                now = time.monotonic()
                if now - last_flush >= 1.0:
                    flushes += 1
                    sink.flush(sync=flushes % 5 == 0)
                    self._index_handle.flush()
                    last_flush = now
        except Exception as error:
            self._fatal = "writer failed: {!r}".format(error)
        finally:
            try:
                sink.close()
            except Exception as error:
                self._fatal = self._fatal or "closing the MCAP file failed: {!r}".format(error)
            try:
                self._index_handle.close()
            except Exception:
                pass

    # ---- lifecycle ------------------------------------------------------------------------
    def _write_manifest(self, status: str, extra: Optional[Dict[str, Any]] = None) -> None:
        self.manifest["status"] = status
        self.manifest["recordings"] = list(self.recordings)
        self.manifest["events"] = self.events
        if extra:
            self.manifest.update(extra)
        write_json_atomic(self.session_dir / "manifest.json", self.manifest)

    def _on_new_recording(self, relative: str) -> None:
        self.recordings.append(relative)
        if self.manifest:
            try:
                self._write_manifest(self.manifest.get("status", "recording"))
            except Exception:
                pass

    def start(self) -> None:
        self.plan_streams()
        self.apply_options()
        streams, extrinsics = self.calibration()
        depth_scale = None
        depth_sensor = self.sensor_roles.get("depth")
        try:
            if depth_sensor is not None and depth_sensor.supports(self.rs.option.depth_units):
                depth_scale = round(float(depth_sensor.get_option(self.rs.option.depth_units)), 9)
        except Exception:
            depth_scale = None
        if "depth" in streams:
            streams["depth"]["depth_scale_m"] = depth_scale
        for name in ("derived", "annotations"):
            (self.session_dir / name).mkdir(parents=True, exist_ok=True)
        hand_back_ownership(Path(self.config.out_root), recursive=False)
        hand_back_ownership(self.session_dir)
        self.manifest = {
            "manifest_version": 1, "format": FORMAT_NAME, "format_version": FORMAT_VERSION,
            "session_id": self.session_id, "device_id": self.device_id, "status": "recording",
            "created_local": local_iso(), "created_epoch_ms": int(time.time() * 1000),
            "recordings": [], "annotations": [], "scene": dict(self.config.scene),
            "anchor_stream": self.anchor, "meta_topic": meta_topic(self.device_id),
            "device": self.info,
            "host": {"platform": platform.platform(), "machine": platform.machine(),
                     "python": platform.python_version(), "pyrealsense2": sdk_version(self.rs),
                     "recorder": "record_d455.py " + __version__},
            "streams": streams, "extrinsics": extrinsics,
            "conventions": {
                "extrinsics": "librealsense: rotation is column-major 3x3, translation in metres; "
                              "p_target = R * p_source + t",
                "depth": "16-bit PNG holds raw Z16 values; metres = value * depth_scale_m",
                "color": "JPEG/PNG decode with OpenCV gives BGR",
                "infrared": "8-bit grey image of the left imager, pixel-aligned with depth and exposed at the "
                            "same instant; it shows the projector dot pattern while the emitter is on",
                "imu": "accel in m/s^2, gyro in rad/s, in the depth sensor coordinate system (x right, y down, z forward)",
                "log_time": "SDK frame timestamp as epoch ns (global_time domain); host arrival time if the "
                            "frame had no epoch-based timestamp",
                "publish_time": "host clock (epoch ns) when the frame reached the recorder callback",
                "sequence": "device frame number (low 32 bits)",
                "hw_timestamp_us": "raw device clock at readout start; 32-bit, wraps every ~71.6 minutes",
                "sensor_timestamp_us": "raw device clock at the middle of the exposure",
            },
            "settings": {"jpeg_quality": self.config.jpeg_quality, "png_level": self.config.png_level,
                         "color_codec": self.config.color_codec, "ir_codec": self.config.ir_codec,
                         "split_minutes": self.config.split_minutes, "streams_requested": list(self.config.streams),
                         "fps_requested": self.config.fps, "emitter": self.config.emitter},
        }
        static_info = {key: self.manifest[key] for key in STATIC_INFO_KEYS}
        self._index_handle = (self.session_dir / "derived" / "frame_index.csv").open(
            "w", newline="", encoding="utf-8", buffering=1024 * 1024)
        self._index_writer = csv.writer(self._index_handle)
        self._index_writer.writerow(INDEX_COLUMNS)
        self._write_manifest("recording")
        self._sink = McapSink(self.session_dir, self.session_id, self.config.split_minutes * 60.0, static_info,
                              self._calibration_messages(streams), self._on_new_recording)
        for plan in self.plans:
            if plan.kind == "video":
                self._encode_queues[plan.name] = queue.Queue()
                thread = threading.Thread(target=self._encode_loop, args=(plan.name,),
                                          name="encode-" + plan.name, daemon=True)
                thread.start()
                self._threads.append(thread)
        self._writer_thread = threading.Thread(target=self._write_loop, name="mcap-writer", daemon=True)
        self._writer_thread.start()

        self._first_frames = set()
        for attempt in range(1, START_CHECK_TRIES + 1):
            self._start_sensors()
            if attempt == 1 and self.after_start:
                self.after_start()
            silent = self._wait_for_first_frames(START_CHECK_S)
            if not silent or self.stop_event.is_set():
                break
            names = ", ".join(silent)
            if attempt == START_CHECK_TRIES:
                # Nothing has been recorded yet, so the caller retries the whole start (with a camera reset).
                raise RuntimeError("no data from {} after {} starts".format(names, attempt))
            # Seen on macOS: the first color start right after the camera was taken over from the system
            # camera service delivers nothing; a second start works.
            self.note("warning", "no data from {} within {:.0f} s of starting; starting all streams again "
                                 "({} of {})".format(names, START_CHECK_S, attempt + 1, START_CHECK_TRIES))
            self._stop_device()
            self.active_sensors = []
            self._first_frames = set()
            time.sleep(1.0)
        self._first_frames = None
        self._accepting = True
        self._started_monotonic = time.monotonic()
        self._started_epoch = time.time()
        self.manifest["options"] = self.snapshot_options()
        self.manifest["streams_active"] = [plan.name for plan in self.plans]
        self._write_manifest("recording")

    def _wait_for_first_frames(self, timeout: float) -> List[str]:
        """Wait until every video stream has delivered a frame (frames are not recorded yet).
        Returns the streams that stayed silent."""
        deadline = time.monotonic() + timeout
        while True:
            seen = self._first_frames or set()
            silent = [plan.name for plan in self.plans if plan.kind == "video" and plan.name not in seen]
            if not silent or time.monotonic() > deadline or self.stop_event.is_set():
                return silent
            time.sleep(0.05)

    def _start_sensors(self) -> None:
        groups: Dict[int, List[StreamPlan]] = {}
        for plan in self.plans:
            groups.setdefault(plan.sensor_index, []).append(plan)
        for sensor_index in sorted(groups):
            plans = groups[sensor_index]
            sensor = self.sensors[sensor_index]
            motion_only = all(plan.kind == "motion" for plan in plans)
            try:
                self._open_and_start(sensor, plans)
            except Exception as error:
                dropped = [plan for plan in plans if plan.name in ("ir_left", "ir_right")]
                kept = [plan for plan in plans if plan not in dropped]
                if motion_only and not self.config.require_imu:
                    self.note("warning", "IMU could not be started ({}); continuing without IMU".format(error))
                    self._forget(plans)
                    continue
                if dropped and kept and "resolve" in str(error).lower():
                    # "Couldn't resolve requests": this firmware/USB link cannot stream that combination.
                    self.note("warning", "sensor rejected the stream combination ({}); retrying without "
                                         "infrared".format(error))
                    self._forget(dropped)
                    self._open_and_start(sensor, kept)
                else:
                    raise

    def _open_and_start(self, sensor: Any, plans: List[StreamPlan]) -> None:
        sensor.open([plan.profile for plan in plans])
        try:
            sensor.start(self._on_frame)
        except Exception:
            try:
                sensor.close()
            except Exception:
                pass
            raise
        self.active_sensors.append(sensor)

    def _forget(self, plans: List[StreamPlan]) -> None:
        for plan in plans:
            if plan in self.plans:
                self.plans.remove(plan)
            self.by_key.pop(plan.key, None)
            self.stats.pop(plan.name, None)
            pending = self._encode_queues.get(plan.name)
            if pending is not None:
                pending.put(None)

    def _device_connected(self) -> bool:
        try:
            if hasattr(self.device, "is_connected"):
                return bool(self.device.is_connected())
            if self.is_present is not None:  # SDK older than 2.56
                return bool(self.is_present())
        except Exception:
            return False
        return True

    def _status_line(self, elapsed: float, interval: float, written_delta: int) -> str:
        parts = ["{:02d}:{:02d}:{:02d}".format(int(elapsed) // 3600, int(elapsed) % 3600 // 60, int(elapsed) % 60)]
        gaps = drops = 0
        for plan in self.plans:
            stats = self.stats[plan.name]
            rate = (stats.received - stats.previous_received) / max(interval, 1e-6)
            stats.previous_received = stats.received
            parts.append("{} {:.1f}".format(plan.name, rate))
            gaps += stats.device_gaps
            drops += stats.recorder_drops + stats.encode_errors
        parts.append("lost {}/{}".format(gaps, drops))  # by the camera or USB / by this recorder
        delta = self._live_delta()
        if delta is not None:
            parts.append("color-depth {:+.2f} ms".format(delta))
        parts.append("buf {:.0f} MB".format(self._buffered / 1e6))
        parts.append("{:.1f} MB/s".format(written_delta / max(interval, 1e-6) / 1e6))
        try:
            parts.append("free {:.0f} GB".format(shutil.disk_usage(str(self.session_dir)).free / 1e9))
        except Exception:
            pass
        return " | ".join(parts)

    def _live_delta(self) -> Optional[float]:
        if "depth" not in self.stats or "color" not in self.stats:
            return None
        depth, color = list(self.stats["depth"].recent), list(self.stats["color"].recent)
        if not depth or not color:
            return None
        latest = color[-1]
        return min((latest - value for value in depth), key=abs)

    def _render_preview(self) -> None:
        import cv2

        sources = [name for name in ("color", "depth", "ir_left") if self._latest.get(name) is not None]
        if not sources:
            return
        height = 360 if len(sources) <= 2 else 300
        panels = []
        for name in sources:
            pixels = self._latest[name]
            if name == "color":
                plan = next(plan for plan in self.plans if plan.name == "color")
                shown = self.np.ascontiguousarray(self._to_bgr(pixels, plan.fmt))
            elif name == "depth":
                scale = self.depth_units or 0.001
                shown = cv2.applyColorMap(cv2.convertScaleAbs(pixels, alpha=255.0 * scale / 6.0), cv2.COLORMAP_JET)
                shown[pixels == 0] = 0  # colour scale 0..6 m, black = no measurement
            else:
                shown = cv2.cvtColor(pixels, cv2.COLOR_GRAY2BGR)
            panels.append(cv2.resize(shown, (int(shown.shape[1] * height / shown.shape[0]), height)))
        canvas = cv2.hconcat(panels) if len(panels) > 1 else panels[0]
        cv2.putText(canvas, "REC  q/Esc = stop", (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        cv2.imshow("record_d455 preview", canvas)
        key = cv2.waitKey(1) & 0xFF
        if key in (27, ord("q")):
            self.stop_event.set()

    def run(self) -> Dict[str, Any]:
        """Start, record until a stop condition, finalize. Returns the verify report."""
        reason = "user"
        try:
            watchdog = StartWatchdog(self.session_dir / "derived")
            try:
                self.start()
            finally:
                watchdog.cancel()
            log("Recording to {}".format(self.session_dir.resolve()))
            log("Streams: " + ", ".join(
                "{} {}x{}@{} {}".format(plan.name, plan.width, plan.height, plan.fps, plan.fmt)
                if plan.kind == "video" else "{} {} Hz".format(plan.name, plan.fps) for plan in self.plans))
            log("Press Ctrl+C to stop." + (" Preview window: q or Esc also stops." if self._preview_ok else ""))
            reason = self._loop()
        except Exception as error:
            reason = "error: {}".format(error)
            self.note("error", "ERROR: {}".format(error))
        return self.finalize(reason)

    def _loop(self) -> str:
        config = self.config
        last_status = time.monotonic()
        last_written = 0
        stalled_since: Optional[float] = None
        warned_silent = set()
        tty = sys.stdout.isatty()
        while True:
            if self._preview_ok:
                try:
                    self._render_preview()
                except Exception as error:
                    self._preview_ok = False
                    self.note("warning", "preview disabled: {}".format(error))
                time.sleep(0.05)
            else:
                time.sleep(0.2)
            now = time.monotonic()
            elapsed = now - self._started_monotonic
            if self.stop_event.is_set():
                return "user"
            if self._fatal:
                return "error: " + self._fatal
            if config.duration > 0 and elapsed >= config.duration:
                return "duration"
            video = [self.stats[plan.name] for plan in self.plans if plan.kind == "video"]
            newest = max((stats.last_receive_monotonic for stats in video), default=0.0)
            if newest == 0.0:
                if elapsed > config.first_frame_timeout:
                    return "error: no video frame arrived within {:.0f} s".format(config.first_frame_timeout)
            elif time.monotonic_ns() * 1e-9 - newest > config.stall_timeout:
                stalled_since = stalled_since or now
                if not self._device_connected():
                    return "device_disconnected"
                if now - stalled_since > config.stall_timeout:
                    return "stalled: no video frames for {:.0f} s".format(2 * config.stall_timeout)
            else:
                stalled_since = None
            if elapsed > 5.0:
                for plan in self.plans:
                    stats = self.stats[plan.name]
                    if stats.received == 0 and plan.name not in warned_silent:
                        warned_silent.add(plan.name)
                        self.note("warning", "no data from stream '{}' after 5 s".format(plan.name))
                    elif (stats.received and plan.name not in warned_silent
                          and now - stats.last_receive_monotonic > config.stall_timeout):
                        warned_silent.add(plan.name)
                        self.note("warning", "stream '{}' stopped delivering data {:.0f} s into the recording "
                                             "while other streams continue".format(plan.name, elapsed))
            if now - last_status >= 1.0:
                try:
                    free = shutil.disk_usage(str(self.session_dir)).free
                    if free < config.min_free_gb * 1e9:
                        return "disk_full: less than {} GB free".format(config.min_free_gb)
                except Exception:
                    pass
                written = self._sink.bytes_written if self._sink else 0
                line = self._status_line(elapsed, now - last_status, written - last_written)
                last_status, last_written = now, written
                if not config.quiet:
                    if tty:
                        width = max(40, shutil.get_terminal_size((120, 20)).columns - 1)
                        sys.stdout.write("\r" + line[:width].ljust(width))
                        sys.stdout.flush()
                    elif int(elapsed) % 5 == 0:
                        log(line)

    def _stop_device(self, timeout: float = 5.0) -> bool:
        def work() -> None:
            for sensor in self.active_sensors:
                for action in ("stop", "close"):
                    try:
                        getattr(sensor, action)()
                    except Exception:
                        pass

        thread = threading.Thread(target=work, name="stop-sensors", daemon=True)
        thread.start()
        thread.join(timeout)
        return not thread.is_alive()

    def finalize(self, reason: str) -> Dict[str, Any]:
        if sys.stdout.isatty() and not self.config.quiet:
            sys.stdout.write("\n")
        log("Stopping ({}) and closing files...".format(reason))
        self._accepting = False
        # Data first: the files are complete even if the SDK is slow to release a pulled camera.
        for pending in self._encode_queues.values():
            pending.put(None)
        for thread in self._threads:
            thread.join(120)
        if self._writer_thread is not None:
            self._write_queue.put(None)
            self._writer_thread.join(300)
        else:  # start() failed before the writer thread existed
            for closable in (self._sink, self._index_handle):
                try:
                    if closable is not None:
                        closable.close()
                except Exception:
                    pass
        self.device_released = self._stop_device()
        if self._preview_ok:
            try:
                import cv2
                cv2.destroyAllWindows()
            except Exception:
                pass
        if self._fatal and not reason.startswith("error"):
            reason = "error: " + self._fatal
        ok = reason in ("user", "duration")
        if not sum(self._written_frames.values()):
            # Nothing usable was captured (start failure, no first frame, immediate Ctrl+C): leave no clutter.
            shutil.rmtree(str(self.session_dir), ignore_errors=True)
            if self._callback_errors:
                log("Last capture error: {}".format(self._last_callback_error))
            return {"session_dir": str(self.session_dir), "stop_reason": reason, "ok": ok, "empty": True}
        duration = (time.monotonic() - self._started_monotonic) if self._started_monotonic else 0.0
        capture: Dict[str, Any] = {}
        for name, stats in self.stats.items():
            capture[name] = {
                "received": stats.received, "written": self._written_frames.get(name, 0),
                "device_frame_number_gaps": stats.device_gaps if stats.has_frame_counter or name in MOTION_STREAMS else None,
                "dropped_by_recorder": stats.recorder_drops, "encode_errors": stats.encode_errors,
                "warmup_discarded": stats.warmup_discarded,
                "timestamp_domains": stats.domains, "bytes": self._written_bytes.get(name, 0),
                "metadata_fields": [item[0] for item in stats.metadata],
            }
        for name, entry in self._imu_stats.items():
            capture.setdefault(name, {})["written"] = entry["count"]
            capture[name]["max_gap_ms"] = round(entry["max_gap_ms"], 3)
            if entry["count"]:
                capture[name]["mean"] = [round(value / entry["count"], 5) for value in entry["sum"]]
                span = entry["last_ms"] - entry["first_ms"]
                capture[name]["measured_hz"] = round((entry["count"] - 1) * 1000.0 / span, 3) if span > 0 else None
        if self.depth_units and "depth" in self.manifest.get("streams", {}):
            self.manifest["streams"]["depth"]["depth_scale_m"] = round(self.depth_units, 9)
        if self._callback_errors:
            self.note("warning", "{} frames hit a callback error, last: {}".format(
                self._callback_errors, self._last_callback_error))
        topics = {}
        if self._sink is not None:
            for topic, count in sorted(self._sink.message_counts.items()):
                topics[topic] = {"schema": self._sink.topic_schema.get(topic, ""), "message_count": count}
        report: Dict[str, Any] = {}
        try:
            report = analyze_session(self.session_dir, nominal={plan.name: plan.fps for plan in self.plans},
                                     capture=capture)
        except Exception as error:
            self.note("warning", "post-recording analysis failed: {!r}".format(error))
        self._write_manifest("complete" if ok else "interrupted", {
            "stop_reason": reason, "finished_local": local_iso(), "duration_s": round(duration, 3),
            "total_frames": self._written_frames.get(self.anchor, 0), "topics": topics,
            "capture": capture, "bytes_written": self._sink.bytes_written if self._sink else 0,
            "verify": {key: report.get(key) for key in ("result", "problems", "warnings")} if report else None,
        })
        hand_back_ownership(self.session_dir)
        report["session_dir"] = str(self.session_dir)
        report["stop_reason"] = reason
        report["ok"] = ok
        return report


# --------------------------------------------------------------------------------------------
# Reading sessions back
# --------------------------------------------------------------------------------------------
class StartWatchdog:
    """Starting the camera normally takes a few seconds (up to ~20 s with restarts). If an SDK call never
    returns, the main thread is stuck in native code and Ctrl+C cannot reach it. Then write the Python
    stacks of all threads to a file, tell the user how to take a native snapshot, and finally exit."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._watch, name="start-watchdog", daemon=True)
        self.thread.start()

    def cancel(self) -> None:
        self.done.set()

    def _watch(self) -> None:
        if self.done.wait(START_HANG_S):
            return
        path = self.folder / "start_hang_stacks.txt"
        try:
            import faulthandler
            self.folder.mkdir(parents=True, exist_ok=True)
            with path.open("w") as handle:
                faulthandler.dump_traceback(file=handle, all_threads=True)
            hand_back_ownership(path, recursive=False)
        except Exception as error:
            path = Path("(could not write stacks: {})".format(error))
        warn("starting the camera has not finished after {:.0f} s; the SDK is stuck inside a call (process id {}). "
             "Python stacks: {}".format(START_HANG_S, os.getpid(), path))
        if sys.platform == "darwin":
            log("  For a native snapshot, run in another Terminal window:\n"
                "    sudo sample {} 3 -file \"{}\"".format(os.getpid(), self.folder / "start_hang_sample.txt"))
        log("  This program exits by itself in {:.0f} s. Then unplug the camera for 10 s and plug it back in."
            .format(START_GIVE_UP_S - START_HANG_S))
        if self.done.wait(START_GIVE_UP_S - START_HANG_S):
            return
        log("Giving up: the SDK never returned. Unplug the camera for 10 s, plug it back in, and run again.")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(3)


def is_finalized(path: Path) -> bool:
    """True if the MCAP file has its footer and chunk index (the recorder closed it normally)."""
    from mcap.reader import make_reader

    with open(path, "rb") as stream:
        try:
            summary = make_reader(stream).get_summary()
            return summary is not None and len(summary.chunk_indexes) > 0
        except Exception:
            return False


def repair_recording(path: Path, session_id: str, index: int, static_info: Dict[str, Any]) -> int:
    """Rewrite an MCAP file that was never finalized (crash, kill, power loss) into an indexed one.
    Every complete message is kept. Returns the number of messages."""
    from mcap.reader import NonSeekingReader
    from mcap.writer import CompressionType, Writer

    temporary = path.with_name(path.name + ".repair")
    count = 0
    with open(path, "rb") as source, open(temporary, "wb") as target:
        writer = Writer(target, chunk_size=4 * 1024 * 1024, compression=CompressionType.NONE)
        writer.start(profile="", library="record_d455.py {} (repair)".format(__version__))
        write_file_header(writer, session_id, index, static_info)
        schema_ids: Dict[int, int] = {}
        channel_ids: Dict[int, int] = {}
        try:
            for schema, channel, message in NonSeekingReader(source).iter_messages(log_time_order=False):
                schema_id = 0
                if schema is not None:
                    if schema.id not in schema_ids:
                        schema_ids[schema.id] = writer.register_schema(name=schema.name, encoding=schema.encoding,
                                                                       data=schema.data)
                    schema_id = schema_ids[schema.id]
                if channel.id not in channel_ids:
                    channel_ids[channel.id] = writer.register_channel(
                        topic=channel.topic, message_encoding=channel.message_encoding, schema_id=schema_id,
                        metadata=dict(channel.metadata or {}))
                writer.add_message(channel_id=channel_ids[channel.id], log_time=message.log_time, data=message.data,
                                   publish_time=message.publish_time, sequence=message.sequence)
                count += 1
        except Exception:
            pass  # the torn tail of the file
        writer.finish()
    os.replace(temporary, path)
    return count


def iter_mcap(path: Path, topics: Optional[List[str]] = None) -> Iterator[Tuple[str, str, Any]]:
    """Yield (topic, schema name, message) from one MCAP file. Files that were never finalized
    (power loss, kill) are read linearly up to the last complete record."""
    from mcap.reader import NonSeekingReader, make_reader

    indexed = is_finalized(path)
    with open(path, "rb") as stream:
        if indexed:
            for schema, channel, message in make_reader(stream).iter_messages(topics=topics):
                yield channel.topic, (schema.name if schema else ""), message
            return
        try:
            for schema, channel, message in NonSeekingReader(stream).iter_messages(topics=topics,
                                                                                    log_time_order=False):
                yield channel.topic, (schema.name if schema else ""), message
        except Exception:
            return


class Session:
    """Read access to a recorded session directory.

        session = Session("sessions/2026-11-26_17-30-05_d455_1234")
        for number, time_ns, image in session.images("depth"):   # uint16 array, metres = image * depth_scale
            ...
        accel = session.imu("accel")                             # (N, 4) array: timestamp_ms, x, y, z
    """

    def __init__(self, path: Any) -> None:
        self.path = Path(path)
        manifest_path = self.path / "manifest.json"
        self.manifest: Dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8")) \
            if manifest_path.is_file() else {}
        self.recordings: List[Path] = sorted((self.path / "recordings").glob("*.mcap"))
        if not self.recordings:
            raise FileNotFoundError("no .mcap recordings under {}".format(self.path / "recordings"))
        self.device_id: str = self.manifest.get("device_id") or self._device_id_from_files()

    def _device_id_from_files(self) -> str:
        for topic, _schema, _message in iter_mcap(self.recordings[0]):
            return topic.split("/")[1]
        raise ValueError("recording has no messages")

    @property
    def depth_scale(self) -> float:
        value = (self.manifest.get("streams", {}).get("depth", {}) or {}).get("depth_scale_m")
        return round(float(value), 9) if value else 0.001

    def topics(self) -> Dict[str, str]:
        """stream name -> topic, discovered from the manifest or from the first recording."""
        found: Dict[str, str] = {}
        for name, entry in (self.manifest.get("streams") or {}).items():
            if entry.get("topic"):
                found[name] = entry["topic"]
        if found:
            return found
        reverse = {value: key for key, value in STREAM_DIR.items()}
        for topic, schema, _message in iter_mcap(self.recordings[0]):
            parts = topic.strip("/").split("/")
            if schema == "foxglove.CompressedImage" and len(parts) == 3 and parts[1] in reverse:
                found.setdefault(reverse[parts[1]], topic)
            elif schema == "realsense.ImuSample" and len(parts) == 3:
                found.setdefault(parts[2], topic)
        return found

    def raw(self, topics: Optional[List[str]] = None) -> Iterator[Tuple[int, str, str, Any]]:
        for index, recording in enumerate(self.recordings):
            for topic, schema, message in iter_mcap(recording, topics):
                yield index, topic, schema, message

    def images(self, stream: str, decode: bool = True) -> Iterator[Tuple[int, int, Any]]:
        """Yield (frame_number, log_time_ns, image). decode=False yields the PNG/JPEG bytes."""
        import cv2
        import numpy as np

        topic = self.topics()[stream]
        cls = Schemas.get().CompressedImage
        for _index, _topic, _schema, message in self.raw([topic]):
            data = cls.FromString(message.data).data
            if decode:
                data = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
            yield message.sequence, message.log_time, data

    def aligned_frames(self, streams: Optional[List[str]] = None, tolerance_ms: Optional[float] = None,
                       decode: bool = True) -> Iterator[Dict[str, Any]]:
        """Time-aligned frame sets, one per anchor (depth) frame, in time order.

        Each item is {"timestamp_ms": anchor epoch time, "frame_number": anchor frame number,
        "<stream>": image or None, "delta_ms": {"<stream>": that frame's time minus the anchor's}}.
        A stream is None when it has no frame within tolerance_ms (default: half a frame period).
        Nothing is resampled: every image is exactly as recorded."""
        import cv2
        import numpy as np

        topics = self.topics()
        names = [name for name in (streams or VIDEO_STREAMS) if name in topics and name in VIDEO_STREAMS]
        anchor = next((name for name in ANCHOR_PRIORITY if name in names), None)
        if anchor is None:
            return
        if tolerance_ms is None:
            fps = float((self.manifest.get("streams", {}).get(anchor, {}) or {}).get("fps") or 30)
            tolerance_ms = 500.0 / fps
        tolerance_ns = int(tolerance_ms * 1e6)

        def image(payload: bytes) -> Any:
            return cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_UNCHANGED) if decode else payload

        class Follower:
            def __init__(self, source: Iterator[Tuple[int, int, Any]]) -> None:
                self.source, self.previous, self.current = source, None, next(source, None)

            def nearest(self, time_ns: int) -> Optional[Tuple[int, int, Any]]:
                while self.current is not None and self.current[1] < time_ns:
                    self.previous, self.current = self.current, next(self.source, None)
                options = [item for item in (self.previous, self.current) if item is not None]
                return min(options, key=lambda item: abs(item[1] - time_ns)) if options else None

        followers = {name: Follower(self.images(name, decode=False)) for name in names if name != anchor}
        for number, time_ns, payload in self.images(anchor, decode=False):
            item: Dict[str, Any] = {"timestamp_ms": time_ns / 1e6, "frame_number": number, anchor: image(payload),
                                    "delta_ms": {}}
            for name, follower in followers.items():
                match = follower.nearest(time_ns)
                if match is not None and abs(match[1] - time_ns) <= tolerance_ns:
                    item[name] = image(match[2])
                    item["delta_ms"][name] = (match[1] - time_ns) / 1e6
                else:
                    item[name] = None
            yield item

    def frame_infos(self) -> Iterator[Tuple[int, Any]]:
        """Yield (recording index, realsense.FrameInfo) for every video frame."""
        cls = Schemas.get().FrameInfo
        for index, _topic, schema, message in self.raw():
            if schema == "realsense.FrameInfo":
                yield index, cls.FromString(message.data)

    def imu(self, stream: str) -> Any:
        import numpy as np

        topic = self.topics()[stream]
        cls = Schemas.get().ImuSample
        rows = array("d")
        for _index, _topic, _schema, message in self.raw([topic]):
            sample = cls.FromString(message.data)
            rows.extend((sample.timestamp_ms, sample.x, sample.y, sample.z))
        return np.frombuffer(rows, dtype=np.float64).reshape(-1, 4).copy() if len(rows) else np.zeros((0, 4))

    def rebuild_index(self) -> Path:
        """Recreate derived/frame_index.csv from the MCAP files (the CSV is derived data)."""
        target = self.path / "derived" / "frame_index.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        with temporary.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(INDEX_COLUMNS)
            for index, info in self.frame_infos():
                log_time = to_log_time_ns(info.timestamp_ms, info.timestamp_domain, info.host_receive_epoch_ns)
                writer.writerow((info.stream, info.frame_number, "{:.4f}".format(info.timestamp_ms),
                                 info.timestamp_domain, log_time, info.host_receive_epoch_ns,
                                 info.host_receive_monotonic_ns, info.hw_timestamp_us, info.sensor_timestamp_us,
                                 info.exposure_us, info.gain, info.payload_bytes, index))
        os.replace(temporary, target)
        return target


def interpolate_imu(samples: Any, times_ms: Any) -> Any:
    """Linear interpolation of an (N, 4) [timestamp_ms, x, y, z] IMU array at the given times."""
    import numpy as np

    times = np.asarray(times_ms, dtype=np.float64)
    return np.stack([np.interp(times, samples[:, 0], samples[:, axis]) for axis in (1, 2, 3)], axis=-1)


# --------------------------------------------------------------------------------------------
# Analysis: drops, jitter, cross-modality time differences
# --------------------------------------------------------------------------------------------
def load_index(path: Path) -> Dict[str, Dict[str, Any]]:
    import numpy as np

    columns: Dict[str, Dict[str, Any]] = {}
    domains: Dict[str, Dict[str, int]] = {}
    with path.open("r", newline="", encoding="utf-8") as stream:
        reader = csv.reader(stream)
        header = next(reader, None)
        if header is None:
            return {}
        position = {name: header.index(name) for name in INDEX_COLUMNS if name in header}
        for row in reader:
            if len(row) < len(header):
                continue  # a torn last line after a crash
            try:
                name = row[position["stream"]]
                store = columns.get(name)
                if store is None:
                    store = columns[name] = {"frame_number": array("q"), "timestamp_ms": array("d"),
                                             "log_time_ns": array("q"), "receive_ns": array("q"),
                                             "hw_us": array("q"), "sensor_us": array("q"),
                                             "exposure_us": array("q"), "bytes": array("q")}
                    domains[name] = {}
                values = (int(row[position["frame_number"]]), float(row[position["timestamp_ms"]]),
                          int(row[position["log_time_ns"]]), int(row[position["host_receive_epoch_ns"]]),
                          int(row[position["hw_timestamp_us"]]), int(row[position["sensor_timestamp_us"]]),
                          int(row[position["exposure_us"]]), int(row[position["payload_bytes"]]))
            except (ValueError, KeyError):
                continue
            for key, value in zip(("frame_number", "timestamp_ms", "log_time_ns", "receive_ns", "hw_us",
                                   "sensor_us", "exposure_us", "bytes"), values):
                store[key].append(value)
            domain = row[position["timestamp_domain"]]
            domains[name][domain] = domains[name].get(domain, 0) + 1
    result: Dict[str, Dict[str, Any]] = {}
    for name, store in columns.items():
        entry: Dict[str, Any] = {key: np.frombuffer(value, dtype=np.float64 if key == "timestamp_ms" else np.int64).copy()
                                 for key, value in store.items()}
        order = np.argsort(entry["log_time_ns"], kind="stable")
        result[name] = {key: value[order] for key, value in entry.items()}
        result[name]["domains"] = domains[name]
    return result


def _percentiles(values: Any) -> Dict[str, float]:
    import numpy as np

    if len(values) == 0:
        return {}
    return {"median": round(float(np.median(values)), 4), "p95": round(float(np.percentile(values, 95)), 4),
            "p99": round(float(np.percentile(values, 99)), 4), "max": round(float(np.max(values)), 4),
            "min": round(float(np.min(values)), 4)}


def _mid_exposure_ms(entry: Dict[str, Any]) -> Any:
    """Epoch time of the middle of the exposure, where the device reported it (NaN elsewhere)."""
    import numpy as np

    valid = (entry["hw_us"] >= 0) & (entry["sensor_us"] >= 0)
    offset = np.mod(entry["hw_us"] - entry["sensor_us"], HW_CLOCK_WRAP_US).astype(np.float64) / 1000.0
    offset[offset > 1000.0] = np.nan  # a readout later than 1 s after exposure is not a real offset
    return np.where(valid, entry["log_time_ns"] / 1e6 - offset, np.nan)


def _device_clock_delta_ms(base_hw: Any, other_hw: Any) -> Any:
    """other - base on the raw device clock (exact, independent of the epoch conversion); NaN if unknown."""
    import numpy as np

    raw = (other_hw - base_hw).astype(np.float64)
    wrapped = np.mod(raw + HW_CLOCK_WRAP_US / 2, HW_CLOCK_WRAP_US) - HW_CLOCK_WRAP_US / 2
    return np.where((base_hw >= 0) & (other_hw >= 0), wrapped / 1000.0, np.nan)


def analyze_session(session_dir: Path, nominal: Optional[Dict[str, float]] = None,
                    capture: Optional[Dict[str, Any]] = None, write: bool = True) -> Dict[str, Any]:
    """Compute drop / jitter / alignment statistics from derived/frame_index.csv and write
    derived/alignment.csv and derived/verify_report.json."""
    import numpy as np

    session_dir = Path(session_dir)
    index_path = session_dir / "derived" / "frame_index.csv"
    manifest_path = session_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    if nominal is None:
        nominal = {name: entry.get("fps", 0) for name, entry in (manifest.get("streams") or {}).items()}
    if capture is None:
        capture = manifest.get("capture") or {}
    index = load_index(index_path) if index_path.is_file() else {}
    problems: List[str] = []
    warnings: List[str] = []
    for name in VIDEO_STREAMS:
        lost = (capture.get(name) or {}).get("dropped_by_recorder", 0) + (capture.get(name) or {}).get("encode_errors", 0)
        if lost:
            warnings.append("{}: {} frames arrived from the camera but were not saved because this computer could "
                            "not encode/write fast enough (see README, 'Dropped frames')".format(name, lost))
    streams: Dict[str, Any] = {}
    for name in VIDEO_STREAMS:
        entry = index.get(name)
        if entry is None:
            if name in nominal:
                problems.append("{}: no frames recorded".format(name))
            continue
        count = int(len(entry["log_time_ns"]))
        times = entry["log_time_ns"] / 1e6
        summary: Dict[str, Any] = {"frames": count, "timestamp_domains": entry["domains"]}
        fps = float(nominal.get(name) or 0)
        if count >= 2:
            intervals = np.diff(times)
            span = float(times[-1] - times[0])
            summary["duration_s"] = round(span / 1000.0, 3)
            summary["measured_fps"] = round((count - 1) * 1000.0 / span, 4) if span > 0 else None
            summary["interval_ms"] = _percentiles(intervals)
            if fps > 0:
                period = 1000.0 / fps
                long_gaps = intervals[intervals > 1.5 * period]
                summary["missing_frames_by_time"] = int(np.sum(np.round(long_gaps / period) - 1))
                summary["interval_jitter_ms_std"] = round(float(np.std(intervals[intervals <= 1.5 * period])), 4) \
                    if np.any(intervals <= 1.5 * period) else None
                if summary["measured_fps"] and abs(summary["measured_fps"] - fps) > 0.05 * fps:
                    warnings.append("{}: measured {:.2f} fps, nominal {:.0f}".format(name, summary["measured_fps"], fps))
            numbers = entry["frame_number"]
            steps = np.diff(numbers)
            if np.all(steps > 0):
                summary["missing_frames_by_counter"] = int(np.sum(steps - 1))
            else:
                summary["missing_frames_by_counter"] = None  # counter restarted or is not from the device
        missing = summary.get("missing_frames_by_counter")
        if missing is None:
            missing = summary.get("missing_frames_by_time", 0)
        summary["missing_frames"] = int(missing or 0)
        if count and summary["missing_frames"] > max(2, 0.01 * count):
            warnings.append("{}: {} frames missing ({:.2f}%)".format(
                name, summary["missing_frames"], 100.0 * summary["missing_frames"] / (count + summary["missing_frames"])))
        latency = (entry["receive_ns"] - entry["log_time_ns"]) / 1e6
        summary["arrival_latency_ms"] = _percentiles(latency)
        epoch_based = sum(value for key, value in entry["domains"].items() if key in ("global_time",))
        if count and epoch_based < 0.98 * count:
            share = {key: value for key, value in entry["domains"].items()}
            if entry["domains"].get("system_time", 0) > 0.5 * count:
                problems.append("{}: timestamps are host arrival times (system_time {}). Per-frame metadata is "
                                "not reaching the SDK, so cross-modality alignment is only as good as USB "
                                "arrival jitter. See the README section on metadata.".format(name, share))
            else:
                warnings.append("{}: {} of {} frames are not in the global_time domain {}".format(
                    name, count - epoch_based, count, share))
        if len(latency) and (float(np.median(latency)) < -5.0 or float(np.median(latency)) > 1000.0):
            warnings.append("{}: median arrival latency {:.1f} ms is implausible; check the host clock".format(
                name, float(np.median(latency))))
        summary["bytes"] = int(np.sum(entry["bytes"]))
        if np.any(entry["exposure_us"] >= 0):
            summary["exposure_us"] = _percentiles(entry["exposure_us"][entry["exposure_us"] >= 0])
        streams[name] = summary

    # Alignment: for every anchor frame, the nearest frame of each other video stream.
    anchor = next((name for name in ANCHOR_PRIORITY if name in index), None)
    alignment: Dict[str, Any] = {"anchor": anchor}
    rows: List[List[Any]] = []
    header: List[str] = []
    if anchor:
        base = index[anchor]
        base_ms = base["log_time_ns"] / 1e6
        base_mid = _mid_exposure_ms(base)
        fps = float(nominal.get(anchor) or 0) or (streams.get(anchor, {}).get("measured_fps") or 30.0)
        tolerance = 0.5 * 1000.0 / fps
        alignment["tolerance_ms"] = round(tolerance, 3)
        header = ["index", anchor + "_frame_number", anchor + "_timestamp_ms", anchor + "_mid_exposure_ms"]
        table = [np.arange(len(base_ms)), base["frame_number"], base_ms, base_mid]
        for name in VIDEO_STREAMS:
            if name == anchor or name not in index:
                continue
            other = index[name]
            other_ms = other["log_time_ns"] / 1e6
            position = np.clip(np.searchsorted(other_ms, base_ms), 1, max(len(other_ms) - 1, 1))
            if len(other_ms) == 1:
                nearest = np.zeros(len(base_ms), dtype=np.int64)
            else:
                left, right = position - 1, position
                nearest = np.where(np.abs(other_ms[left] - base_ms) <= np.abs(other_ms[right] - base_ms), left, right)
            delta = other_ms[nearest] - base_ms
            matched = np.abs(delta) <= tolerance
            mid_delta = _mid_exposure_ms(other)[nearest] - base_mid
            clock_delta = _device_clock_delta_ms(base["hw_us"], other["hw_us"][nearest])
            good = delta[matched]
            entry = {"matched": int(np.sum(matched)), "unmatched": int(np.sum(~matched)),
                     "delta_ms": _percentiles(good), "abs_delta_ms": _percentiles(np.abs(good)),
                     "mean_delta_ms": round(float(np.mean(good)), 4) if len(good) else None}
            finite = mid_delta[matched & np.isfinite(mid_delta)]
            if len(finite):
                entry["mid_exposure_delta_ms"] = _percentiles(finite)
                entry["mid_exposure_abs_delta_ms"] = _percentiles(np.abs(finite))
            exact = clock_delta[matched & np.isfinite(clock_delta)]
            if len(exact):
                entry["device_clock_delta_ms"] = _percentiles(exact)
                entry["device_clock_abs_delta_ms"] = _percentiles(np.abs(exact))
                if len(exact) >= 0.5 * max(1, len(good)):
                    good = exact  # judge alignment on the raw device clock when the camera reports it
            alignment[name] = entry
            if len(base_ms) and entry["unmatched"] > max(2, 0.01 * len(base_ms)):
                warnings.append("{}: {} of {} {} frames have no {} frame within {:.1f} ms".format(
                    name, entry["unmatched"], len(base_ms), anchor, name, tolerance))
            limit = 1.0 if name.startswith("ir") and anchor == "depth" else 0.25 * 1000.0 / fps
            if len(good) and float(np.percentile(np.abs(good), 95)) > limit:
                warnings.append("{} vs {}: p95 |time difference| is {:.2f} ms (expected below {:.2f} ms)".format(
                    name, anchor, float(np.percentile(np.abs(good), 95)), limit))
            header += [name + "_frame_number", name + "_delta_ms", name + "_device_clock_delta_ms",
                       name + "_mid_exposure_delta_ms", name + "_matched"]
            table += [other["frame_number"][nearest], delta, clock_delta, mid_delta, matched.astype(np.int64)]
        for line in zip(*table):
            rows.append([("" if isinstance(value, float) and math.isnan(value) else
                          ("{:.4f}".format(value) if isinstance(value, (float, np.floating)) else int(value)))
                         for value in line])

    # Device clock vs epoch: how stable is the SDK's hardware-to-epoch mapping.
    clock: Dict[str, Any] = {}
    if anchor and np.any(index[anchor]["hw_us"] >= 0):
        entry = index[anchor]
        valid = entry["hw_us"] >= 0
        hardware = entry["hw_us"][valid].astype(np.float64)
        if len(hardware) >= 10:
            wraps = np.concatenate(([0], np.cumsum(np.diff(hardware) < -HW_CLOCK_WRAP_US / 2)))
            unwrapped = hardware + wraps * HW_CLOCK_WRAP_US
            epoch = entry["log_time_ns"][valid] / 1e6
            x = (unwrapped - unwrapped[0]) / 1000.0
            slope, intercept = np.polyfit(x, epoch - epoch[0], 1)
            residual = (epoch - epoch[0]) - (slope * x + intercept)
            clock = {"hw_clock_wraps": int(wraps[-1]), "drift_ppm": round((float(slope) - 1.0) * 1e6, 3),
                     "residual_ms": _percentiles(np.abs(residual))}
    imu_summary: Dict[str, Any] = {}
    for name in MOTION_STREAMS:
        entry = capture.get(name)
        if entry:
            imu_summary[name] = {key: entry.get(key) for key in ("written", "measured_hz", "max_gap_ms", "mean",
                                                                 "timestamp_domains", "device_frame_number_gaps")}
            rate = float(nominal.get(name) or 0)
            if rate and entry.get("max_gap_ms") and entry["max_gap_ms"] > 3.0 * 1000.0 / rate:
                warnings.append("{}: largest gap between samples is {:.1f} ms".format(name, entry["max_gap_ms"]))
        elif name in nominal:
            warnings.append("{}: no samples recorded".format(name))
    report = {
        "session_id": manifest.get("session_id", session_dir.name), "generated_local": local_iso(),
        "result": "FAIL" if problems else ("WARN" if warnings else "PASS"),
        "problems": problems, "warnings": warnings, "streams": streams, "alignment": alignment,
        "device_clock": clock, "imu": imu_summary,
    }
    if write:
        derived = session_dir / "derived"
        derived.mkdir(parents=True, exist_ok=True)
        if header:
            temporary = derived / "alignment.csv.tmp"
            with temporary.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(header)
                writer.writerows(rows)
            os.replace(temporary, derived / "alignment.csv")
        write_json_atomic(derived / "verify_report.json", report)
    return report


def print_report(report: Dict[str, Any]) -> None:
    log("")
    log("=== {}  [{}] ===".format(report.get("session_id", ""), report.get("result", "?")))
    for name, summary in report.get("streams", {}).items():
        interval = summary.get("interval_ms", {})
        latency = summary.get("arrival_latency_ms", {})
        log("  {:<8} {:>7} frames  {:>7} fps  missing {:<4} interval median {} ms (max {})  "
            "arrival latency median {} ms  {:.1f} MB  {}".format(
                name, summary.get("frames"), summary.get("measured_fps"), summary.get("missing_frames"),
                interval.get("median"), interval.get("max"), latency.get("median"),
                summary.get("bytes", 0) / 1e6, summary.get("timestamp_domains")))
    alignment = report.get("alignment", {})
    for name, entry in alignment.items():
        if not isinstance(entry, dict):
            continue
        absolute = entry.get("abs_delta_ms", {})
        middle = entry.get("mid_exposure_abs_delta_ms", {})
        exact = entry.get("device_clock_abs_delta_ms", {})
        log("  {} vs {}: matched {} unmatched {}  |dt| median {} ms p95 {} max {}{}{}".format(
            name, alignment.get("anchor"), entry.get("matched"), entry.get("unmatched"), absolute.get("median"),
            absolute.get("p95"), absolute.get("max"),
            "  device clock |dt| median {} p95 {}".format(exact.get("median"), exact.get("p95")) if exact else "",
            "  mid-exposure |dt| median {} p95 {}".format(middle.get("median"), middle.get("p95")) if middle else ""))
    for name, entry in report.get("imu", {}).items():
        log("  {:<8} {:>7} samples  {} Hz  max gap {} ms  mean {}".format(
            name, entry.get("written"), entry.get("measured_hz"), entry.get("max_gap_ms"), entry.get("mean")))
    clock = report.get("device_clock") or {}
    if clock:
        log("  device clock: drift {} ppm vs host, fit residual p95 {} ms, wraps {}".format(
            clock.get("drift_ppm"), clock.get("residual_ms", {}).get("p95"), clock.get("hw_clock_wraps")))
    for line in report.get("problems", []):
        log("  PROBLEM: " + line)
    for line in report.get("warnings", []):
        log("  WARNING: " + line)


# --------------------------------------------------------------------------------------------
# Simulated D455 (official rs.software_device) for the camera-free self-test
# --------------------------------------------------------------------------------------------
class SimulatedD455:
    """A software device with the D455 stream layout that feeds synthetic, timestamped frames."""

    def __init__(self, rs: Any, fps: int = 30, color_offset_ms: float = 0.0, drop_depth_every: int = 0,
                 start_near_wrap: bool = True, imu: bool = True, warmup_frames: int = 0) -> None:
        import numpy as np

        self.rs, self.np = rs, np
        self.fps = fps
        self.color_offset_ms = color_offset_ms
        self.drop_depth_every = drop_depth_every
        self.warmup_frames = warmup_frames
        self.hw_start_us = (HW_CLOCK_WRAP_US - 2_000_000) if start_near_wrap else 5_000_000
        self.device = rs.software_device()
        for key, value in (("name", "Simulated RealSense D455"), ("serial_number", "SIM000000001"),
                           ("firmware_version", "5.17.0.10"), ("recommended_firmware_version", "5.17.0.10"),
                           ("usb_type_descriptor", "3.2"), ("product_line", "D400"), ("product_id", "0B5C")):
            try:
                self.device.register_info(getattr(rs.camera_info, key), value)
            except Exception:
                pass
        self.depth_sensor = self.device.add_sensor("Stereo Module")
        self.color_sensor = self.device.add_sensor("RGB Camera")
        self.motion_sensor = self.device.add_sensor("Motion Module") if imu else None
        self.size = (848, 480)
        self.color_size = (1280, 720)
        self.profiles: Dict[str, Any] = {}
        self.profiles["depth"] = self._video(self.depth_sensor, rs.stream.depth, 0, 1, self.size, 2, rs.format.z16)
        self.profiles["ir_left"] = self._video(self.depth_sensor, rs.stream.infrared, 1, 2, self.size, 1, rs.format.y8)
        self.profiles["ir_right"] = self._video(self.depth_sensor, rs.stream.infrared, 2, 3, self.size, 1, rs.format.y8)
        self.profiles["color"] = self._video(self.color_sensor, rs.stream.color, 0, 4, self.color_size, 3, rs.format.bgr8)
        if imu:
            self.profiles["accel"] = self._motion(rs.stream.accel, 5, 200)
            self.profiles["gyro"] = self._motion(rs.stream.gyro, 6, 200)
        baseline = rs.extrinsics()
        baseline.rotation = [1, 0, 0, 0, 1, 0, 0, 0, 1]
        baseline.translation = [-0.059, 0.0, 0.0]
        for target in ("color", "ir_left", "ir_right"):
            try:
                self.profiles["depth"].register_extrinsics_to(self.profiles[target], baseline)
            except Exception:
                pass
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.sent: Dict[str, int] = {}

    def _video(self, sensor: Any, stream: Any, index: int, uid: int, size: Tuple[int, int], bpp: int, fmt: Any) -> Any:
        rs = self.rs
        intrinsics = rs.intrinsics()
        intrinsics.width, intrinsics.height = size
        intrinsics.ppx, intrinsics.ppy = size[0] / 2.0, size[1] / 2.0
        intrinsics.fx = intrinsics.fy = 0.5 * size[0]
        intrinsics.model = rs.distortion.brown_conrady
        intrinsics.coeffs = [0, 0, 0, 0, 0]
        video = rs.video_stream()
        video.type, video.index, video.uid = stream, index, uid
        video.width, video.height, video.fps, video.bpp, video.fmt = size[0], size[1], self.fps, bpp, fmt
        video.intrinsics = intrinsics
        return sensor.add_video_stream(video)

    def _motion(self, stream: Any, uid: int, rate: int) -> Any:
        rs = self.rs
        motion = rs.motion_stream()
        motion.type, motion.index, motion.uid, motion.fps, motion.fmt = stream, 0, uid, rate, rs.format.motion_xyz32f
        intrinsics = rs.motion_device_intrinsic()
        intrinsics.data = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]]
        intrinsics.noise_variances = [0, 0, 0]
        intrinsics.bias_variances = [0, 0, 0]
        motion.intrinsics = intrinsics
        return self.motion_sensor.add_motion_stream(motion)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._feed, name="simulated-d455", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)

    def _active(self, sensor: Any) -> set:
        if sensor is None:
            return set()
        try:
            return {(int(profile.stream_type()), int(profile.stream_index())) for profile in sensor.get_active_streams()}
        except Exception:
            return set()

    def _feed(self) -> None:
        rs, np = self.rs, self.np
        metadata = rs.frame_metadata_value
        active = set()
        deadline = time.monotonic() + 5.0
        while not active and time.monotonic() < deadline:
            active = self._active(self.depth_sensor) | self._active(self.color_sensor) | self._active(self.motion_sensor)
            time.sleep(0.01)

        def on(name: str) -> bool:
            profile = self.profiles.get(name)
            return profile is not None and (int(profile.stream_type()), int(profile.stream_index())) in active

        width, height = self.size
        yy, xx = np.mgrid[0:height, 0:width]
        background = (1500 + 2.0 * xx + 1.0 * yy).astype(np.float32)
        color_yy, color_xx = np.mgrid[0:self.color_size[1], 0:self.color_size[0]]
        color_base = np.stack([(color_xx * 255 // self.color_size[0]), (color_yy * 255 // self.color_size[1]),
                               np.full_like(color_xx, 90)], axis=-1).astype(np.uint8)
        generator = np.random.default_rng(7)
        period = 1.0 / self.fps
        imu_period = 1.0 / 200.0
        epoch0 = time.time()
        clock0 = time.monotonic()
        frame = imu = 0
        exposure_us = 8000
        while not self._stop.is_set():
            elapsed = time.monotonic() - clock0
            while imu * imu_period <= elapsed and self.motion_sensor is not None:
                stamp = imu * imu_period
                for name, values in (("accel", (0.05, -9.80, 0.10)), ("gyro", (0.001, -0.002, 0.0005))):
                    if not on(name):
                        continue
                    sample = rs.software_motion_frame()
                    vector = rs.vector()
                    vector.x, vector.y, vector.z = values
                    sample.data = vector
                    sample.timestamp = (epoch0 + stamp) * 1000.0
                    sample.domain = rs.timestamp_domain.global_time
                    sample.frame_number = imu
                    sample.profile = self.profiles[name].as_motion_stream_profile()
                    self.motion_sensor.set_metadata(metadata.frame_timestamp,
                                                    int(self.hw_start_us + stamp * 1e6) % HW_CLOCK_WRAP_US)
                    self.motion_sensor.on_motion_frame(sample)
                    self.sent[name] = self.sent.get(name, 0) + 1
                imu += 1
            while frame * period <= elapsed:
                stamp = frame * period
                hardware = int(self.hw_start_us + stamp * 1e6) % HW_CLOCK_WRAP_US
                x0 = int((frame * 6) % (width - 120))
                depth = background + generator.normal(0, 2.0, background.shape).astype(np.float32)
                depth[150:400, x0:x0 + 110] = 900 + 0.2 * yy[150:400, x0:x0 + 110]
                depth = depth.astype(np.uint16)
                infrared = np.clip(depth // 12, 0, 255).astype(np.uint8)
                skip_depth = self.drop_depth_every and frame and frame % self.drop_depth_every == 0
                for key, value in ((metadata.frame_timestamp, hardware),
                                   (metadata.sensor_timestamp, (hardware - exposure_us // 2) % HW_CLOCK_WRAP_US),
                                   (metadata.actual_exposure, exposure_us), (metadata.gain_level, 16),
                                   (metadata.frame_counter, frame), (metadata.frame_laser_power, 150)):
                    self.depth_sensor.set_metadata(key, value)
                if not skip_depth:
                    for name, pixels, bpp in (("depth", depth, 2), ("ir_left", infrared, 1), ("ir_right", infrared, 1)):
                        if on(name):
                            self._send_video(self.depth_sensor, name, pixels, bpp, (epoch0 + stamp) * 1000.0, frame)
                if on("color"):
                    color = color_base.copy()
                    cx0 = int((frame * 9) % (self.color_size[0] - 180))
                    color[220:600, cx0:cx0 + 170] = (40, 60, 220)
                    color_hw = int(hardware + self.color_offset_ms * 1000.0) % HW_CLOCK_WRAP_US
                    for key, value in ((metadata.frame_timestamp, color_hw),
                                       (metadata.sensor_timestamp, (color_hw - 5000) % HW_CLOCK_WRAP_US),
                                       (metadata.actual_exposure, 10000), (metadata.gain_level, 64),
                                       (metadata.frame_counter, frame)):
                        self.color_sensor.set_metadata(key, value)
                    self._send_video(self.color_sensor, "color", color, 3,
                                     (epoch0 + stamp) * 1000.0 + self.color_offset_ms, frame)
                frame += 1
            time.sleep(0.002)

    def _send_video(self, sensor: Any, name: str, pixels: Any, bpp: int, timestamp_ms: float, number: int) -> None:
        rs = self.rs
        item = rs.software_video_frame()
        item.pixels = pixels
        item.stride = pixels.shape[1] * bpp
        item.bpp = bpp
        item.timestamp = timestamp_ms
        item.domain = rs.timestamp_domain.global_time
        if number < self.warmup_frames:  # like a real camera before the SDK has fitted its clock
            item.timestamp = (self.hw_start_us % HW_CLOCK_WRAP_US) / 1000.0 + number * 1000.0 / self.fps
            item.domain = rs.timestamp_domain.hardware_clock
        item.frame_number = number
        item.profile = self.profiles[name].as_video_stream_profile()
        if name == "depth":
            try:
                item.depth_units = 0.001
            except AttributeError:  # SDK older than 2.55
                pass
        sensor.on_video_frame(item)
        self.sent[name] = self.sent.get(name, 0) + 1


class _MockProfile:
    """Stands in for rs.video_stream_profile; stream and format values are the SDK's own enums."""

    def __init__(self, stream: Any, index: int, fmt: Any, fps: int, size: Tuple[int, int], intrinsics: Any) -> None:
        self._stream, self._index, self._fmt, self._fps = stream, index, fmt, fps
        self._size, self._intrinsics = size, intrinsics
        self.extrinsics: Dict[int, Any] = {}

    def stream_type(self) -> Any:
        return self._stream

    def stream_index(self) -> int:
        return self._index

    def format(self) -> Any:
        return self._fmt

    def fps(self) -> int:
        return self._fps

    def width(self) -> int:
        return self._size[0]

    def height(self) -> int:
        return self._size[1]

    def is_video_stream_profile(self) -> bool:
        return True

    def as_video_stream_profile(self) -> "_MockProfile":
        return self

    def get_intrinsics(self) -> Any:
        return self._intrinsics

    def get_extrinsics_to(self, other: "_MockProfile") -> Any:
        if id(other) in self.extrinsics:
            return self.extrinsics[id(other)]
        raise RuntimeError("no extrinsics registered")


class _MockFrame:
    def __init__(self, profile: _MockProfile, number: int, timestamp_ms: float, domain: Any, pixels: Any,
                 metadata: Dict[int, int]) -> None:
        self._profile, self._number, self._timestamp, self._domain = profile, number, timestamp_ms, domain
        self._pixels, self._metadata = pixels, metadata

    def get_profile(self) -> _MockProfile:
        return self._profile

    def get_frame_number(self) -> int:
        return self._number

    def get_timestamp(self) -> float:
        return self._timestamp

    def get_frame_timestamp_domain(self) -> Any:
        return self._domain

    def supports_frame_metadata(self, value: Any) -> bool:
        return int(value) in self._metadata

    def get_frame_metadata(self, value: Any) -> int:
        return self._metadata[int(value)]

    def get_data(self) -> Any:
        return self._pixels

    def as_depth_frame(self) -> "_MockFrame":
        return self

    def get_units(self) -> float:
        return 0.001


class _MockSensor:
    def __init__(self, rs: Any, name: str, options: Tuple[str, ...]) -> None:
        self.rs, self.name = rs, name
        self.profiles: List[_MockProfile] = []
        self.options = {int(getattr(rs.option, option)): 0.0 for option in options if hasattr(rs.option, option)}
        self.pending: Dict[int, int] = {}
        self.active: List[_MockProfile] = []
        self.callback: Optional[Callable[[Any], None]] = None
        self.lock = threading.Lock()

    def _is_info(self, item: Any) -> bool:
        return type(item) is type(self.rs.camera_info.name)

    def supports(self, item: Any) -> bool:
        return item == self.rs.camera_info.name if self._is_info(item) else int(item) in self.options

    def get_info(self, item: Any) -> str:
        if item == self.rs.camera_info.name:
            return self.name
        raise RuntimeError("info not supported")

    def get_stream_profiles(self) -> List[_MockProfile]:
        return list(self.profiles)

    def get_supported_options(self) -> List[Any]:
        return [self.rs.option(value) for value in self.options]

    def set_option(self, option: Any, value: float) -> None:
        self.options[int(option)] = float(value)

    def get_option(self, option: Any) -> float:
        return self.options[int(option)]

    def set_metadata(self, key: Any, value: int) -> None:
        self.pending[int(key)] = int(value)

    def open(self, profiles: Any) -> None:
        self.active = list(profiles) if isinstance(profiles, (list, tuple)) else [profiles]

    def start(self, callback: Callable[[Any], None]) -> None:
        self.callback = callback

    def stop(self) -> None:
        with self.lock:
            self.callback = None

    def close(self) -> None:
        self.active = []

    def get_active_streams(self) -> List[_MockProfile]:
        return list(self.active) if self.callback is not None else []

    def deliver(self, frame: _MockFrame) -> None:
        with self.lock:
            if self.callback is not None:
                self.callback(frame)


class _MockDevice:
    def __init__(self, rs: Any, sensors: List[_MockSensor], info: Dict[str, str]) -> None:
        self.rs, self.sensors, self.info = rs, sensors, info

    @staticmethod
    def _key(item: Any) -> str:
        # Not item.name: rs.camera_info has a member called "name", which shadows the enum's name property.
        return str(item).split(".")[-1]

    def supports(self, item: Any) -> bool:
        return self._key(item) in self.info

    def get_info(self, item: Any) -> str:
        return self.info[self._key(item)]

    def query_sensors(self) -> List[_MockSensor]:
        return list(self.sensors)

    def is_connected(self) -> bool:
        return True

    def hardware_reset(self) -> None:
        pass


class PythonD455(SimulatedD455):
    """The same synthetic D455 as SimulatedD455, but the frames are plain Python objects handed straight
    to the recorder's callback. Used on macOS, where the community SDK build crashes while injecting
    software frames (malloc double free in software_sensor::on_video_frame, pyrealsense2-macosx 2.56.5).
    The recorder, encoders, MCAP writer, read-back and alignment analysis are exactly the same."""

    def __init__(self, rs: Any, fps: int = 30, color_offset_ms: float = 0.0, drop_depth_every: int = 0,
                 start_near_wrap: bool = True, imu: bool = False, warmup_frames: int = 0) -> None:
        import numpy as np

        self.rs, self.np = rs, np
        self.fps = fps
        self.color_offset_ms = color_offset_ms
        self.drop_depth_every = drop_depth_every
        self.warmup_frames = warmup_frames
        self.hw_start_us = (HW_CLOCK_WRAP_US - 2_000_000) if start_near_wrap else 5_000_000
        self.depth_sensor = _MockSensor(rs, "Stereo Module", ("global_time_enabled", "emitter_enabled"))
        self.color_sensor = _MockSensor(rs, "RGB Camera", ("global_time_enabled", "auto_exposure_priority"))
        self.motion_sensor = None  # the IMU path is covered by the SDK simulator on Windows and Linux
        self.device = _MockDevice(rs, [self.depth_sensor, self.color_sensor], {
            "name": "Simulated RealSense D455", "serial_number": "SIM000000001", "firmware_version": "5.17.0.10",
            "recommended_firmware_version": "5.17.0.10", "usb_type_descriptor": "3.2", "product_line": "D400",
            "product_id": "0B5C"})
        self.size = (848, 480)
        self.color_size = (1280, 720)
        self.profiles: Dict[str, Any] = {}
        for name, sensor, stream, index, size, fmt in (
                ("depth", self.depth_sensor, rs.stream.depth, 0, self.size, rs.format.z16),
                ("ir_left", self.depth_sensor, rs.stream.infrared, 1, self.size, rs.format.y8),
                ("ir_right", self.depth_sensor, rs.stream.infrared, 2, self.size, rs.format.y8),
                ("color", self.color_sensor, rs.stream.color, 0, self.color_size, rs.format.bgr8)):
            intrinsics = types.SimpleNamespace(width=size[0], height=size[1], ppx=size[0] / 2.0, ppy=size[1] / 2.0,
                                               fx=0.5 * size[0], fy=0.5 * size[0], model=rs.distortion.brown_conrady,
                                               coeffs=[0.0] * 5)
            profile = _MockProfile(stream, index, fmt, fps, size, intrinsics)
            sensor.profiles.append(profile)
            self.profiles[name] = profile
        baseline = types.SimpleNamespace(rotation=[1, 0, 0, 0, 1, 0, 0, 0, 1], translation=[-0.059, 0.0, 0.0])
        for target in ("color", "ir_left", "ir_right"):
            self.profiles["depth"].extrinsics[id(self.profiles[target])] = baseline
        self._thread = None
        self._stop = threading.Event()
        self.sent = {}

    def _send_video(self, sensor: Any, name: str, pixels: Any, bpp: int, timestamp_ms: float, number: int) -> None:
        domain = self.rs.timestamp_domain.global_time
        if number < self.warmup_frames:
            timestamp_ms = (self.hw_start_us % HW_CLOCK_WRAP_US) / 1000.0 + number * 1000.0 / self.fps
            domain = self.rs.timestamp_domain.hardware_clock
        sensor.deliver(_MockFrame(self.profiles[name], number, timestamp_ms, domain, pixels, dict(sensor.pending)))
        self.sent[name] = self.sent.get(name, 0) + 1


def make_simulator(rs: Any, engine: str = "auto", **kwargs: Any) -> SimulatedD455:
    """engine: 'sdk' = rs.software_device, 'python' = PythonD455, 'auto' = python on macOS, sdk elsewhere."""
    if engine == "auto":
        engine = "python" if sys.platform == "darwin" else "sdk"
    if engine == "python":
        kwargs["imu"] = False
        return PythonD455(rs, **kwargs)
    return SimulatedD455(rs, **kwargs)


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------
def import_realsense() -> Any:
    try:
        import pyrealsense2 as rs
        return rs
    except ImportError as error:
        package = "pyrealsense2-macosx" if sys.platform == "darwin" else "pyrealsense2"
        raise SystemExit("pyrealsense2 is not installed ({}).\n  pip install {} numpy opencv-python mcap protobuf".format(
            error, package))


def check_dependencies() -> None:
    missing = []
    for module, package in (("numpy", "numpy"), ("cv2", "opencv-python"), ("mcap", "mcap"),
                            ("google.protobuf", "protobuf")):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        raise SystemExit("Missing Python packages: {0}\n  pip install {0}".format(" ".join(missing)))


def platform_hints() -> List[str]:
    if sys.platform.startswith("linux"):
        return ["Linux: without the RealSense udev rules the camera is visible to lsusb but not to the SDK. "
                "Install them once (scripts/setup_udev_rules.sh of librealsense), then replug.",
                "Check with: lsusb | grep 8086"]
    if sys.platform == "darwin":
        return ["macOS 12 and newer: the SDK needs administrator rights for USB access. Run with sudo "
                "(bash run.sh does that).",
                "Errors like 'failed to set power state' or 'failed to claim usb interface' are the macOS "
                "symptom of missing rights or of another program holding the camera. Quit apps that use "
                "cameras (browser tabs, video calls, Photo Booth), unplug the D455, wait 5 s, plug it in again.",
                "Plug the camera directly into the Mac, not through a hub, with a cable that carries USB 3 data "
                "(5 Gb/s or more). Charging cables are USB 2 only: the camera then links at 480 Mb/s."]
    return ["Windows: check that the camera shows up in Device Manager under Cameras, and use a USB 3 port "
            "and the original cable.", "Close RealSense Viewer and other apps that use the camera."]


LAST_SDK_ERROR = {"text": ""}


def macos_usb_view() -> Optional[Dict[str, str]]:
    """What macOS itself lists on USB, independent of the SDK. {} = no RealSense device listed,
    None = not macOS or the system tool gave nothing."""
    if sys.platform != "darwin":
        return None
    import subprocess
    answered = False
    for data_type in ("SPUSBHostDataType", "SPUSBDataType"):
        try:
            text = subprocess.run(["system_profiler", data_type], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  universal_newlines=True, timeout=25).stdout
        except Exception:
            continue
        lines = text.splitlines()
        answered = answered or bool(text.strip())
        for position, line in enumerate(lines):
            if "realsense" in line.lower() and line.strip().endswith(":"):
                speed = next((item.split(":", 1)[1].strip() for item in lines[position + 1:position + 16]
                              if "speed" in item.lower() and ":" in item), "")
                return {"name": line.strip().rstrip(":"), "speed": speed}
    return {} if answered else None


def describe_macos_usb_view(view: Optional[Dict[str, str]]) -> List[str]:
    if view is None:
        return []
    if not view:
        return ["macOS itself lists no RealSense device on USB. This is a cable, port or accessory-permission "
                "problem, the software is not involved yet: replug, use a USB 3 data cable, and click Allow "
                "if macOS asks 'Allow accessory to connect?'."]
    lines = ["macOS lists the camera on USB: {} (link speed: {}).".format(view["name"], view["speed"] or "unknown")]
    if "480" in view.get("speed", ""):
        lines.append("480 Mb/s is a USB 2 link. Flip the USB-C plug or use a USB 3 data cable; the camera needs "
                     "5 Gb/s for the default streams.")
    return lines


def macos_iokit_camera_tree(limit: int = 90) -> List[str]:
    """The camera's IOKit subtree, reduced to node headers and the properties that say which driver or
    process owns each USB interface. Read-only."""
    import subprocess
    try:
        text = subprocess.run(["ioreg", "-r", "-c", "IOUSBHostDevice", "-l", "-w0"], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, universal_newlines=True, timeout=25).stdout
    except Exception as error:
        return ["(ioreg failed: {})".format(error)]
    blocks: List[List[str]] = []
    for line in text.splitlines():
        if line.startswith("+-o "):
            blocks.append([])
        if blocks:
            blocks[-1].append(line)
    keys = ("+-o ", "IOUserClientCreator", "UsbExclusiveOwner", "bInterfaceClass", "bInterfaceSubClass",
            "bInterfaceNumber", "bConfigurationValue", "kUSBCurrentConfiguration", "USBSpeed", "Device Speed",
            '"IOClass"', "CFBundleIdentifier", "bcdDevice", "kUSBSerialNumberString", "IOPowerManagement")
    picked: List[str] = []
    for block in blocks:
        if "realsense" not in block[0].lower():
            continue
        picked.extend(line.rstrip()[:260] for line in block if any(key in line for key in keys))
    if not picked:
        return ["(no RealSense node among {} USB host devices)".format(len(blocks))]
    if len(picked) > limit:
        picked = picked[:limit] + ["... ({} more lines)".format(len(picked) - limit)]
    return picked


def macos_interface_owners() -> List[str]:
    """One line per USB interface of the camera: who holds it right now (UVCAssistant is the macOS camera
    service; "free" means nobody, or libusb inside this process). Read-only."""
    if sys.platform != "darwin":
        return []
    import subprocess
    try:
        text = subprocess.run(["ioreg", "-r", "-c", "IOUSBHostDevice", "-l", "-w0"], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, universal_newlines=True, timeout=25).stdout
    except Exception as error:
        return ["(ioreg failed: {})".format(error)]
    device, inside, rows = "", False, []  # type: str, bool, List[List[str]]
    for line in text.splitlines():
        if line.startswith("+-o "):
            inside = "realsense" in line.lower()
            match = re.search(r"\bid (0x[0-9a-fA-F]+)", line)
            device = match.group(1) if inside and match else device
            continue
        if not inside:
            continue
        if "+-o " in line and "IOUSBHostInterface" in line:
            name = line.split("+-o ", 1)[1].split("  <class", 1)[0].split()[-1]
            rows.append([name, "free"])
        elif "UsbExclusiveOwner" in line and rows:
            rows[-1][1] = line.split("=", 1)[1].strip().strip('"')
    if not rows:
        return ["(camera not listed)"]
    return ["device {}: ".format(device) + ", ".join("{}={}".format(name, owner.split(",")[-1].strip())
                                                     for name, owner in rows)]


def macos_usb_c_ports() -> List[Dict[str, Any]]:
    """State of each USB-C port as the Mac's port controller reports it: whether a plug is detected (CC)
    and which data links are up (USB2, USB3, ...). Shows a cable without data wires, which the USB
    device list cannot: the plug is detected but no USB link ever comes up."""
    if sys.platform != "darwin":
        return []
    import subprocess
    try:
        text = subprocess.run(["ioreg", "-l", "-w0"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              universal_newlines=True, timeout=25).stdout
    except Exception:
        return []
    ports: Dict[str, Dict[str, Any]] = {}
    block: Dict[str, str] = {}

    def flush() -> None:
        name = block.get("PortDescription", "")
        if name.startswith("Port-USB-C") and "TransportsActive" in block and name not in ports:
            ports[name] = {"port": name, "plug": block.get("ConnectionActive") == "Yes",
                           "links": re.findall(r'"([^"]+)"', block.get("TransportsActive", ""))}

    for line in text.splitlines():
        if re.match(r"^[\s|]*\+-o ", line):
            flush()
            block = {}
            continue
        match = re.search(r'"(PortDescription|ConnectionActive|TransportsActive)" = (.*)$', line)
        if match and match.group(1) not in block:
            block[match.group(1)] = match.group(2).strip().strip('"')
    flush()
    return [ports[name] for name in sorted(ports)]


def describe_usb_c_ports(ports: List[Dict[str, Any]]) -> List[str]:
    lines = []
    for item in ports:
        if not item["plug"]:
            lines.append("{}: nothing plugged in".format(item["port"]))
            continue
        data = [link for link in item["links"] if link != "CC"]
        if data:
            lines.append("{}: plug detected, data links up: {}".format(item["port"], ", ".join(data)))
        else:
            lines.append("{}: plug detected but NO data link (a charger looks like this; a camera must not: "
                         "its cable has no working data wires or the plug is not fully in)".format(item["port"]))
    return lines


MACOS_SETTLE_S = 3.0


def macos_camera_device_id() -> Optional[str]:
    """macOS: the IOKit id of the RealSense USB device ("" = not listed, None = not macOS / unknown).
    The id changes whenever macOS re-creates the device (unplug, or the re-enumeration libusb triggers
    to take the camera over from the system camera service), so it tells "steady" from "just came back"."""
    if sys.platform != "darwin":
        return None
    import subprocess
    try:
        text = subprocess.run(["ioreg", "-p", "IOUSB", "-w0"], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, universal_newlines=True, timeout=10).stdout
    except Exception:
        return None
    if not text.strip():
        return None
    for line in text.splitlines():
        if "realsense" in line.lower():
            match = re.search(r"\bid (0x[0-9a-fA-F]+)", line)
            return match.group(1) if match else "listed"
    return ""


def macos_camera_on_bus() -> Optional[bool]:
    """Whether macOS lists a RealSense device on USB right now. Fast (ioreg), no SDK.
    None = not macOS or the answer is not available."""
    identity = macos_camera_device_id()
    return None if identity is None else identity != ""


class BusGate:
    """macOS: keep the SDK away from a camera that the system has not listed steadily for a few seconds.
    Opening it while it is still enumerating (or dropping on and off a bad cable) crashes inside the SDK."""

    def __init__(self, settle: float = MACOS_SETTLE_S) -> None:
        self.since: Optional[float] = None
        self.settle = settle
        self.identity: Optional[str] = None

    def ready(self) -> Optional[bool]:
        identity = macos_camera_device_id()
        if identity is None:
            return None
        if not identity:
            self.since = self.identity = None
            return False
        now = time.monotonic()
        if self.since is None or identity != self.identity:  # new device object: it was just re-created
            self.since, self.identity = now, identity
        return now - self.since >= self.settle


def wait_for_macos_usb(stop_event: Optional[threading.Event] = None, timeout: Optional[float] = None,
                       settle: float = MACOS_SETTLE_S) -> bool:
    """Block until macOS has listed the camera steadily (True), or until stop/timeout (False).
    Returns True at once on other systems."""
    gate = BusGate(settle)
    started = time.monotonic()
    announced = hinted = False
    while stop_event is None or not stop_event.is_set():
        state = gate.ready()
        if state is None or state:
            return True
        waited = time.monotonic() - started
        if timeout is not None and waited > timeout:
            return False
        if not announced and gate.since is None and waited > 1.0:
            announced = True
            log("Waiting for the camera: macOS does not list a RealSense device on USB yet. Plug it in.")
        if not hinted and gate.since is None and waited > 15.0:
            hinted = True
            log("Still not listed by macOS. The SDK is not involved at this point, so check the hardware:")
            for line in describe_usb_c_ports(macos_usb_c_ports()):
                log("  " + line)
            log("  - Plug the camera directly into the Mac, push the plug in fully, try another port.")
            log("  - Use a cable that carries USB 3 data (5 Gb/s or more). Charging cables are USB 2 only.")
            log("  - Click Allow if macOS asks 'Allow accessory to connect?'.")
        time.sleep(0.4)
    return False


HID_GUARD_SOURCE = r"""
/* hid_guard.c, built by record_d455.py on macOS.
 * The hidapi copy inside pyrealsense2-macosx (librealsense 2.54 / 2.56, third-party/hidapi/hidapi.cpp,
 * hid_open_path) calls IOHIDDeviceOpen even when IOHIDDeviceCreate returned NULL. On macOS 26/27 that
 * happens for the D455's IMU (HID) interface, and the process dies with SIGSEGV while the SDK merely
 * builds the device object. This library makes IOHIDDeviceOpen(NULL) return an error instead; hidapi
 * then returns NULL, the SDK treats the IMU as unavailable, and depth, color and infrared work. */
#include <IOKit/hid/IOHIDDevice.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static IOReturn guarded_IOHIDDeviceOpen(IOHIDDeviceRef device, IOOptionBits options) {
    if (device == NULL) {
        setenv("RS_HID_GUARD_BLOCKED", "1", 1);
        if (getenv("RS_HID_GUARD_VERBOSE")) fprintf(stderr, "[hid_guard] blocked IOHIDDeviceOpen(NULL)\n");
        return kIOReturnBadArgument;
    }
    return IOHIDDeviceOpen(device, options);
}

__attribute__((used)) static struct { const void *replacement; const void *replacee; } interposers[]
    __attribute__((section("__DATA,__interpose"))) = {
    { (const void *)(unsigned long)&guarded_IOHIDDeviceOpen, (const void *)(unsigned long)&IOHIDDeviceOpen },
};

__attribute__((constructor)) static void hid_guard_loaded(void) {
    char pid[32];
    snprintf(pid, sizeof pid, "%d", (int)getpid());   /* the pid tells a real load from an inherited variable */
    setenv("RS_HID_GUARD_LOADED", pid, 1);
    if (getenv("RS_HID_GUARD_VERBOSE")) fprintf(stderr, "[hid_guard] loaded in pid %s\n", pid);
}
"""
HID_GUARD_STATE = {"note": ""}


def hid_guard_loaded() -> bool:
    return os.environ.get("RS_HID_GUARD_LOADED") == str(os.getpid())


def hid_guard_blocked() -> bool:
    """True once the guard has stopped the SDK's IMU crash. The flag is set from C after Python started,
    so os.environ (a copy taken at startup) does not see it; ask libc directly."""
    if not hid_guard_loaded():
        return False
    try:
        import ctypes
        libc = ctypes.CDLL(None)
        libc.getenv.restype = ctypes.c_char_p
        return libc.getenv(b"RS_HID_GUARD_BLOCKED") == b"1"
    except Exception:
        return False


def macos_hid_guard_path() -> Path:
    return Path(os.path.abspath(__file__)).resolve().parent / ".venv" / "hid_guard_{}.dylib".format(
        platform.machine() or "native")


def prepare_macos_hid_guard() -> Optional[Path]:
    """Build the guard library once (needs the Xcode command line tools). Returns its path or None."""
    if sys.platform != "darwin" or os.environ.get("D455_NO_HID_GUARD"):
        return None
    import hashlib
    import subprocess
    target = macos_hid_guard_path()
    stamp = target.with_suffix(".sha256")
    digest = hashlib.sha256(HID_GUARD_SOURCE.encode("utf-8")).hexdigest()
    try:
        if target.exists() and stamp.exists() and stamp.read_text().strip() == digest:
            return target
    except OSError:
        pass
    # xcode-select -p only checks; plain xcrun would pop up an installer dialog when the tools are missing.
    if subprocess.call(["xcode-select", "-p"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        HID_GUARD_STATE["note"] = ("the Xcode command line tools are not installed, so the IMU crash guard "
                                   "could not be built. Install them once with: xcode-select --install")
        return None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="d455_hid_guard_") as folder:
            source = Path(folder) / "hid_guard.c"
            source.write_text(HID_GUARD_SOURCE, encoding="utf-8")
            built = Path(folder) / target.name
            result = subprocess.run(["xcrun", "clang", "-arch", platform.machine() or "arm64", "-dynamiclib", "-O2",
                                     "-o", str(built), str(source), "-framework", "IOKit",
                                     "-framework", "CoreFoundation"],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                                    timeout=180)
            if result.returncode != 0 or not built.exists():
                HID_GUARD_STATE["note"] = "building the IMU crash guard failed: " + result.stdout.strip()[-400:]
                return None
            # A new file (new inode) each time: overwriting a signed library in place can get the process killed.
            staged = target.with_name(target.name + ".new")
            shutil.copy2(str(built), str(staged))
            os.replace(str(staged), str(target))
        stamp.write_text(digest + "\n")
        hand_back_ownership(target, recursive=False)
        hand_back_ownership(stamp, recursive=False)
        return target
    except Exception as error:
        HID_GUARD_STATE["note"] = "building the IMU crash guard failed: {}".format(error)
        return None


def activate_macos_hid_guard() -> None:
    """Restart this process once with the guard injected (DYLD_INSERT_LIBRARIES), before the SDK loads.
    It has to be done after sudo, because sudo drops DYLD_* variables."""
    if sys.platform != "darwin" or os.environ.get("D455_NO_HID_GUARD"):
        return
    if hid_guard_loaded():
        HID_GUARD_STATE["note"] = "active"
        # Keep it out of the commands this process starts; a child Python injects it again for itself.
        guard = str(macos_hid_guard_path())
        rest = [part for part in os.environ.get("DYLD_INSERT_LIBRARIES", "").split(":") if part and part != guard]
        if rest:
            os.environ["DYLD_INSERT_LIBRARIES"] = ":".join(rest)
        else:
            os.environ.pop("DYLD_INSERT_LIBRARIES", None)
        return
    if os.environ.get("RS_HID_GUARD_TRIED") == str(os.getpid()):  # execve keeps the pid
        HID_GUARD_STATE["note"] = ("the IMU crash guard was injected but did not load (this Python build may "
                                   "ignore DYLD_INSERT_LIBRARIES)")
        return
    guard = prepare_macos_hid_guard()
    if guard is None:
        return
    env = dict(os.environ)
    env["DYLD_INSERT_LIBRARIES"] = ":".join(part for part in (str(guard), env.get("DYLD_INSERT_LIBRARIES", "")) if part)
    env["RS_HID_GUARD_TRIED"] = str(os.getpid())
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execve(sys.executable, [sys.executable] + sys.argv, env)
    except OSError as error:
        HID_GUARD_STATE["note"] = "could not restart with the IMU crash guard: {}".format(error)


def describe_hid_guard() -> str:
    note = HID_GUARD_STATE["note"]
    if hid_guard_loaded():
        return "IMU crash guard: active ({})".format(macos_hid_guard_path().name)
    return "IMU crash guard: NOT active, " + (note or "disabled")


REALSENSE_VENDOR_ID = 0x8086
REALSENSE_PRODUCT_IDS = range(0x0AD0, 0x0B80)  # D400 family incl. D455 (0x0B5C); 0x8086 alone is any Intel device
USB_HOLD_RELEASES = 100000  # far more interface releases than the SDK makes in any session


def macos_loaded_images(fragment: str) -> List[str]:
    """Paths of the native libraries loaded in this process whose path contains `fragment` (macOS)."""
    import ctypes
    libc = ctypes.CDLL(None)
    libc._dyld_image_count.restype = ctypes.c_uint32
    libc._dyld_get_image_name.restype = ctypes.c_char_p
    libc._dyld_get_image_name.argtypes = [ctypes.c_uint32]
    names = []
    for index in range(libc._dyld_image_count()):
        raw = libc._dyld_get_image_name(index)
        name = raw.decode(errors="replace") if raw else ""
        if fragment in name.lower():
            names.append(name)
    return names


class MacUsbHold:
    """macOS: take the camera over from the system once, and keep it for the whole run.

    The SDK opens and releases the camera's USB interfaces many times (every sensor power-up, many option
    queries). libusb on macOS takes a device away from its system drivers (the camera service UVCAssistant
    and the HID driver) by a capture, and keeps one counter per device and process: a claim on an interface
    that a system driver holds raises it, every release lowers it, and when it reaches zero libusb hands
    the whole device back to macOS by re-enumerating it. The camera service then takes the color interfaces
    again, and the pipes the SDK still uses for the other streams go stale ("pipe ref not recognized").
    diagnose 1.2.3 showed exactly this: each order worked in one run and failed in the next, color alone
    always failed, and some runs crashed inside libusb.

    The hold loads the same libusb the SDK uses (one copy per process, so the counter is shared), opens the
    camera, takes it over once and raises the counter far above anything the SDK releases. The counter never
    reaches zero, so nothing is handed back until this process exits; then macOS gets the camera back.
    The hold's context also keeps libusb's macOS backend alive between the SDK's short-lived contexts, which
    otherwise tear it down and build it again on every device poll."""

    def __init__(self) -> None:
        self.lib: Any = None
        self.ctx: Any = None
        self.descriptor_type: Any = None
        self.handles: Dict[int, Any] = {}
        self.refused: set = set()  # devices macOS would not hand over; not retried until they are re-created
        self.disabled = False
        self.note = ""
        self.logged = ""

    def error_name(self, code: int) -> str:
        try:
            return self.lib.libusb_error_name(int(code)).decode()
        except Exception:
            return "error {}".format(code)

    def start(self) -> bool:
        """Create the long-lived libusb context. pyrealsense2 must be imported already (it loads libusb)."""
        if self.lib is not None:
            return True
        if sys.platform != "darwin" or self.disabled:
            return False
        if os.geteuid() != 0:
            self.note = "it needs administrator rights"
            return False
        try:
            import ctypes
            copies = [path for path in macos_loaded_images("libusb") if Path(path).name.startswith("libusb-1.0")]
            copies.sort(key=lambda path: "pyrealsense2" not in path)  # the SDK's own copy first
            if not copies:
                self.note = "the SDK has no separate libusb library here"
                return False
            lib = ctypes.CDLL(copies[0])  # already loaded by the SDK: dyld returns that same copy
            pointer, integer = ctypes.c_void_p, ctypes.c_int

            class Descriptor(ctypes.Structure):
                _fields_ = [("bLength", ctypes.c_uint8), ("bDescriptorType", ctypes.c_uint8),
                            ("bcdUSB", ctypes.c_uint16), ("bDeviceClass", ctypes.c_uint8),
                            ("bDeviceSubClass", ctypes.c_uint8), ("bDeviceProtocol", ctypes.c_uint8),
                            ("bMaxPacketSize0", ctypes.c_uint8), ("idVendor", ctypes.c_uint16),
                            ("idProduct", ctypes.c_uint16), ("bcdDevice", ctypes.c_uint16),
                            ("iManufacturer", ctypes.c_uint8), ("iProduct", ctypes.c_uint8),
                            ("iSerialNumber", ctypes.c_uint8), ("bNumConfigurations", ctypes.c_uint8)]

            signatures = {
                "libusb_init": ([ctypes.POINTER(pointer)], integer),
                "libusb_get_device_list": ([pointer, ctypes.POINTER(ctypes.POINTER(pointer))], ctypes.c_ssize_t),
                "libusb_free_device_list": ([ctypes.POINTER(pointer), integer], None),
                "libusb_get_device_descriptor": ([pointer, ctypes.POINTER(Descriptor)], integer),
                "libusb_open": ([pointer, ctypes.POINTER(pointer)], integer),
                "libusb_close": ([pointer], None),
                "libusb_detach_kernel_driver": ([pointer, integer], integer),
                "libusb_kernel_driver_active": ([pointer, integer], integer),
                "libusb_error_name": ([integer], ctypes.c_char_p),
            }
            for name, (argtypes, restype) in signatures.items():
                function = getattr(lib, name)
                function.argtypes, function.restype = argtypes, restype
            self.lib = lib
            context = pointer()
            saved = os.environ.pop("LIBUSB_DEBUG", None)  # keep this context out of diagnose's libusb logs
            try:
                code = lib.libusb_init(ctypes.byref(context))
            finally:
                if saved is not None:
                    os.environ["LIBUSB_DEBUG"] = saved
            if code != 0:
                self.note = "libusb_init failed: {}".format(self.error_name(code))
                self.lib = None
                return False
        except Exception as error:
            self.lib = None
            self.note = "could not load the SDK's libusb: {}".format(error)
            return False
        self.ctx, self.descriptor_type = context, Descriptor
        if len(copies) > 1:
            self.note = "{} libusb copies are loaded, the hold uses {}".format(len(copies), copies[0])
        return True

    def interfaces_with_system_driver(self, handle: Any) -> List[int]:
        """Per interface: 1 = a system driver (or another client) holds it, 0 = free, <0 = libusb error."""
        values = []
        for interface in range(8):
            code = self.lib.libusb_kernel_driver_active(handle, interface)
            if code == -5:  # LIBUSB_ERROR_NOT_FOUND: the camera has no more interfaces
                break
            values.append(code)
        return values

    def ensure(self) -> int:
        """Take over every RealSense device that is not held yet. Returns how many were taken now."""
        if self.lib is None:
            return 0
        import ctypes
        listing = ctypes.POINTER(ctypes.c_void_p)()
        count = self.lib.libusb_get_device_list(self.ctx, ctypes.byref(listing))
        if count < 0:
            self.note = "listing USB devices failed: {}".format(self.error_name(count))
            return 0
        present, taken = set(), 0
        try:
            for index in range(count):
                device = listing[index]
                if not device:
                    continue
                descriptor = self.descriptor_type()
                if self.lib.libusb_get_device_descriptor(device, ctypes.byref(descriptor)) != 0:
                    continue
                if descriptor.idVendor != REALSENSE_VENDOR_ID or descriptor.idProduct not in REALSENSE_PRODUCT_IDS:
                    continue
                present.add(device)
                if device in self.handles or device in self.refused:
                    continue
                if self._take(device, descriptor.idProduct):
                    taken += 1
                else:
                    self.refused.add(device)
        finally:
            self.lib.libusb_free_device_list(listing, 1)
        for device in [key for key in self.handles if key not in present]:  # unplugged, or re-created by macOS
            self.lib.libusb_close(self.handles.pop(device))
        self.refused &= present
        return taken

    def _take(self, device: int, product: int) -> bool:
        import ctypes
        handle = ctypes.c_void_p()
        code = self.lib.libusb_open(device, ctypes.byref(handle))
        if code != 0:
            self.note = "could not open the camera through libusb: {}".format(self.error_name(code))
            return False
        before = self.interfaces_with_system_driver(handle)
        began = time.monotonic()
        code = self.lib.libusb_detach_kernel_driver(handle, 0)  # the one take-over (macOS captures the device)
        if code != 0:
            self.lib.libusb_close(handle)
            self.note = "macOS did not hand the camera over: {}".format(self.error_name(code))
            return False
        detach, raised = self.lib.libusb_detach_kernel_driver, 1
        while raised < USB_HOLD_RELEASES and detach(handle, 0) == 0:  # only raises the counter from here on
            raised += 1
        took = time.monotonic() - began
        after = self.interfaces_with_system_driver(handle)
        deadline = time.monotonic() + 5.0
        while 1 in after and time.monotonic() < deadline:
            time.sleep(0.25)
            after = self.interfaces_with_system_driver(handle)
        self.handles[device] = handle

        def held(values: List[int]) -> str:
            failed = [value for value in values if value < 0]
            if failed:
                return "unknown ({})".format(self.error_name(failed[0]))
            return "{} of {}".format(sum(1 for value in values if value == 1), len(values))

        self.note = ("camera {:04x}:{:04x} taken over from macOS in {:.0f} ms and held until this program ends; "
                     "interfaces held by system drivers: {} before, {} after{}".format(
                         REALSENSE_VENDOR_ID, product, took * 1000.0, held(before), held(after),
                         "" if raised == USB_HOLD_RELEASES else " (counter raised only {} times)".format(raised)))
        return True

    def describe(self) -> str:
        if self.disabled:
            return "USB hold: off (--no-usb-hold)"
        if self.handles:
            return "USB hold: active, " + self.note
        return "USB hold: NOT active" + (", " + self.note if self.note else "")


USB_HOLD = MacUsbHold()


def start_macos_usb_hold(args: argparse.Namespace) -> None:
    """macOS: create the hold's libusb context before the SDK's first context (see MacUsbHold)."""
    if sys.platform != "darwin":
        return
    if getattr(args, "no_usb_hold", False):
        USB_HOLD.disabled = True
        return
    if not USB_HOLD.start():
        log(USB_HOLD.describe())
        USB_HOLD.logged = USB_HOLD.note


def take_macos_camera(stop_event: Optional[threading.Event] = None, prefix: str = "") -> None:
    """macOS: take a newly listed camera over before the SDK opens it. Cheap when nothing changed."""
    if USB_HOLD.lib is None:
        return
    taken = USB_HOLD.ensure()
    if taken or USB_HOLD.note != USB_HOLD.logged:
        USB_HOLD.logged = USB_HOLD.note
        log(prefix + USB_HOLD.describe())
    if taken:
        wait_for_macos_usb(stop_event, timeout=15.0, settle=1.0)  # in case macOS re-created the device


def is_unprivileged_macos() -> bool:
    return sys.platform == "darwin" and hasattr(os, "geteuid") and os.geteuid() != 0


def require_admin_on_macos(args: argparse.Namespace) -> None:
    """macOS 12+: librealsense only gets USB access as root. Without it the SDK does not merely fail:
    after 'failed to set power state' its next USB enumeration segfaults inside libusb (seen on
    macOS 27 / Apple Silicon), which Python cannot catch. So elevate before the SDK touches USB."""
    if sys.platform != "darwin":
        return
    if not is_unprivileged_macos():
        activate_macos_hid_guard()
        return
    if getattr(args, "no_sudo", False):
        return
    prepare_macos_hid_guard()  # build it as the normal user, so the file does not end up owned by root
    command = ["sudo", sys.executable, os.path.abspath(sys.argv[0])] + sys.argv[1:]
    interactive = sys.stdin is not None and sys.stdin.isatty()
    if not interactive:
        import subprocess
        try:
            cached = subprocess.call(["sudo", "-n", "true"], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL) == 0
        except OSError:
            cached = False
        if not cached:
            raise SystemExit("macOS needs administrator rights to open the RealSense camera, and this is not an "
                             "interactive terminal, so the password cannot be asked for here.\n"
                             "Open Terminal and run:\n    cd \"{}\" && ".format(os.getcwd()) + " ".join(
                                 '"{}"'.format(part) if " " in part else part for part in command))
    else:
        log("macOS needs administrator rights to open the RealSense camera.")
        log("Enter this Mac's login password if asked (nothing is shown while typing).")
    sys.stdout.flush()
    try:
        os.execvp("sudo", command)
    except OSError as error:
        raise SystemExit("could not start sudo: {}".format(error))


def find_device(rs: Any, context: Any, serial: str, quiet: bool = False) -> Optional[Tuple[Any, str, str]]:
    try:
        devices = list(context.query_devices())
    except Exception as error:
        if is_unprivileged_macos():
            # Do not enumerate again: the next attempt crashes the process inside libusb.
            raise SystemExit("The RealSense SDK could not open the USB layer without administrator rights ({}).\n"
                             "Run this with sudo (bash run.sh does that).".format(error))
        if str(error) != LAST_SDK_ERROR["text"] and not quiet:
            LAST_SDK_ERROR["text"] = str(error)
            log("The SDK reported while looking for cameras: {}".format(error))
        return None
    found = []
    for device in devices:
        try:
            if not device.supports(rs.camera_info.serial_number):
                continue  # e.g. a camera in firmware-update mode
            number = device.get_info(rs.camera_info.serial_number)
            name = device.get_info(rs.camera_info.name)
        except Exception:
            continue
        if serial and number != serial:
            continue
        found.append((device, name, number))
    if not found:
        return None
    found.sort(key=lambda item: "D455" not in item[1])
    if len(found) > 1 and not quiet:
        warn("{} RealSense cameras are connected; using {} ({}). Unplug the others or pass --serial.".format(
            len(found), found[0][1], found[0][2]))
    return found[0]


def wait_for_device(rs: Any, context: Any, serial: str, stop_event: threading.Event) -> Optional[Tuple[Any, str, str]]:
    started = time.monotonic()
    announced = hinted = False
    gate = BusGate()
    while not stop_event.is_set():
        ready = gate.ready()
        if ready is not False:
            take_macos_camera(stop_event)  # before the SDK opens a camera that just appeared
        found = None if ready is False else find_device(rs, context, serial)
        if found:
            if announced:
                time.sleep(1.5)  # let a freshly plugged camera finish enumerating all its interfaces
                found = find_device(rs, context, serial, quiet=True) or found
            return found
        settling = ready is False and gate.since is not None  # listed by macOS, just not steady for long yet
        if not announced and not settling:
            log("Waiting for a RealSense D455{}... plug it into a USB 3 port.".format(
                " with serial " + serial if serial else ""))
            announced = True
        if not hinted and time.monotonic() - started > 15.0:
            hinted = True
            log("Still no camera visible to the SDK.")
            for line in describe_macos_usb_view(macos_usb_view()):
                log("  " + line)
            log("Things to check:")
            for hint in platform_hints():
                log("  - " + hint)
            if sys.platform == "darwin":
                log("  - For a full report run:  bash run.sh diagnose")
        time.sleep(0.5)
    return None


def describe_device(rs: Any, device: Any, name: str, serial: str) -> None:
    info = device_summary(rs, device)
    usb = info.get("usb_type_descriptor", "?")
    log("Camera: {}  serial {}  firmware {}  USB {}".format(name, serial, info.get("firmware_version", "?"), usb))
    if "D455" not in name:
        warn("this is not a D455; recording continues with the streams it offers")
    if sys.platform == "darwin":
        log(describe_hid_guard() + (", it caught the SDK's IMU open failure (IMU unavailable)" if hid_guard_blocked()
                                    else "" if hid_guard_loaded() else ". The SDK may crash while opening the IMU."))
    if usb.startswith("2"):
        warn("the camera is on a USB 2 link. Resolutions and frame rates are limited and frames will be "
             "dropped. Use a USB 3 port and a USB 3 cable.")
    recommended = info.get("recommended_firmware_version")
    if recommended and recommended != info.get("firmware_version"):
        log("Note: firmware {} is installed, the SDK recommends {}.".format(info.get("firmware_version"), recommended))
    if sys.platform == "win32" and hasattr(device, "is_metadata_enabled"):
        try:
            if not device.is_metadata_enabled():
                warn("per-frame metadata is not enabled for this camera on this PC, so timestamps would fall "
                     "back to host arrival time. Run once from an Administrator terminal:\n"
                     "    python record_d455.py enable-metadata")
        except Exception:
            pass


def install_signal_handlers(stop_event: threading.Event) -> None:
    def handler(_signum: int, _frame: Any) -> None:
        if stop_event.is_set():
            log("\nSecond interrupt: exiting immediately. The last MCAP file may be missing its index; "
                "'verify' can still read it.")
            os._exit(130)
        stop_event.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), handler)
            except (ValueError, OSError):
                pass


def config_from_args(args: argparse.Namespace) -> RecordConfig:
    scene = {key: value for key, value in (("location", args.location), ("activity", args.activity),
                                           ("notes", args.note)) if value}
    if args.distance_m is not None:
        scene["distance_m"] = args.distance_m
    return RecordConfig(
        out_root=Path(args.out), streams=args.streams, depth_size=args.depth_size, color_size=args.color_size,
        fps=args.fps, accel_fps=args.accel_fps, gyro_fps=args.gyro_fps, jpeg_quality=args.jpeg_quality,
        color_codec=args.color_codec, ir_codec=args.ir_codec, png_level=args.png_level, duration=args.duration,
        split_minutes=args.split_minutes, max_buffer_mb=args.max_buffer_mb, min_free_gb=args.min_free_gb,
        emitter=args.emitter, option_overrides=tuple(args.set or ()), require_imu=args.require_imu,
        preview=args.preview, scene=scene, quiet=args.quiet)


def reset_device(device: Any) -> None:
    try:
        log("Sending a hardware reset to the camera (it reconnects after a few seconds)...")
        device.hardware_reset()
    except Exception as error:
        log("  hardware reset not possible: {}".format(error))


def command_record(args: argparse.Namespace) -> int:
    check_dependencies()
    rs = import_realsense()
    stop_event = threading.Event()
    install_signal_handlers(stop_event)
    config = config_from_args(args)
    context = None
    if not args.simulate:
        require_admin_on_macos(args)
        if sys.platform == "darwin":
            log("process id {} (for troubleshooting)".format(os.getpid()))
        if getattr(args, "sdk_log", False):
            folder = Path(os.path.abspath(__file__)).resolve().parent / "logs"
            folder.mkdir(parents=True, exist_ok=True)
            sdk_log = folder / "record_{}_sdk.log".format(datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
            try:
                rs.log_to_file(rs.log_severity.debug, str(sdk_log))
                hand_back_ownership(folder, recursive=False)
                log("SDK debug log: {}".format(sdk_log))
            except Exception as error:
                log("could not enable the SDK log: {}".format(error))
        start_macos_usb_hold(args)
        if wait_for_macos_usb(stop_event):
            take_macos_camera(stop_event)
            context = rs.context()
    for leftover in unfinished_sessions(Path(config.out_root)):
        log("Note: session {} was never closed (crash or power loss). Recover it with:\n"
            "    python record_d455.py repair \"{}\"".format(leftover.name, leftover))
    release_awake = keep_awake()
    exit_code = 0
    hung = False
    failures = 0
    while not stop_event.is_set():
        simulator = None
        if args.simulate:
            simulator = make_simulator(rs, args.engine, fps=config.fps,
                                       imu=any(name in MOTION_STREAMS for name in config.streams))
            device, name, serial = simulator.device, "Simulated RealSense D455", "SIM000000001"
            recorder = Recorder(rs, device, config, stop_event, after_start=simulator.start)
        else:
            found = wait_for_device(rs, context, args.serial, stop_event)
            if not found:
                break
            device, name, serial = found
            describe_device(rs, device, name, serial)
            recorder = Recorder(rs, device, config, stop_event,
                                is_present=lambda: find_device(rs, context, serial, quiet=True) is not None)
        report = recorder.run()
        if simulator is not None:
            simulator.stop()
        hung = hung or not recorder.device_released
        if report.get("empty"):
            if stop_event.is_set():
                log("Stopped before any frame was recorded.")
                break
            failures += 1
            if failures > max(0, args.start_retries) or args.simulate:
                exit_code = 1
                log("")
                log("Could not start recording: {}".format(report.get("stop_reason")))
                for hint in platform_hints():
                    log("  - " + hint)
                break
            log("Start failed ({}). Retry {} of {}...".format(report.get("stop_reason"), failures, args.start_retries))
            if failures >= 2:
                reset_device(device)
            stop_event.wait(6.0 if failures >= 2 else 3.0)
            continue
        failures = 0
        print_report(report)
        log("")
        log("Session: {}".format(Path(report["session_dir"]).resolve()))
        if not report.get("ok"):
            exit_code = 1
            log("Session ended early: {}. Everything received up to that point is saved.".format(
                report.get("stop_reason")))
        elif report.get("result") == "FAIL":
            exit_code = 2
        if not args.loop or stop_event.is_set() or args.simulate:
            break
        log("")
        log("--loop: unplug this camera and plug in the next one (Ctrl+C to quit).")
        while not stop_event.is_set() and find_device(rs, context, serial, quiet=True):
            time.sleep(0.5)
    release_awake()
    if hung or sys.platform == "darwin":
        # All files are closed. Skip the SDK teardown: it blocks on a vanished camera, and on macOS
        # librealsense is known to abort in its USB cleanup at interpreter exit ("mutex lock failed").
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(exit_code)
    return exit_code


def command_selftest(args: argparse.Namespace) -> int:
    check_dependencies()
    rs = import_realsense()
    root = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="record_d455_selftest_"))
    # The SDK's own frame injection (rs.software_device) aborts inside librealsense on macOS (malloc double
    # free in software_sensor::on_video_frame and on_motion_frame, pyrealsense2-macosx 2.56.5), so the
    # macOS self-test feeds Python frames to the same recorder. The IMU path is opt-in and SDK-only.
    simulator = make_simulator(rs, args.engine, fps=30, color_offset_ms=args.color_offset_ms,
                               drop_depth_every=args.drop_every, imu=args.imu)
    engine = "SDK software device" if type(simulator) is SimulatedD455 else "Python frames, SDK not involved"
    log("Self-test with a simulated D455 ({}; pyrealsense2 {}), {} s, output under {}".format(
        engine, sdk_version(rs), args.duration, root))
    if args.imu and simulator.motion_sensor is None:
        log("Note: --imu needs the SDK engine (--engine sdk); this run covers depth, color and infrared.")
    streams = ("depth", "color", "ir_left", "ir_right") + (("accel", "gyro") if simulator.motion_sensor else ())
    config = RecordConfig(out_root=root, streams=streams, duration=args.duration,
                          split_minutes=args.split_minutes, quiet=True)
    stop_event = threading.Event()
    recorder = Recorder(rs, simulator.device, config, stop_event, after_start=simulator.start)
    report = recorder.run()
    simulator.stop()
    print_report(report)
    failures: List[str] = []
    session = Session(report["session_dir"])
    counts: Dict[str, int] = {}
    decoded = {"depth": None, "color": None}
    for _index, topic, _schema, _message in session.raw():
        counts[topic] = counts.get(topic, 0) + 1
    for stream in ("depth", "color"):
        for _number, _time, image in session.images(stream):
            decoded[stream] = image
            break
    expected = max(1, int(args.duration * 30 * 0.6))
    for stream, topic in session.topics().items():
        minimum = expected * (6 if stream in MOTION_STREAMS else 1)
        if counts.get(topic, 0) < minimum:
            failures.append("{}: only {} messages on {} (expected at least {})".format(
                stream, counts.get(topic, 0), topic, minimum))
    if decoded["depth"] is None or str(decoded["depth"].dtype) != "uint16" or decoded["depth"].shape != (480, 848):
        failures.append("depth PNG did not decode to a 480x848 uint16 image")
    if decoded["color"] is None or decoded["color"].shape != (720, 1280, 3):
        failures.append("color image did not decode to 720x1280x3")
    if not report.get("ok"):
        failures.append("recording stopped early: {}".format(report.get("stop_reason")))
    if report.get("result") == "FAIL":
        failures.extend(report.get("problems", []))
    if abs(args.color_offset_ms) < 1e-9 and not args.drop_every and report.get("warnings"):
        failures.extend("unexpected warning: " + line for line in report["warnings"])
    color = report.get("alignment", {}).get("color", {})
    measured = color.get("mean_delta_ms")
    if measured is None or abs(measured - args.color_offset_ms) > 0.05:
        failures.append("color-depth offset: injected {} ms, measured {}".format(args.color_offset_ms, measured))
    log("")
    if failures:
        log("SELF-TEST FAILED")
        for line in failures:
            log("  - " + line)
        log("Output kept at {}".format(report["session_dir"]))
        return 1
    log("SELF-TEST PASSED: capture path, MCAP writing, read-back, decoding and alignment analysis all work "
        "on this machine.")
    if args.out:
        log("Output kept at {}".format(report["session_dir"]))
    else:
        shutil.rmtree(root, ignore_errors=True)
    return 0


def command_verify(args: argparse.Namespace) -> int:
    check_dependencies()
    worst = 0
    for target in args.session:
        directory = Path(target)
        session = Session(directory)
        index_path = directory / "derived" / "frame_index.csv"
        if args.deep or not index_path.is_file():
            log("Reading every MCAP message of {} (this reads the whole recording)...".format(directory.name))
            session.rebuild_index()
        nominal = {name: entry.get("fps", 0) for name, entry in (session.manifest.get("streams") or {}).items()}
        imu = None
        if args.deep:
            import numpy as np
            imu = {}
            for name in MOTION_STREAMS:
                if name in session.topics():
                    samples = session.imu(name)
                    if len(samples) > 1:
                        gaps = np.diff(samples[:, 0])
                        span = samples[-1, 0] - samples[0, 0]
                        imu[name] = {"written": int(len(samples)), "max_gap_ms": round(float(gaps.max()), 3),
                                     "measured_hz": round((len(samples) - 1) * 1000.0 / span, 3) if span > 0 else None,
                                     "mean": [round(float(value), 5) for value in samples[:, 1:].mean(axis=0)]}
        capture = dict(session.manifest.get("capture") or {})
        capture.update(imu or {})
        report = analyze_session(directory, nominal=nominal or None, capture=capture)
        if args.deep:
            counts: Dict[str, int] = {}
            for _index, topic, _schema, _message in session.raw():
                counts[topic] = counts.get(topic, 0) + 1
            report["message_counts"] = counts
            write_json_atomic(directory / "derived" / "verify_report.json", report)
            slack = 0 if session.manifest.get("status") == "complete" else 1  # a killed recorder tears the tail
            for stream, topic in session.topics().items():
                indexed = report.get("streams", {}).get(stream, {}).get("frames")
                if indexed is not None and abs(indexed - counts.get(topic, 0)) > slack:
                    report.setdefault("problems", []).append("{}: {} image messages but {} index rows".format(
                        stream, counts.get(topic, 0), indexed))
                    report["result"] = "FAIL"
        print_report(report)
        status = session.manifest.get("status")
        if status == "recording":
            log("  NOTE: this session was never closed (the recorder was killed or is still running). "
                "To finalize it run:  python record_d455.py repair \"{}\"".format(directory))
        elif status not in (None, "complete"):
            log("  NOTE: manifest status is '{}' ({})".format(status, session.manifest.get("stop_reason", "")))
        worst = max(worst, {"PASS": 0, "WARN": 0, "FAIL": 2}.get(report.get("result", "FAIL"), 2))
    return worst


def session_is_live(directory: Path, quiet_seconds: float = 15.0) -> bool:
    newest = max((path.stat().st_mtime for path in (directory / "recordings").glob("*.mcap")), default=0.0)
    return time.time() - newest < quiet_seconds


def unfinished_sessions(root: Path) -> List[Path]:
    found = []
    try:
        candidates = sorted(path for path in root.iterdir() if (path / "manifest.json").is_file())
    except OSError:
        return found
    for directory in candidates[-200:]:
        try:
            status = json.loads((directory / "manifest.json").read_text(encoding="utf-8")).get("status")
        except Exception:
            continue
        if status == "recording" and not session_is_live(directory):
            found.append(directory)
    return found


def command_repair(args: argparse.Namespace) -> int:
    check_dependencies()
    worst = 0
    for target in args.session:
        directory = Path(target)
        session = Session(directory)
        if session_is_live(directory) and not args.force:
            log("{}: files changed in the last seconds, a recorder may still be writing. Stop it first "
                "(or pass --force).".format(directory.name))
            worst = 1
            continue
        manifest = session.manifest
        static_info = {key: manifest[key] for key in STATIC_INFO_KEYS if key in manifest}
        static_info.setdefault("device_id", session.device_id)
        for index, recording in enumerate(session.recordings):
            if is_finalized(recording):
                continue
            log("{}: rewriting the unfinalized file {} ...".format(directory.name, recording.name))
            count = repair_recording(recording, manifest.get("session_id", directory.name), index, static_info)
            log("  kept {} messages".format(count))
        session.rebuild_index()
        nominal = {name: entry.get("fps", 0) for name, entry in (manifest.get("streams") or {}).items()}
        report = analyze_session(directory, nominal=nominal or None)
        if manifest and manifest.get("status") != "complete":
            counts: Dict[str, int] = {}
            schemas: Dict[str, str] = {}
            for _index, topic, schema, _message in session.raw():
                counts[topic] = counts.get(topic, 0) + 1
                schemas[topic] = schema
            anchor = report.get("alignment", {}).get("anchor")
            manifest.update({
                "status": "recovered", "recordings": ["recordings/" + path.name for path in session.recordings],
                "stop_reason": manifest.get("stop_reason") or "recorder did not shut down; recovered by 'repair'",
                "total_frames": report.get("streams", {}).get(anchor, {}).get("frames", 0),
                "topics": {topic: {"schema": schemas[topic], "message_count": counts[topic]} for topic in sorted(counts)},
                "verify": {key: report.get(key) for key in ("result", "problems", "warnings")},
            })
            write_json_atomic(directory / "manifest.json", manifest)
        hand_back_ownership(directory)
        print_report(report)
        worst = max(worst, 2 if report.get("result") == "FAIL" else 0)
    return worst


def command_export(args: argparse.Namespace) -> int:
    check_dependencies()
    directory = Path(args.session)
    session = Session(directory)
    target = Path(args.out) if args.out else directory / "derived" / "export"
    topics = session.topics()
    streams = [name for name in (args.streams or VIDEO_STREAMS + MOTION_STREAMS) if name in topics]
    step = max(1, args.every)
    log("Exporting {} to {}".format(", ".join(streams), target))
    for stream in streams:
        if stream in MOTION_STREAMS:
            samples = session.imu(stream)
            target.mkdir(parents=True, exist_ok=True)
            with (target / (stream + ".csv")).open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(("timestamp_ms", "x", "y", "z"))
                for row in samples:
                    writer.writerow(("{:.4f}".format(row[0]), repr(float(row[1])), repr(float(row[2])), repr(float(row[3]))))
            log("  {}: {} samples".format(stream, len(samples)))
            continue
        folder = target / stream
        folder.mkdir(parents=True, exist_ok=True)
        extension = ".jpg" if topics[stream].endswith("/jpeg") else ".png"
        written = 0
        with (target / (stream + "_frames.csv")).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(("index", "frame_number", "timestamp_ms", "file"))
            for position, (number, log_time, payload) in enumerate(session.images(stream, decode=False)):
                if position % step:
                    continue
                if args.max and written >= args.max:
                    break
                name = "{:06d}_{}{}".format(position, number, extension)
                (folder / name).write_bytes(payload)  # bytes exactly as recorded, no re-encoding
                writer.writerow((position, number, "{:.4f}".format(log_time / 1e6), "{}/{}".format(stream, name)))
                written += 1
        log("  {}: {} images".format(stream, written))
    for name in ("frame_index.csv", "alignment.csv"):
        source = directory / "derived" / name
        if source.is_file() and target.resolve() != source.parent.resolve():
            shutil.copy2(source, target / name)
    log("Done. Depth PNGs are 16-bit: cv2.imread(path, cv2.IMREAD_UNCHANGED) * {:g} = metres".format(session.depth_scale))
    return 0


class Tee:
    """Console output that is also kept in a file, so a report can be read back later."""

    def __init__(self, stream: Any, path: Path) -> None:
        self._stream, self._file = stream, path.open("a", encoding="utf-8", errors="replace")

    def write(self, text: str) -> int:
        self._file.write(text)
        self._file.flush()
        return self._stream.write(text)

    def flush(self) -> None:
        self._stream.flush()

    def isatty(self) -> bool:
        return False


DIAGNOSE_MODES = {
    1: "the recorder's order: depth + infrared, then color; started again if a stream stays silent, as the recorder does",
    2: "the recorder's order twice in one process (all streams stopped and started again)",
    3: "color alone",
}


def _diagnose_streams(rs: Any, sensors: List[Any], mode: int) -> bool:
    """Open the default streams in one of the DIAGNOSE_MODES and count frames per stream for 5 s.
    On macOS the interface owners are logged before and while streaming: UVCAssistant (the system camera
    service) holding an interface the SDK needs means libusb handed the camera back (see MacUsbHold)."""
    by_name: Dict[str, Any] = {}
    for sensor in sensors:
        try:
            label = str(sensor.get_info(rs.camera_info.name))
        except Exception:
            continue
        if "stereo" in label.lower():
            by_name["stereo"] = sensor
        elif "rgb" in label.lower() or "color" in label.lower():
            by_name["rgb"] = sensor

    def pick(sensor: Any, stream: Any, index: int, size: Tuple[int, int], formats: Tuple[str, ...]) -> Any:
        for fmt in formats:
            for fps in (30, 15):
                for profile in sensor.get_stream_profiles():
                    if (profile.stream_type() == stream and profile.format().name == fmt and profile.fps() == fps
                            and profile.stream_index() in (index, -1) and profile.is_video_stream_profile()):
                        video = profile.as_video_stream_profile()
                        if (video.width(), video.height()) == size:
                            return profile
        return None

    def stream_name(profile: Any) -> str:
        return "{}{}".format(profile.stream_type().name,
                             profile.stream_index() if profile.stream_type() == rs.stream.infrared else "")

    log("")
    log("--- streaming test {}: {}".format(mode, DIAGNOSE_MODES.get(mode, "?")))
    plan: Dict[str, Tuple[Any, List[Any]]] = {}
    if "stereo" in by_name and mode != 3:
        depth = pick(by_name["stereo"], rs.stream.depth, 0, (848, 480), ("z16",))
        infrared = pick(by_name["stereo"], rs.stream.infrared, 1, (848, 480), ("y8",))
        chosen = [item for item in (depth, infrared) if item is not None]
        if chosen:
            plan["stereo"] = (by_name["stereo"], chosen)
    if "rgb" in by_name:
        color = pick(by_name["rgb"], rs.stream.color, 0, (1280, 720), ("bgr8", "rgb8", "yuyv"))
        if color is not None:
            plan["rgb"] = (by_name["rgb"], [color])
    expected = []
    for _, profiles in plan.values():
        for profile in profiles:
            video = profile.as_video_stream_profile()
            expected.append(stream_name(profile))
            log("  {}: {}x{}@{} {}".format(stream_name(profile), video.width(), video.height(), profile.fps(),
                                          profile.format().name))
    if not expected:
        log("  no usable profiles")
        return False

    def one_round(label: str) -> bool:
        counts: Dict[str, int] = {name: 0 for name in expected}
        errors: List[str] = []

        def on_frame(frame: Any) -> None:
            try:
                name = stream_name(frame.get_profile())
                counts[name] = counts.get(name, 0) + 1
            except Exception as error:
                errors.append(repr(error))

        if label:
            log("  " + label)
        if sys.platform == "darwin":
            log("  before opening: " + "; ".join(macos_interface_owners()))
        opened: List[Any] = []
        started: List[Any] = []
        ok = True
        began = time.monotonic()

        def step(text: str) -> None:
            log("  {:6.0f} ms  {}".format((time.monotonic() - began) * 1000.0, text))

        try:
            for key in plan:  # stereo first, then color, as the recorder does
                step("open + start {}".format(key))
                plan[key][0].open(plan[key][1])
                opened.append(plan[key][0])
                plan[key][0].start(on_frame)
                started.append(plan[key][0])
            step("all started")
            time.sleep(1.5)
            if sys.platform == "darwin":
                log("  while streaming: " + "; ".join(macos_interface_owners()))
            time.sleep(3.5)
        except Exception as error:
            ok = False
            log("  FAILED: {!r}".format(error))
        for sensor in started:
            try:
                sensor.stop()
            except Exception as error:
                log("  stop raised {!r}".format(error))
        for sensor in opened:
            try:
                sensor.close()
            except Exception as error:
                log("  close raised {!r}".format(error))
        log("  frames in 5 s: " + ", ".join("{} {}".format(name, counts.get(name, 0)) for name in expected))
        if errors:
            log("  callback errors: {}".format(errors[:3]))
        return ok and all(counts.get(name, 0) > 0 for name in expected)

    if mode == 1:
        if one_round(""):
            return True
        # The recorder restarts all streams when one stays silent (seen for color right after the camera
        # was taken over from the macOS camera service). Check that this second start works.
        log("  a stream stayed silent; starting all streams again, as the recorder does")
        time.sleep(1.0)
        return one_round("second start")
    if mode == 3:
        return one_round("")
    first = one_round("round 1 of 2")
    time.sleep(1.0)
    second = one_round("round 2 of 2 (same process, same device object)")
    return first and second


def _diagnose_sdk_stage(args: argparse.Namespace) -> int:
    """The part of 'diagnose' that talks to the SDK. It runs in a child process, because on macOS a failed
    open can end in a native crash that takes the whole interpreter down."""
    rs = import_realsense()
    folder = Path(args.out)
    report_path = folder / "diagnose_{}.txt".format(args.stamp)
    sdk_log = folder / "diagnose_{}_sdk_{}.log".format(args.stamp, args.stage)
    crash_path = folder / "diagnose_{}_crash_{}.txt".format(args.stamp, args.stage)
    sdk_log.touch()
    sys.stdout = Tee(sys.stdout, report_path)  # type: ignore[assignment]
    crash_file = crash_path.open("w")
    try:
        import faulthandler
        faulthandler.enable(file=crash_file, all_threads=True)  # a native crash leaves the Python stack here
    except Exception:
        pass
    hand_back_ownership(folder)  # now, so the files stay readable even if the SDK takes the process down
    try:
        rs.log_to_file(rs.log_severity.debug, str(sdk_log))
        log("SDK debug log: {}".format(sdk_log))
    except Exception as error:
        log("could not enable the SDK log: {}".format(error))
    if sys.platform == "darwin":
        log("test {}: {}".format(args.stage, describe_hid_guard()))
        start_macos_usb_hold(args)
        take_macos_camera(prefix="test {}: ".format(args.stage))
        if USB_HOLD.disabled:
            log("test {}: {}".format(args.stage, USB_HOLD.describe()))
        log("  after the hold: " + "; ".join(macos_interface_owners()))

    log("creating the SDK context")
    context = rs.context()
    device = None
    failed = False
    for attempt in range(1, 4):
        try:
            log("query_devices, try {}".format(attempt))
            listing = context.query_devices()
            log("  the SDK lists {} device(s); opening them".format(len(listing)))
            devices = list(listing)
            if hid_guard_blocked():
                log("  the IMU crash guard caught the SDK's IMU open failure; the IMU is unavailable, "
                    "depth, color and infrared are not affected")
            for item in devices:
                log("  " + ", ".join("{}={}".format(key, value) for key, value in device_summary(rs, item).items()))
                device = device or item
            if devices:
                break
        except Exception as error:
            log("  query_devices raised {!r}".format(error))
        time.sleep(2.0)

    sensors: List[Any] = []
    if device is not None:
        try:
            sensors = list(device.query_sensors())
        except Exception as error:
            failed = True
            log("query_sensors raised {!r}".format(error))
    else:
        log("The SDK found no camera.")
    if sensors:
        failed = not _diagnose_streams(rs, sensors, int(args.stage)) or failed
    try:
        import faulthandler
        faulthandler.disable()
        crash_file.close()
        if crash_path.stat().st_size == 0:
            crash_path.unlink()
    except Exception:
        pass
    hand_back_ownership(folder)
    if device is None:
        return 3
    return 4 if failed else 0


LIBUSB_CHURN_MARKERS = (
    ("hand-backs to macOS", "reenumerating device for kernel driver attach"),
    ("take-overs by the SDK", "attempting to detach kernel driver"),
    ("stale-pipe errors", "pipe ref not recognized"),
)


def libusb_churn(path: Path) -> str:
    """How often libusb gave the camera back to macOS and took it again during one SDK test. With the
    USB hold all three counts are 0 (the hold's own take-over happens in a context that does not log)."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return "no libusb log"
    return ", ".join("{} {}".format(text.count(marker), label) for label, marker in LIBUSB_CHURN_MARKERS)


def libusb_highlights(path: Path, limit: int = 40) -> List[str]:
    """The informative lines of a LIBUSB_DEBUG log: errors, warnings, and how libusb took the camera over
    from the system (capture, re-enumeration, claims). Per-transfer debug lines are left out."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    keys = ("error", "warning", "capture", "detach", "attach", "reenumer", "restore_state", "claim",
            "USBInterfaceOpen", "not found", "exclusive", "failed", "Fatal Python", "Traceback")
    noise = ("submit_transfer", "handle_events", "async_io_callback", "handle_transfer_completion",
             "usbi_handle_transfer", "libusb_free_transfer", "darwin_handle_transfer_completion")
    picked = [line.strip()[:220] for line in lines if any(key in line for key in keys)
              and not (any(word in line for word in noise) and "error" not in line.lower())]
    errors = [line for line in picked if " error " in line or "warning" in line or "failed" in line.lower()]
    rest = [line for line in picked if line not in errors]
    chosen = (errors[:limit // 2] + rest)[:limit]
    if len(picked) > len(chosen):
        chosen.append("... {} more matching lines in {}".format(len(picked) - len(chosen), path.name))
    return chosen


def command_diagnose(args: argparse.Namespace) -> int:
    """Everything needed to debug 'camera not found' or 'no frames' on this computer, in one run.
    Exit codes: 0 camera streams, 1 SDK problem, 2 the operating system does not list the camera."""
    import subprocess

    if getattr(args, "stage", 0):
        require_admin_on_macos(args)  # the child runs as root already; this only injects the IMU crash guard
        return _diagnose_sdk_stage(args)
    rs = import_realsense()
    require_admin_on_macos(args)
    folder = Path(args.out)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    report_path = folder / "diagnose_{}.txt".format(stamp)
    sys.stdout = Tee(sys.stdout, report_path)  # type: ignore[assignment]
    hand_back_ownership(folder)
    log("record_d455.py {} diagnose, {}".format(__version__, local_iso()))
    log("pyrealsense2 {} | Python {} | {} | uid {}".format(
        sdk_version(rs), platform.python_version(), platform.platform(),
        os.geteuid() if hasattr(os, "geteuid") else "n/a"))
    if sys.platform == "darwin":
        log(describe_hid_guard())
        log("USB hold: " + ("off (--no-usb-hold)" if args.no_usb_hold else
                            "on, each SDK test takes the camera over from macOS for its own duration"))

    def shell(title: str, command: str, limit: int = 60, timeout: float = 30.0) -> None:
        log("")
        log("--- " + title)
        try:
            output = subprocess.run(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    universal_newlines=True, timeout=timeout).stdout
        except Exception as error:
            output = "(command failed: {})".format(error)
        lines = output.rstrip().splitlines() or ["(no output)"]
        for line in lines[:limit]:
            log("  " + line[:300])

    def system_view(title: str, limit: int) -> None:
        log("")
        log("--- " + title)
        for line in macos_iokit_camera_tree(limit=limit):
            log("  " + line)

    listed = True
    if sys.platform == "darwin":
        log("")
        log("--- what macOS itself sees on USB (no SDK involved)")
        if not wait_for_macos_usb(timeout=20.0):
            listed = False
        view = macos_usb_view()
        for line in describe_macos_usb_view(view):
            log(line)
        log("")
        log("--- USB-C ports (plug detection and data links, from the port controller)")
        for line in describe_usb_c_ports(macos_usb_c_ports()) or ["(not available)"]:
            log("  " + line)
        shell("system USB list, RealSense entries", "system_profiler SPUSBHostDataType 2>/dev/null | grep -i -B1 -A10 "
                                 "realsense || system_profiler SPUSBDataType 2>/dev/null | grep -i -B1 -A10 realsense "
                                 "|| echo 'no RealSense entry in the system USB list'")
        system_view("IOKit: the camera, its interfaces and who owns them", 90)
        shell("processes that may hold a camera", "ps -axo pid,user,comm | grep -i -E "
              "'UVCAssistant|VDCAssistant|cameracaptured|AppleCameraAssistant|realsense|record_d455' | grep -v grep")
        shell("kernel log: USB connects and disconnects in the last 10 minutes",
              "log show --last 10m --style compact --predicate 'process == \"kernel\" AND "
              "(eventMessage CONTAINS[c] \"connection lost\" OR eventMessage CONTAINS[c] \"Mbps\" OR "
              "eventMessage CONTAINS[c] \"Gbps\" OR eventMessage CONTAINS[c] \"enumerat\")' 2>/dev/null "
              "| grep -i -E 'usb|port' | tail -25", limit=25, timeout=90)
    elif sys.platform.startswith("linux"):
        shell("lsusb", "lsusb | grep -i -E '8086|intel'")
        shell("udev rules", "ls /etc/udev/rules.d/ | grep -i realsense")

    result = 2
    if not listed:
        log("")
        log("RESULT: macOS does not list the camera on USB, so the SDK was not started. This is hardware: "
            "cable, plug, port, or the camera needs a replug.")
    else:
        result = 1
        outcomes: List[str] = []
        for stage in sorted(DIAGNOSE_MODES):
            # After a test the camera is handed back to macOS, which re-creates the device; wait until that
            # is over (same device id for 6 s), because touching it mid-way crashes inside libusb.
            if sys.platform == "darwin" and not wait_for_macos_usb(timeout=40.0, settle=6.0):
                log("")
                log("The camera is no longer listed by macOS (it dropped off USB). Stopping the SDK tests.")
                result = 2
                break
            log("")
            log("=== SDK test {} of {} (separate process): {}".format(stage, len(DIAGNOSE_MODES),
                                                                    DIAGNOSE_MODES[stage]))
            sys.stdout.flush()
            command = [sys.executable, os.path.abspath(sys.argv[0]), "diagnose", "--out", str(folder),
                       "--stage", str(stage), "--stamp", stamp, "--no-sudo"] + (
                           ["--no-usb-hold"] if args.no_usb_hold else [])
            libusb_log = folder / "diagnose_{}_libusb_{}.txt".format(stamp, stage)
            child_env = dict(os.environ, LIBUSB_DEBUG="4")  # libusb's own messages, to this file
            try:
                with libusb_log.open("w") as child_stderr:
                    code = subprocess.call(command, timeout=120, stderr=child_stderr, env=child_env)
            except subprocess.TimeoutExpired:
                code = None
            hand_back_ownership(libusb_log, recursive=False)
            churn = libusb_churn(libusb_log)
            outcomes.append("test {} ({}): {}; libusb: {}".format(stage, DIAGNOSE_MODES[stage], {
                0: "all streams delivered frames", 3: "SDK saw no camera", 4: "a stream failed",
                None: "hung"}.get(code, "crashed" if code is not None and (code < 0 or code > 128)
                                  else "exit {}".format(code)), churn))
            if code == 0:
                log("=== SDK test {}: all streams delivered frames".format(stage))
                if stage != 3:
                    result = 0
            elif code is None:
                log("=== SDK test {}: no answer within 120 s (the SDK hung); the process was stopped".format(stage))
            elif code < 0 or code > 128:
                number = -code if code < 0 else code - 128
                try:
                    name = signal.Signals(number).name
                except (ValueError, AttributeError):
                    name = "signal {}".format(number)
                log("=== SDK test {}: the SDK CRASHED the process ({}).".format(stage, name))
                crash_path = folder / "diagnose_{}_crash_{}.txt".format(stamp, stage)
                try:
                    trace = crash_path.read_text(errors="replace").strip().splitlines()
                except OSError:
                    trace = []
                for line in trace[:40]:
                    log("    " + line)
            elif code == 3:
                log("=== SDK test {}: the SDK sees no camera although the system lists one".format(stage))
            else:
                log("=== SDK test {}: the camera opened but a stream failed (exit code {})".format(stage, code))
            if code != 0:
                for line in libusb_highlights(libusb_log):
                    log("    libusb: " + line)
            log("--- libusb during SDK test {}: {}".format(stage, churn))
            if sys.platform == "darwin":
                log("--- right after SDK test {}: ".format(stage) + "; ".join(macos_interface_owners()))
        log("")
        log("--- summary")
        for line in outcomes:
            log("  " + line)
        if sys.platform == "darwin" and result != 0:
            shell("system log of the last 3 minutes (camera and USB ownership lines)",
                  "log show --last 3m --style compact --predicate 'eventMessage CONTAINS[c] \"realsense\" "
                  "OR eventMessage CONTAINS[c] \"exclusive\" OR eventMessage CONTAINS[c] \"connection lost\" "
                  "OR process == \"UVCAssistant\"' 2>/dev/null | tail -40", limit=40, timeout=90)
        log("")
        log("RESULT: " + {0: "the camera works through the SDK on this computer.",
                          1: "the system lists the camera, but the SDK could not use it. Send the files below.",
                          2: "the camera dropped off USB during the test. This is hardware: cable, plug or port."
                          }[result])
    log("")
    log("Report: {}".format(report_path.resolve()))
    log("All files of this run: {}/diagnose_{}*".format(folder.resolve(), stamp))
    hand_back_ownership(folder)
    return result


def command_info(args: argparse.Namespace) -> int:
    rs = import_realsense()
    require_admin_on_macos(args)
    start_macos_usb_hold(args)
    if not wait_for_macos_usb(timeout=8.0):
        for line in describe_macos_usb_view(macos_usb_view()) or ["macOS lists no RealSense device on USB."]:
            log(line)
        return 1
    take_macos_camera()
    context = rs.context()
    try:
        devices = list(context.query_devices())
    except Exception as error:
        log("The SDK could not enumerate USB devices: {}".format(error))
        for hint in platform_hints():
            log("  - " + hint)
        sys.stdout.flush()
        os._exit(1)  # the SDK's device-watcher thread crashes the interpreter on a normal exit from here
    log("pyrealsense2 {} on {} / Python {}".format(sdk_version(rs), platform.platform(),
                                                   platform.python_version()))
    if not devices:
        log("No RealSense camera is visible to the SDK.")
        for hint in platform_hints():
            log("  - " + hint)
        return 1
    for device in devices:
        info = device_summary(rs, device)
        log("")
        log("{}".format(info.get("name", "RealSense device")))
        for key, value in info.items():
            if key != "name":
                log("  {:<30} {}".format(key, value))
        if hasattr(device, "is_metadata_enabled"):
            try:
                log("  {:<30} {}".format("metadata_enabled", device.is_metadata_enabled()))
            except Exception as error:
                log("  {:<30} unknown ({})".format("metadata_enabled", error))
        for sensor in device.query_sensors():
            try:
                profiles = list(sensor.get_stream_profiles())
                label = sensor.get_info(rs.camera_info.name)
            except Exception:
                continue
            log("  Sensor: {} ({})".format(label, sensor_role(rs, profiles)))
            groups: Dict[str, set] = {}
            for profile in profiles:
                key = "{}{} {}".format(profile.stream_type().name,
                                       "[{}]".format(profile.stream_index()) if profile.stream_index() else "",
                                       profile.format().name)
                if profile.is_video_stream_profile():
                    video = profile.as_video_stream_profile()
                    groups.setdefault(key, set()).add("{}x{}@{}".format(video.width(), video.height(), profile.fps()))
                else:
                    groups.setdefault(key, set()).add("{} Hz".format(profile.fps()))
            for key in sorted(groups):
                if args.all or key.split()[-1] in ("z16", "y8", "bgr8", "rgb8", "motion_xyz32f"):
                    log("    {:<28} {}".format(key, ", ".join(sorted(groups[key], key=_profile_sort))))
    return 0


def _profile_sort(text: str) -> Tuple[int, int, int]:
    numbers = [int(value) for value in re.findall(r"\d+", text)]
    return tuple((numbers + [0, 0, 0])[:3])  # type: ignore[return-value]


def command_enable_metadata(_args: argparse.Namespace) -> int:
    rs = import_realsense()
    if not hasattr(rs, "enable_metadata"):
        log("This pyrealsense2 build has no enable_metadata(). On Windows run the SDK script "
            "scripts/realsense_metadata_win10.ps1 from an Administrator PowerShell, or open RealSense Viewer "
            "once and accept its metadata prompt.")
        return 1
    try:
        rs.enable_metadata()
    except Exception as error:
        log("enable_metadata failed: {}\nRun this command from an Administrator terminal with the camera "
            "plugged in.".format(error))
        return 1
    log("Per-frame metadata enabled for the connected camera. Replug the camera before recording.")
    return 0


NO_USB_HOLD_HELP = ("macOS: do not take the camera over from the system for the whole run "
                    "(only for comparison tests; color then fails intermittently)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="record_d455.py",
        description="Plug-and-play recorder for one Intel RealSense D455: depth, color and infrared (IMU "
                    "optional) with aligned timestamps, written as an MCAP session directory.",
        epilog="Run without a command to record. Commands: record, selftest, verify, repair, export, info, "
               "diagnose, enable-metadata.")
    parser.add_argument("--version", action="version", version="record_d455.py " + __version__)
    sub = parser.add_subparsers(dest="command")

    def add_record_arguments(target: argparse.ArgumentParser) -> None:
        target.add_argument("--out", default="sessions", help="root folder for session directories (default: ./sessions)")
        target.add_argument("--streams", type=parse_streams, default=DEFAULT_STREAMS,
                            help="comma list of depth,color,ir_left,ir_right,ir,accel,gyro,imu "
                                 "(default: depth,color,ir_left)")
        target.add_argument("--fps", type=int, default=30, help="frame rate for depth, infrared and color (default 30)")
        target.add_argument("--depth-size", type=parse_size, default=(848, 480), help="depth / infrared size (default 848x480)")
        target.add_argument("--color-size", type=parse_size, default=(1280, 720), help="color size (default 1280x720)")
        target.add_argument("--accel-fps", type=int, default=0, help="accelerometer rate, 0 = closest to 200 Hz")
        target.add_argument("--gyro-fps", type=int, default=0, help="gyroscope rate, 0 = closest to 200 Hz")
        target.add_argument("--duration", type=float, default=0.0, help="stop after this many seconds (default: until Ctrl+C)")
        target.add_argument("--serial", default="", help="only use the camera with this serial number")
        target.add_argument("--loop", action="store_true", help="after a session, wait for the next camera to be plugged in")
        target.add_argument("--preview", action="store_true", help="show a live color + depth window (q or Esc stops)")
        target.add_argument("--color-codec", choices=("jpeg", "png"), default="jpeg", help="color storage (default jpeg)")
        target.add_argument("--ir-codec", choices=("jpeg", "png"), default="jpeg",
                            help="infrared storage: jpeg (default) or png, which is lossless and about 4x larger")
        target.add_argument("--jpeg-quality", type=int, default=90, help="JPEG quality (default 90)")
        target.add_argument("--png-level", type=int, default=1, help="PNG compression level 0-9, always lossless (default 1)")
        target.add_argument("--split-minutes", type=float, default=10.0, help="start a new .mcap file every N minutes (default 10)")
        target.add_argument("--emitter", choices=("keep", "on", "off"), default="keep", help="IR projector (default: leave as is)")
        target.add_argument("--set", action="append", metavar="ROLE.OPTION=VALUE",
                            help="set an SDK option before streaming, e.g. --set depth.laser_power=150 "
                                 "(roles: depth, color, motion); repeatable")
        target.add_argument("--require-imu", action="store_true", help="fail instead of continuing when the IMU cannot start")
        target.add_argument("--max-buffer-mb", type=int, default=1024, help="RAM budget for frames waiting to be written (default 1024)")
        target.add_argument("--min-free-gb", type=float, default=2.0, help="stop when free disk space drops below this (default 2)")
        target.add_argument("--location", default="", help="free text stored in manifest.scene.location")
        target.add_argument("--activity", default="", help="free text stored in manifest.scene.activity")
        target.add_argument("--distance-m", type=float, default=None, help="stored in manifest.scene.distance_m")
        target.add_argument("--note", default="", help="free text stored in manifest.scene.notes")
        target.add_argument("--start-retries", type=int, default=3,
                            help="how often to retry when the camera fails to start (default 3)")
        target.add_argument("--simulate", action="store_true",
                            help="use a simulated D455 instead of a real camera, to try the workflow")
        target.add_argument("--engine", choices=("auto", "sdk", "python"), default="auto", help=argparse.SUPPRESS)
        target.add_argument("--no-sudo", action="store_true",
                            help="macOS: do not re-run through sudo (the SDK usually cannot open the camera then)")
        target.add_argument("--no-usb-hold", action="store_true", help=NO_USB_HOLD_HELP)
        target.add_argument("--sdk-log", action="store_true",
                            help="write the SDK's debug log to logs/record_<time>_sdk.log (for troubleshooting)")
        target.add_argument("--quiet", action="store_true", help="no live status line")

    add_record_arguments(parser)
    add_record_arguments(sub.add_parser("record", help="record a session (default command)"))
    selftest = sub.add_parser("selftest", help="end-to-end test with a simulated D455, no camera needed")
    selftest.add_argument("--duration", type=float, default=6.0)
    selftest.add_argument("--out", default="", help="keep the test session under this folder")
    selftest.add_argument("--color-offset-ms", type=float, default=0.0, help="inject a known color-depth time offset")
    selftest.add_argument("--drop-every", type=int, default=0, help="drop every Nth depth/IR frame to test gap detection")
    selftest.add_argument("--split-minutes", type=float, default=10.0)
    selftest.add_argument("--imu", action="store_true",
                          help="also simulate accel + gyro (needs --engine sdk; Windows and Linux only)")
    selftest.add_argument("--engine", choices=("auto", "sdk", "python"), default="auto",
                          help="simulated frames from the SDK software device or from Python "
                               "(auto: python on macOS, where the SDK one crashes; sdk elsewhere)")
    verify = sub.add_parser("verify", help="report drops, jitter and cross-modality time differences")
    verify.add_argument("session", nargs="+", help="session directory")
    verify.add_argument("--deep", action="store_true", help="re-read every MCAP message and rebuild the frame index")
    repair = sub.add_parser("repair", help="finalize a session whose recorder was killed (crash, power loss)")
    repair.add_argument("session", nargs="+", help="session directory")
    repair.add_argument("--force", action="store_true", help="repair even if the files changed a moment ago")
    export = sub.add_parser("export", help="dump images (as recorded) and CSV tables from a session")
    export.add_argument("session", help="session directory")
    export.add_argument("--out", default="", help="target folder (default: <session>/derived/export)")
    export.add_argument("--streams", type=lambda text: parse_streams(text, require_video=False), default=None,
                        help="comma list of streams to export (default: everything recorded)")
    export.add_argument("--every", type=int, default=1, help="keep every Nth frame")
    export.add_argument("--max", type=int, default=0, help="at most this many frames per stream")
    diagnose = sub.add_parser("diagnose", help="write a full report on why the camera is not found or not streaming")
    diagnose.add_argument("--out", default="logs", help="folder for the report (default: ./logs)")
    diagnose.add_argument("--no-sudo", action="store_true", help="macOS: do not re-run through sudo")
    diagnose.add_argument("--no-usb-hold", action="store_true", help=NO_USB_HOLD_HELP)
    diagnose.add_argument("--stage", type=int, default=0, help=argparse.SUPPRESS)
    diagnose.add_argument("--stamp", default="", help=argparse.SUPPRESS)
    info = sub.add_parser("info", help="list connected cameras and their usable profiles")
    info.add_argument("--all", action="store_true", help="show every format")
    info.add_argument("--no-sudo", action="store_true", help="macOS: do not re-run through sudo")
    info.add_argument("--no-usb-hold", action="store_true", help=NO_USB_HOLD_HELP)
    sub.add_parser("enable-metadata", help="Windows: enable per-frame metadata (run as Administrator, once per PC)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    command = args.command or "record"
    handlers = {"record": command_record, "selftest": command_selftest, "verify": command_verify,
                "repair": command_repair, "export": command_export, "info": command_info,
                "diagnose": command_diagnose, "enable-metadata": command_enable_metadata}
    code = handlers[command](args)
    if sys.platform == "darwin" and command in ("record", "info", "selftest", "diagnose"):
        # Everything is written and closed. Skip interpreter teardown: on macOS librealsense is known
        # to abort in its USB cleanup at exit ("mutex lock failed").
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(int(code or 0))
    return code


if __name__ == "__main__":
    sys.exit(main())
