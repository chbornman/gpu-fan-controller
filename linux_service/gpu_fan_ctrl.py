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
from dataclasses import dataclass
from pathlib import Path

import serial
import yaml

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
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
    fan_curve: list = None

    # Hysteresis: don't change speed unless temp changed by this much
    hysteresis_c: float = 2.0

    # Minimum speed change to actually send (reduces serial chatter)
    min_speed_change: int = 3

    def __post_init__(self):
        if self.fan_curve is None:
            # Default fan curve for V620 / LLM workloads
            # Step function at 35C, then ramp to 100% at 75C
            self.fan_curve = [
                (35, 0),      # 35C and below -> 0% (fan off)
                (35.1, 25),   # Just above 35C -> step to 25%
                (75, 100),    # 75C -> 100% (full blast)
            ]


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
            label_file = temp_input.with_name(temp_input.name.replace("_input", "_label"))
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
            if label == preferred or (preferred == "" and label not in ["junction", "edge"]):
                log.info(f"Using AMD GPU sensor: {path} (label: {label or 'unknown'})")
                return path

    # Fallback to first sensor
    path, label = all_sensors[0]
    log.info(f"Using AMD GPU sensor: {path} (label: {label or 'unknown'})")
    return path


def read_gpu_temp(sensor_path: str) -> float | None:
    """Read GPU temperature in Celsius from sysfs."""
    try:
        with open(sensor_path, 'r') as f:
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
        log.debug(f"Temp {temp_c:.1f}C >= max {fan_curve[-1][0]}C -> {fan_curve[-1][1]}%")
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
            log.debug(f"Temp {temp_c:.1f}C in [{t1}C,{t2}C] -> lerp({s1}%,{s2}%) = {result}%")
            return result

    return fan_curve[-1][1]  # Fallback to max


class FanController:
    """Manages serial communication with ESP32 fan controller."""

    def __init__(self, config: Config):
        self.config = config
        self.serial: serial.Serial | None = None
        self.last_temp = None
        self.last_speed = None
        self.running = True

    def connect(self) -> bool:
        """Connect to ESP32 over serial."""
        try:
            self.serial = serial.Serial(
                port=self.config.serial_port,
                baudrate=self.config.serial_baud,
                timeout=1.0
            )
            # Wait for ESP32 to be ready
            time.sleep(0.5)

            # Flush any startup messages
            self.serial.reset_input_buffer()

            # Test connection
            if self.ping():
                log.info(f"Connected to ESP32 on {self.config.serial_port}")
                return True
            else:
                log.error("ESP32 not responding to PING")
                return False

        except serial.SerialException as e:
            log.error(f"Failed to connect to {self.config.serial_port}: {e}")
            return False

    def disconnect(self):
        """Close serial connection."""
        if self.serial and self.serial.is_open:
            self.serial.close()
            log.info("Disconnected from ESP32")

    def send_command(self, cmd: str) -> str | None:
        """Send command and return response."""
        if not self.serial or not self.serial.is_open:
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
                        if line.startswith("OK:") or line.startswith("ERR:") or line.startswith("PONG"):
                            break
                else:
                    time.sleep(0.01)

            response = "\n".join(response_lines)
            return response

        except serial.SerialException as e:
            log.error(f"Serial error: {e}")
            return None

    def ping(self) -> bool:
        """Check if ESP32 is responding."""
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
            log.warning(f"RPM reading {rpm} is bogus (pulses: {pulses}) - shared ground connected?")
            return None, pulses

        return rpm, pulses

    def run(self, sensor_path: str):
        """Main control loop."""
        log.info("Starting fan control loop")
        log.info(f"Poll interval: {self.config.poll_interval}s")
        log.info(f"Fan curve: {self.config.fan_curve}")

        while self.running:
            try:
                # Read GPU temperature
                temp = read_gpu_temp(sensor_path)
                if temp is None:
                    log.warning("Failed to read GPU temp, using max speed")
                    self.set_speed(100)
                    time.sleep(self.config.poll_interval)
                    continue

                # Apply hysteresis
                if self.last_temp is not None:
                    if abs(temp - self.last_temp) < self.config.hysteresis_c:
                        # Temp hasn't changed enough, just ping to reset watchdog
                        self.ping()
                        time.sleep(self.config.poll_interval)
                        continue

                # Calculate target speed
                target_speed = calculate_fan_speed(temp, self.config.fan_curve)

                # Check if speed change is significant
                if self.last_speed is not None:
                    if abs(target_speed - self.last_speed) < self.config.min_speed_change:
                        # Not enough change, just ping and log RPM
                        self.ping()
                        rpm, pulses = self.get_rpm()
                        log.debug(f"Temp: {temp:.1f}C | Fan: {self.last_speed}% | RPM: {rpm} | Pulses: {pulses}")
                        time.sleep(self.config.poll_interval)
                        continue

                # Set new speed
                if self.set_speed(target_speed):
                    rpm, pulses = self.get_rpm()
                    log.info(f"GPU: {temp:.1f}C -> Fan: {target_speed}% (was {self.last_speed}%) | RPM: {rpm} | Pulses: {pulses}")
                    self.last_temp = temp
                    self.last_speed = target_speed
                else:
                    # Failed to set speed, try to reconnect
                    log.warning("Lost connection, attempting reconnect...")
                    self.disconnect()
                    time.sleep(1)
                    if not self.connect():
                        log.error("Reconnect failed, will retry...")

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
            with open(config_path, 'r') as f:
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
    parser.add_argument("--dry-run", action="store_true", help="Don't send commands, just log")
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
