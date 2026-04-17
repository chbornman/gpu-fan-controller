#!/usr/bin/env python3
"""
GPU Fan Controller — Linux Service

Drives an ESP32-S3 fan controller over a COBS-framed binary protocol on
USB Serial/JTAG.

Design:
  - ESP32 pushes MSG_STATUS frames unsolicited every 500 ms.
  - Host sends MSG_CMD_SET only when the target fan speed changes, plus a
    MSG_CMD_KEEPALIVE every poll tick so the device watchdog stays happy.
  - Host watchdog: if no STATUS arrives within host_watchdog_s, the link is
    declared dead and the connection is torn down and retried.
  - Device watchdog: if no frame arrives within 5 s, the device ramps to 100%.

Logs from the ESP firmware are routed to UART0 so they can never be confused
for protocol bytes.
"""

import argparse
import glob
import logging
import os
import signal
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import serial
import yaml
from cobs import cobs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------- Protocol (must match firmware/proto.h) ----------

MSG_CMD_SET       = 0x01
MSG_CMD_KEEPALIVE = 0x02
MSG_STATUS        = 0x10

# STATUS payload: u8 speed, u16 rpm, u8 wd_triggered, u32 uptime_ms
STATUS_STRUCT = struct.Struct("<BHBI")

MAX_FRAME_BYTES = 256


def encode_frame(msg_type: int, payload: bytes = b"") -> bytes:
    body = bytes([msg_type]) + payload
    return cobs.encode(body) + b"\x00"


def decode_frame(frame: bytes) -> tuple[int, bytes] | None:
    try:
        body = cobs.decode(frame)
    except cobs.DecodeError:
        return None
    if not body:
        return None
    return body[0], body[1:]


# ---------- Config ----------

@dataclass
class Config:
    serial_port: str = "/dev/gpu-fan-ctrl"
    serial_baud: int = 115200           # Ignored by USB-CDC, required by pyserial.
    poll_interval: float = 1.0          # Control/keepalive tick.
    host_watchdog_s: float = 2.0        # Declare disconnected if no STATUS in this time.

    fan_curve: list = field(
        default_factory=lambda: [
            (35, 0),
            (35.1, 25),
            (75, 100),
        ]
    )

    hysteresis_c: float = 2.0
    min_speed_change: int = 3
    ramp_up_rate: int = 20
    ramp_down_rate: int = 10


# ---------- GPU sensor ----------

def find_gpu_temp_sensor() -> str | None:
    all_sensors = []
    for drm_path in glob.glob("/sys/class/drm/card*/device/hwmon/hwmon*"):
        hwmon_dir = Path(drm_path)
        name_file = hwmon_dir / "name"
        if not name_file.exists():
            continue
        name = name_file.read_text().strip()
        if "amdgpu" not in name.lower():
            continue
        for temp_input in hwmon_dir.glob("temp*_input"):
            label_file = temp_input.with_name(
                temp_input.name.replace("_input", "_label")
            )
            label = ""
            if label_file.exists():
                label = label_file.read_text().strip().lower()
            all_sensors.append((str(temp_input), label))

    if not all_sensors:
        log.warning("No AMD GPU temperature sensor found in sysfs")
        return None

    for preferred in ["junction", "edge", ""]:
        for path, label in all_sensors:
            if label == preferred or (
                preferred == "" and label not in ["junction", "edge"]
            ):
                log.info(f"Using AMD GPU sensor: {path} (label: {label or 'unknown'})")
                return path

    path, label = all_sensors[0]
    log.info(f"Using AMD GPU sensor: {path} (label: {label or 'unknown'})")
    return path


def read_gpu_temp(sensor_path: str) -> float | None:
    try:
        with open(sensor_path, "r") as f:
            return int(f.read().strip()) / 1000.0
    except (IOError, ValueError) as e:
        log.error(f"Failed to read GPU temp: {e}")
        return None


def calculate_fan_speed(temp_c: float, fan_curve: list) -> int:
    if temp_c <= fan_curve[0][0]:
        return fan_curve[0][1]
    if temp_c >= fan_curve[-1][0]:
        return fan_curve[-1][1]

    for i in range(len(fan_curve) - 1):
        t1, s1 = fan_curve[i]
        t2, s2 = fan_curve[i + 1]
        if t1 <= temp_c <= t2:
            ratio = (temp_c - t1) / (t2 - t1)
            return int(round(s1 + ratio * (s2 - s1)))
    return fan_curve[-1][1]


# ---------- Fan controller ----------

class FanController:
    RECONNECT_INITIAL_S    = 1.0
    RECONNECT_MAX_S        = 10.0
    RECONNECT_MULTIPLIER   = 2.0
    INITIAL_STATUS_TIMEOUT = 2.0

    def __init__(self, config: Config):
        self.config = config
        self.serial: serial.Serial | None = None
        self.running = True

        self._reader_thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._last_status: dict | None = None
        self._last_status_time: float = 0.0

        self._reconnect_delay = self.RECONNECT_INITIAL_S

        self.last_temp: float | None = None
        self.current_speed = 100
        self.target_speed = 100

    # ----- Connection management -----

    def connect(self) -> bool:
        try:
            self.serial = serial.Serial(
                port=self.config.serial_port,
                baudrate=self.config.serial_baud,
                timeout=0.5,
                write_timeout=0.5,
            )
            self.serial.reset_input_buffer()
            self.serial.reset_output_buffer()
        except (serial.SerialException, OSError) as e:
            log.warning(f"Open {self.config.serial_port} failed: {e}")
            self.serial = None
            return False

        with self._state_lock:
            self._last_status = None
            self._last_status_time = 0.0

        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            args=(self.serial,),
            daemon=True,
            name="serial-reader",
        )
        self._reader_thread.start()

        deadline = time.monotonic() + self.INITIAL_STATUS_TIMEOUT
        while time.monotonic() < deadline:
            with self._state_lock:
                st = self._last_status
            if st is not None:
                log.info(
                    f"Connected to ESP32 on {self.config.serial_port} "
                    f"(speed={st['speed']}%, rpm={st['rpm']}, wd={'TRIP' if st['wd'] else 'ok'})"
                )
                self._reconnect_delay = self.RECONNECT_INITIAL_S
                return True
            time.sleep(0.05)

        log.warning("Opened port but no STATUS received — tearing down")
        self._teardown_connection()
        return False

    def _teardown_connection(self):
        s = self.serial
        self.serial = None
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
        t = self._reader_thread
        self._reader_thread = None
        if t is not None and t is not threading.current_thread() and t.is_alive():
            t.join(timeout=1.0)

    def _reconnect(self):
        log.info(f"Reconnecting in {self._reconnect_delay:.1f}s...")
        self._teardown_connection()
        time.sleep(self._reconnect_delay)
        if not self.connect():
            self._reconnect_delay = min(
                self._reconnect_delay * self.RECONNECT_MULTIPLIER,
                self.RECONNECT_MAX_S,
            )

    # ----- Reader thread -----

    def _reader_loop(self, s: serial.Serial):
        accum = bytearray()
        while self.running:
            try:
                data = s.read(64)
            except (serial.SerialException, OSError, TypeError) as e:
                # TypeError happens when the main thread closes the port mid-read:
                # pyserial nulls its internal fd and the blocked os.read trips.
                log.debug(f"Reader exiting: {type(e).__name__}: {e}")
                self.serial = None
                return
            if not data:
                continue
            for b in data:
                if b == 0:
                    if accum:
                        self._handle_frame(bytes(accum))
                    accum.clear()
                else:
                    accum.append(b)
                    if len(accum) > MAX_FRAME_BYTES:
                        # Garbage or drift — drop and wait for next delimiter.
                        accum.clear()

    def _handle_frame(self, frame: bytes):
        result = decode_frame(frame)
        if result is None:
            return
        msg_type, payload = result
        if msg_type == MSG_STATUS and len(payload) == STATUS_STRUCT.size:
            speed, rpm, wd, uptime_ms = STATUS_STRUCT.unpack(payload)
            with self._state_lock:
                self._last_status = {
                    "speed": speed,
                    "rpm": rpm,
                    "wd": bool(wd),
                    "uptime_ms": uptime_ms,
                }
                self._last_status_time = time.monotonic()

    # ----- Sending -----

    def _send(self, msg_type: int, payload: bytes = b"") -> bool:
        s = self.serial
        if s is None:
            return False
        try:
            s.write(encode_frame(msg_type, payload))
            return True
        except (serial.SerialException, OSError) as e:
            log.warning(f"Serial write error: {e}")
            self.serial = None
            return False

    def send_set(self, percent: int) -> bool:
        percent = max(0, min(100, percent))
        return self._send(MSG_CMD_SET, bytes([percent]))

    def send_keepalive(self) -> bool:
        return self._send(MSG_CMD_KEEPALIVE)

    # ----- Control loop -----

    def _smooth_speed(self, current: int, target: int) -> int:
        if current == target:
            return target
        if target > current:
            return current + min(self.config.ramp_up_rate, target - current)
        return current - min(self.config.ramp_down_rate, current - target)

    def run(self, sensor_path: str):
        log.info("Starting control loop")
        log.info(
            f"Poll: {self.config.poll_interval}s, "
            f"host watchdog: {self.config.host_watchdog_s}s"
        )
        log.info(f"Fan curve: {self.config.fan_curve}")

        while self.running:
            try:
                if self.serial is None:
                    self._reconnect()
                    continue

                with self._state_lock:
                    last_time = self._last_status_time
                age = time.monotonic() - last_time
                if age > self.config.host_watchdog_s:
                    log.warning(f"Host watchdog: no STATUS in {age:.1f}s — reconnecting")
                    self._teardown_connection()
                    continue

                temp = read_gpu_temp(sensor_path)
                if temp is None:
                    log.warning("Failed to read GPU temp — forcing 100%")
                    self.target_speed = 100
                else:
                    new_target = calculate_fan_speed(temp, self.config.fan_curve)
                    if (
                        self.last_temp is None
                        or abs(temp - self.last_temp) >= self.config.hysteresis_c
                    ):
                        self.target_speed = new_target
                        self.last_temp = temp

                next_speed = self._smooth_speed(self.current_speed, self.target_speed)

                if next_speed != self.current_speed:
                    change = abs(next_speed - self.current_speed)
                    if change >= self.config.min_speed_change or next_speed == self.target_speed:
                        if self.send_set(next_speed):
                            self.current_speed = next_speed
                            with self._state_lock:
                                st = self._last_status
                            rpm = st["rpm"] if st else "?"
                            tag = (
                                "target reached"
                                if next_speed == self.target_speed
                                else f"ramping to {self.target_speed}%"
                            )
                            tstr = f"{temp:.1f}C" if temp is not None else "?"
                            log.info(f"GPU: {tstr} -> Fan: {next_speed}% ({tag}) | RPM: {rpm}")

                # Keep the device watchdog fed every tick, regardless of SET activity.
                self.send_keepalive()

                time.sleep(self.config.poll_interval)

            except KeyboardInterrupt:
                break
            except Exception:
                log.exception("Unexpected error in control loop")
                time.sleep(self.config.poll_interval)

        log.info("Control loop stopped")
        self._teardown_connection()

    def stop(self):
        self.running = False


# ---------- CLI ----------

def load_config(config_path: str | None) -> Config:
    config = Config()
    if config_path and os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                data = yaml.safe_load(f) or {}
            for key, value in data.items():
                if hasattr(config, key):
                    setattr(config, key, value)
            log.info(f"Loaded config from {config_path}")
        except Exception as e:
            log.warning(f"Failed to load config ({e}) — using defaults")
    return config


def main():
    parser = argparse.ArgumentParser(description="GPU Fan Controller Service")
    parser.add_argument("--config", "-c", help="Path to config YAML file")
    parser.add_argument("--port", "-p", help="Serial port (overrides config)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config = load_config(args.config)
    if args.port:
        config.serial_port = args.port

    sensor_path = find_gpu_temp_sensor()
    if not sensor_path:
        log.error("No GPU temperature sensor found — is amdgpu loaded?")
        sys.exit(1)

    controller = FanController(config)

    def signal_handler(signum, frame):
        log.info(f"Received signal {signum}, shutting down...")
        controller.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        controller.run(sensor_path)
    finally:
        controller._teardown_connection()

    log.info("Service stopped")


if __name__ == "__main__":
    main()
