# GPU Fan Controller

ESP32-S3 based fan controller for GPUs that lack Linux software fan control (like the AMD Radeon Pro V620).

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ LINUX PC                                                    │
│                                                             │
│  ┌──────────────────┐     ┌──────────────────────────────┐ │
│  │ gpu_fan_ctrl.py  │────►│ /sys/class/drm/.../temp1     │ │
│  │ (systemd svc)    │     └──────────────────────────────┘ │
│  └────────┬─────────┘                                       │
│           │ USB Serial: "SET 75"                            │
└───────────┼─────────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────────┐
│ ESP32-S3                                                    │
│                                                             │
│  • Receives speed commands (SET 0-100)                      │
│  • Outputs 25kHz PWM to fan                                 │
│  • Reads tachometer for RPM                                 │
│  • WATCHDOG: No command in 5s → 100% speed (failsafe)       │
│                                                             │
└───────────┬─────────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────────┐
│ 4-PIN PWM FAN                                               │
│                                                             │
│  • 12V power from motherboard fan header (BIOS: 100%)       │
│  • PWM signal from ESP32                                    │
│  • Tach signal to ESP32                                     │
└─────────────────────────────────────────────────────────────┘
```

## Wiring

### Fan Header Connection

Keep the fan plugged into motherboard header for 12V power, disconnect PWM and Tach:

```
Motherboard Fan Header           4-Pin Fan              ESP32-S3
      │                              │                      │
      ├── GND (black) ───────────────┤ GND ─────────────────┤ GND
      ├── 12V (yellow) ──────────────┤ 12V                  │
      ├── TACH (green) ──── X        │                      │
      ├── PWM (blue) ────── X        │                      │
                                     │                      │
                      Fan TACH ──────┼──────────────────────┤ GPIO4
                      Fan PWM ───────┼──────────────────────┤ GPIO5
```

**Important:** Set BIOS fan header to 100% (or DC mode) since we're controlling PWM separately.

### ESP32-S3 Pinout

| GPIO | Function | Wire Color | Notes |
|------|----------|------------|-------|
| 4    | TACH IN  | Green      | Internal pull-up enabled |
| 5    | PWM OUT  | Blue       | 25kHz PWM signal |
| GND  | Ground   | Black      | Shared with fan |

### USB Connection

Connect ESP32-S3 to internal motherboard USB 2.0 header using a **USB-C to USB 2.0 header cable**:

1. Plug USB-C end into ESP32-S3 COM port
2. Connect header end to motherboard (e.g., JUSB2)
3. Route cable inside case

### Fan Wire Splicing

Use a **4-pin fan extension cable** - cut it to expose the PWM (blue) and Tach (green) wires:

1. Cut the extension cable in the middle
2. Strip the blue (PWM) and green (Tach) wires
3. Connect to ESP32 GPIO pins with jumper wires or solder
4. Keep GND (black) and 12V (yellow) connected through to the fan
5. Connect a jumper from ESP32 GND to the fan's GND wire (shared ground required for tach)

## Building the Firmware

### Prerequisites

```bash
# Install ESP-IDF (if not already)
# https://docs.espressif.com/projects/esp-idf/en/latest/esp32s3/get-started/

# Set up environment
. ~/esp/esp-idf/export.sh
```

### Build & Flash

```bash
cd /path/to/gpu_switch

# Configure (first time)
idf.py set-target esp32s3

# Build
idf.py build

# Flash (adjust port if needed)
idf.py -p /dev/ttyACM0 flash

# Monitor (for debugging)
idf.py -p /dev/ttyACM0 monitor
```

## ESP32 Serial Protocol

Commands (case-insensitive):

| Command | Description | Response |
|---------|-------------|----------|
| `SET <0-100>` | Set fan speed % | `OK: <value>` |
| `GET` | Get current speed | `SPEED: <value>` |
| `RPM` | Get fan RPM | `RPM: <value>` |
| `STATUS` | Full status | Speed, RPM, watchdog state |
| `PING` | Connectivity check | `PONG` |
| `WD` | Watchdog status | Timeout, last command time |

### Watchdog

If no `SET` or `PING` command received within **5 seconds**, ESP32 automatically sets fan to **100%** as a safety measure.

## Linux Service Installation

```bash
cd linux_service
sudo ./install.sh
```

### Manual Testing

```bash
# Test serial connection
echo "PING" > /dev/ttyACM0
cat /dev/ttyACM0  # Should show PONG

# Test service manually
sudo python3 /opt/gpu_fan_ctrl/gpu_fan_ctrl.py -v

# Check GPU temp sensor
cat /sys/class/drm/card*/device/hwmon/hwmon*/temp1_input
```

### Service Management

```bash
# Enable on boot
sudo systemctl enable gpu-fan-ctrl

# Start/stop
sudo systemctl start gpu-fan-ctrl
sudo systemctl stop gpu-fan-ctrl

# Check status
sudo systemctl status gpu-fan-ctrl

# View logs
journalctl -u gpu-fan-ctrl -f
```

## Configuration

Edit `/etc/gpu_fan_ctrl/config.yaml`:

```yaml
serial_port: "/dev/ttyACM0"
poll_interval: 2.0

# Fan curve: [temp_C, speed_%]
# Step function at 35C, then linear ramp to 100% at 75C
fan_curve:
  - [35, 0]      # 35C and below -> fan off
  - [35.1, 25]   # Just above 35C -> step to 25%
  - [75, 100]    # 75C -> full blast

hysteresis_c: 2.0      # Temp change threshold
min_speed_change: 3    # Speed change threshold
```

The service reads **junction temperature** (hottest spot on GPU die) - preferred for LLM/compute workloads where sustained high utilization causes heat buildup.

## Troubleshooting

### ESP32 not detected

```bash
# Check USB devices
lsusb | grep -i espressif

# Check serial port
ls -la /dev/ttyACM*

# Check permissions (add user to dialout group)
sudo usermod -a -G dialout $USER
# Then logout/login
```

### GPU temp sensor not found

```bash
# Check for amdgpu driver
lsmod | grep amdgpu

# Find hwmon devices
ls /sys/class/hwmon/*/name
cat /sys/class/hwmon/*/name

# Try rocm-smi if available
rocm-smi --showtemp
```

### Fan not spinning

1. Check 12V power from motherboard header
2. Verify BIOS fan header is set to 100% or DC mode
3. Test PWM signal with oscilloscope/multimeter
4. Try `SET 100` command manually

## Parts List

- ESP32-S3 DevKit with USB-C (~$8)
- USB-C to USB 2.0 header cable (~$8)
- 4-pin PWM fan extension cable (~$5) - cut to splice wires
- Jumper wires / Dupont connectors
- (Optional) 3D printed enclosure to mount inside case
