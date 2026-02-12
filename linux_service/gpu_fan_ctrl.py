#!/usr/bin/env python3
"""
GPU Fan Controller - Linux Service

Monitors GPU temperature and sends fan speed commands to ESP32 controller.
Designed for AMD Radeon Pro V620 but should work with any AMD GPU.

Usage:
    python gpu_fan_ctrl.py [--config /path/to/config.yaml]

The service will:
1. Find the GPU temperature sensor in /sys/class/drm/
2. Connect to the ESP32 over USB serial
3. Apply a fan curve based on temperature
4. Send speed commands every POLL_INTERVAL seconds
"""

import argparse
import glob
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import serial
import yaml

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


@dataclass
class Config:
    """Service configuration"""

    serial_port: str = "/dev/ttyACM0"
    serial_baud: int = 115200
    poll_interval: float = 2.0  # seconds - must be < watchdog timeout (5s)

    # Fan curve: list of (temp_c, speed_percent) tuples
    # Linear interpolation between points
    # Default fan curve for V620 / LLM workloads
    # Step function at 35C, then ramp to 100% at 75C
    fan_curve: list = field(
        default_factory=lambda: [
            (35, 0),  # 35C and below -> 0% (fan off)
            (35.1, 25),  # Just above 35C -> step to 25%
            (75, 100),  # 75C -> 100% (full blast)
        ]
    )

    # Hysteresis: don't change speed unless temp changed by this much
    hysteresis_c: float = 2.0

    # Minimum speed change to actually send (reduces serial chatter)
    min_speed_change: int = 3

    # Smoothing: max speed change per poll interval (0 = disabled)
    # Ramp up faster (cooling needed), ramp down slower (avoid oscillation)
    ramp_up_rate: int = 20  # Max % increase per poll
    ramp_down_rate: int = 10  # Max % decrease per poll


def find_gpu_temp_sensor() -> str | None:
    """
    Find AMD GPU temperature sensor in sysfs.
    Prefers junction temp (hottest spot) over edge temp.
    Returns path to temp*_input file or None if not found.
    """
    # First pass: find all AMD GPU hwmon directories and their sensors
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

    # Log all found sensors
    log.debug(f"Found {len(all_sensors)} AMD GPU temp sensors")
    for path, label in all_sensors:
        log.debug(f"  {path} ({label or 'no label'})")

    # Prefer junction > edge > anything else
    for preferred in ["junction", "edge", ""]:
        for path, label in all_sensors:
            if label == preferred or (
                preferred == "" and label not in ["junction", "edge"]
            ):
                log.info(f"Using AMD GPU sensor: {path} (label: {label or 'unknown'})")
                return path

    # Fallback to first sensor
    path, label = all_sensors[0]
    log.info(f"Using AMD GPU sensor: {path} (label: {label or 'unknown'})")
    return path


def read_gpu_temp(sensor_path: str) -> float | None:
    """Read GPU temperature in Celsius from sysfs."""
    try:
        with open(sensor_path, "r") as f:
            # sysfs reports millidegrees
            millidegrees = int(f.read().strip())
            return millidegrees / 1000.0
    except (IOError, ValueError) as e:
        log.error(f"Failed to read GPU temp: {e}")
        return None


def calculate_fan_speed(temp_c: float, fan_curve: list) -> int:
    """
    Calculate target fan speed based on temperature using linear interpolation.
    """
    if temp_c <= fan_curve[0][0]:
        log.debug(f"Temp {temp_c:.1f}C <= min {fan_curve[0][0]}C -> {fan_curve[0][1]}%")
        return fan_curve[0][1]

    if temp_c >= fan_curve[-1][0]:
        log.debug(
            f"Temp {temp_c:.1f}C >= max {fan_curve[-1][0]}C -> {fan_curve[-1][1]}%"
        )
        return fan_curve[-1][1]

    # Find the two points to interpolate between
    for i in range(len(fan_curve) - 1):
        t1, s1 = fan_curve[i]
        t2, s2 = fan_curve[i + 1]

        if t1 <= temp_c <= t2:
            # Linear interpolation
            ratio = (temp_c - t1) / (t2 - t1)
            speed = s1 + ratio * (s2 - s1)
            result = int(round(speed))
            log.debug(
                f"Temp {temp_c:.1f}C in [{t1}C,{t2}C] -> lerp({s1}%,{s2}%) = {result}%"
            )
            return result

    return fan_curve[-1][1]  # Fallback to max


class FanController:
    """Manages serial communication with ESP32 fan controller."""

    # Reconnection settings
    RECONNECT_DELAY_INITIAL = 1.0  # seconds
    RECONNECT_DELAY_MAX = 30.0  # seconds
    RECONNECT_DELAY_MULTIPLIER = 2.0

    def __init__(self, config: Config):
        self.config = config
        self.serial: serial.Serial | None = None
        self.last_temp = None
        self.last_speed = None
        self.running = True
        self._connected = False
        self._reconnect_delay = self.RECONNECT_DELAY_INITIAL

    def connect(self) -> bool:
        """Connect to ESP32 over serial."""
        try:
            self.serial = serial.Serial(
                port=self.config.serial_port,
                baudrate=self.config.serial_baud,
                timeout=1.0,
            )
            # Wait for ESP32 to be ready
            time.sleep(0.5)

            # Flush any startup messages
            self.serial.reset_input_buffer()

            # Test connection with direct write/read (not ping, to avoid recursion)
            self.serial.write(b"PING\n")
            self.serial.flush()
            time.sleep(0.1)
            response = self.serial.read(self.serial.in_waiting or 1).decode(
                errors="ignore"
            )

            if "PONG" in response:
                log.info(f"Connected to ESP32 on {self.config.serial_port}")
                self._connected = True
                self._reconnect_delay = self.RECONNECT_DELAY_INITIAL
                return True
            else:
                log.error("ESP32 not responding to PING")
                self._connected = False
                return False

        except (serial.SerialException, OSError) as e:
            log.error(f"Failed to connect to {self.config.serial_port}: {e}")
            self._connected = False
            return False

    def disconnect(self):
        """Close serial connection."""
        self._connected = False
        if self.serial:
            try:
                if self.serial.is_open:
                    self.serial.close()
            except (serial.SerialException, OSError):
                pass  # Already broken, ignore
            self.serial = None
            log.info("Disconnected from ESP32")

    def reconnect(self) -> bool:
        """
        Attempt to reconnect with exponential backoff.
        Returns True if reconnection succeeded.
        """
        self.disconnect()

        log.warning(f"Attempting reconnect in {self._reconnect_delay:.1f}s...")
        time.sleep(self._reconnect_delay)

        if self.connect():
            log.info("Reconnected successfully")
            return True
        else:
            # Increase delay for next attempt (exponential backoff)
            self._reconnect_delay = min(
                self._reconnect_delay * self.RECONNECT_DELAY_MULTIPLIER,
                self.RECONNECT_DELAY_MAX,
            )
            log.error(f"Reconnect failed, next attempt in {self._reconnect_delay:.1f}s")
            return False

    def send_command(self, cmd: str) -> str | None:
        """
        Send command and return response.
        Returns None and marks disconnected on I/O errors.
        """
        if not self._connected or not self.serial or not self.serial.is_open:
            return None

        try:
            self.serial.write(f"{cmd}\n".encode())
            self.serial.flush()

            # Read response (may be multiple lines)
            response_lines = []
            deadline = time.time() + 1.0
            while time.time() < deadline:
                if self.serial.in_waiting:
                    line = self.serial.readline().decode().strip()
                    if line:
                        response_lines.append(line)
                        # Check if we got a complete response
                        if (
                            line.startswith("OK:")
                            or line.startswith("ERR:")
                            or line.startswith("PONG")
                        ):
                            break
                else:
                    time.sleep(0.01)

            response = "\n".join(response_lines)
            return response

        except (serial.SerialException, OSError) as e:
            log.error(f"Serial error: {e}")
            self._connected = False
            return None

    def ping(self) -> bool:
        """Check if ESP32 is responding."""
        if not self._connected:
            return False
        response = self.send_command("PING")
        ok = response is not None and "PONG" in response
        if ok:
            log.debug("PING OK")
        return ok

    def set_speed(self, percent: int) -> bool:
        """Set fan speed percentage."""
        percent = max(0, min(100, percent))
        response = self.send_command(f"SET {percent}")
        if response and "OK:" in response:
            log.debug(f"Set fan speed to {percent}%")
            return True
        else:
            log.error(f"Failed to set speed: {response}")
            return False

    def get_status(self) -> dict | None:
        """Get current status from ESP32."""
        response = self.send_command("STATUS")
        if not response:
            return None

        status = {}
        for line in response.split("\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                status[key.strip()] = value.strip()
        return status

    def get_rpm(self) -> tuple[int | None, int | None]:
        """Get current fan RPM and pulse count from ESP32."""
        response = self.send_command("RPM")
        rpm = None
        pulses = None

        if response:
            for line in response.split("\n"):
                if "RPM:" in line:
                    try:
                        rpm = int(line.split(":")[-1].strip())
                    except ValueError:
                        pass
                elif "PULSES:" in line:
                    try:
                        pulses = int(line.split(":")[-1].strip())
                    except ValueError:
                        pass

        # Sanity check - PC fans max out around 10k RPM
        if rpm is not None and rpm > 50000:
            log.warning(
                f"RPM reading {rpm} is bogus (pulses: {pulses}) - shared ground connected?"
            )
            return None, pulses

        return rpm, pulses

    def smooth_speed(self, current: int, target: int) -> int:
        """
        Calculate next speed step toward target with smoothing.
        Ramps up faster than down to prioritize cooling.
        Returns the next speed to set.
        """
        if current == target:
            return target

        if target > current:
            # Ramping up - use faster rate
            step = min(self.config.ramp_up_rate, target - current)
            return current + step
        else:
            # Ramping down - use slower rate
            step = min(self.config.ramp_down_rate, current - target)
            return current - step

    def run(self, sensor_path: str):
        """Main control loop."""
        log.info("Starting fan control loop")
        log.info(f"Poll interval: {self.config.poll_interval}s")
        log.info(f"Fan curve: {self.config.fan_curve}")
        log.info(
            f"Smoothing: ramp_up={self.config.ramp_up_rate}%/poll, ramp_down={self.config.ramp_down_rate}%/poll"
        )

        target_speed = None  # What the fan curve says we should be at
        current_speed = self.last_speed  # What we've actually set (None = unknown)

        # Initial read and write on startup
        temp = read_gpu_temp(sensor_path)
        if temp is not None:
            target_speed = calculate_fan_speed(temp, self.config.fan_curve)
            current_speed = target_speed
            self.set_speed(current_speed)
            self.last_temp = temp
            self.last_speed = current_speed
            log.info(f"Initial: GPU {temp:.1f}C -> Fan {current_speed}%")
        else:
            log.warning("Failed to read initial GPU temp, starting at 100%")
            current_speed = 100
            target_speed = 100
            self.set_speed(100)

        while self.running:
            try:
                # Check if we need to reconnect
                if not self._connected:
                    if not self.reconnect():
                        # Reconnect failed, wait and retry next loop
                        continue
                    # After reconnect, re-send current speed to sync ESP32 state
                    if current_speed is not None:
                        log.info(f"Re-sending speed {current_speed}% after reconnect")
                        self.set_speed(current_speed)

                # Read GPU temperature
                temp = read_gpu_temp(sensor_path)
                if temp is None:
                    log.warning("Failed to read GPU temp, using max speed")
                    self.set_speed(100)
                    current_speed = 100
                    target_speed = 100
                    time.sleep(self.config.poll_interval)
                    continue

                # Calculate target speed from fan curve
                new_target = calculate_fan_speed(temp, self.config.fan_curve)

                # Update target if temp changed enough (hysteresis)
                if (
                    self.last_temp is None
                    or abs(temp - self.last_temp) >= self.config.hysteresis_c
                ):
                    target_speed = new_target
                    self.last_temp = temp

                # If no target yet, initialize it
                if target_speed is None:
                    target_speed = new_target

                # Calculate next step toward target (smoothing)
                next_speed = self.smooth_speed(current_speed, target_speed)

                # Check if we need to send a command
                if next_speed != current_speed:
                    if self.set_speed(next_speed):
                        rpm, pulses = self.get_rpm()
                        if next_speed == target_speed:
                            log.info(
                                f"GPU: {temp:.1f}C -> Fan: {next_speed}% (target reached) | RPM: {rpm}"
                            )
                        else:
                            log.info(
                                f"GPU: {temp:.1f}C -> Fan: {next_speed}% (ramping to {target_speed}%) | RPM: {rpm}"
                            )
                        current_speed = next_speed
                        self.last_speed = current_speed
                    # If set_speed failed, _connected is now False,
                    # reconnect will happen at start of next loop
                else:
                    # At target, just ping to reset watchdog
                    self.ping()
                    rpm, pulses = self.get_rpm()
                    log.debug(f"Temp: {temp:.1f}C | Fan: {current_speed}% | RPM: {rpm}")

                time.sleep(self.config.poll_interval)

            except KeyboardInterrupt:
                break
            except Exception as e:
                log.exception(f"Unexpected error: {e}")
                time.sleep(self.config.poll_interval)

        log.info("Fan control loop stopped")

    def stop(self):
        """Signal the control loop to stop."""
        self.running = False


def load_config(config_path: str | None) -> Config:
    """Load configuration from YAML file or use defaults."""
    config = Config()

    if config_path and os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                data = yaml.safe_load(f)
                if data:
                    for key, value in data.items():
                        if hasattr(config, key):
                            setattr(config, key, value)
            log.info(f"Loaded config from {config_path}")
        except Exception as e:
            log.warning(f"Failed to load config: {e}, using defaults")

    return config


def main():
    parser = argparse.ArgumentParser(description="GPU Fan Controller Service")
    parser.add_argument("--config", "-c", help="Path to config YAML file")
    parser.add_argument("--port", "-p", help="Serial port (overrides config)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    parser.add_argument(
        "--dry-run", action="store_true", help="Don't send commands, just log"
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load config
    config = load_config(args.config)
    if args.port:
        config.serial_port = args.port

    # Find GPU sensor
    sensor_path = find_gpu_temp_sensor()
    if not sensor_path:
        log.error("No GPU temperature sensor found!")
        log.error("Make sure amdgpu driver is loaded")
        sys.exit(1)

    # Create controller
    controller = FanController(config)

    # Handle signals for clean shutdown
    def signal_handler(signum, frame):
        log.info(f"Received signal {signum}, shutting down...")
        controller.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Connect to ESP32
    if not controller.connect():
        log.error("Failed to connect to ESP32")
        sys.exit(1)

    try:
        # Run control loop
        controller.run(sensor_path)
    finally:
        controller.disconnect()

    log.info("Service stopped")


if __name__ == "__main__":
    main()
