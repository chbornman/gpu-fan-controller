# GPU Fan Controller

ESP32-S3 based fan controller for GPUs that lack Linux software fan control (like the AMD Radeon Pro V620).

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ LINUX PC                                                    │
│                                                             │
│  ┌──────────────────┐     ┌──────────────────────────────┐ │
│  │ gpu_fan_ctrl.py  │────►│ max junction, both cards     │ │
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
│  • Outputs 25kHz PWM on GPIO5 (MOSFET gate)                 │
│  • Tach unused (3-pin fans)                                 │
│  • WATCHDOG: No command in 5s → 100% speed (failsafe)       │
│                                                             │
└───────────┬─────────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────────┐
│ MOSFET-DRIVEN 3-PIN FANS                                    │
│                                                             │
│  • 12V from PSU/header, split to all four fans              │
│  • ESP PWM (GPIO5) drives an N-channel MOSFET gate          │
│  • MOSFET switches the fans' shared ground                  │
└─────────────────────────────────────────────────────────────┘
```

## Wiring

Two GPUs, each with two 30mm 3-pin fans (four fans total), all driven together
from a single ESP32 PWM output through a low-side N-channel MOSFET. 3-pin fans
have no PWM input pin: speed is controlled by switching their shared 12V rail,
not by feeding them a logic signal, so the MOSFET is required.

### MOSFET Fan Driver

GPIO5's existing 25kHz PWM drives the MOSFET gate; the MOSFET switches the fans'
shared ground. All four fans get 12V in parallel through 3-pin splitters.

```
                 +12V   (motherboard fan header yellow  —or—  PSU 12V rail)
                   │
     ┌─────────────┼──────────────────────────────────────┐
     │             │                                       │
     │        ┌────┴─────┐                            cathode (striped end)
  3-pin       │  4 fans  │  all + wires → +12V             ┿  1N5819
  splitters   │    in    │                                 │   (flyback diode)
  fan out     │ parallel │  all − wires → node A           │
  the rail    └────┬─────┘                            anode │
     │             │ node A                                 │
     └─────────────┼────────────────────────────────────── ┘
                   │   (node A = every fan's − wire, bundled together)
                   │
                   │ Drain
               ┌───┴───┐
 GPIO5 ─[220Ω]─┤ Gate  │   N-channel LOGIC-LEVEL MOSFET
   (PWM 25k)   └───┬───┘   (AO3400 for this current, or IRLZ44N)
       │           │ Source
     [10kΩ]        │
       │           │
      GND ─────────┴───────────────  COMMON GROUND
                                     (ESP32 GND + PSU/header GND all tied here)
```

### Connection Table

| From | To | Notes |
|------|----|-------|
| ESP32 **GPIO5** | 220 Ω → MOSFET **gate** | existing PWM output, no firmware change |
| MOSFET **gate** | 10 kΩ → **GND** | pulldown; keeps fans off while the ESP boots/floats |
| MOSFET **drain** | **node A** (all four fan − wires) | |
| MOSFET **source** | **common GND** | |
| All four fan **+ wires** | **+12V** rail | bundled via 3-pin splitters |
| Flyback diode **anode** | **node A** (drain) | 1N5819 |
| Flyback diode **cathode** (stripe) | **+12V** | clamps inductive kickback |
| ESP32 **GND** | **common GND** | mandatory — ESP and fan power must share ground |

### ESP32-S3 Pinout

| GPIO | Function | Notes |
|------|----------|-------|
| 5    | PWM OUT  | 25kHz, drives the MOSFET gate through 220 Ω |
| GND  | Ground   | Must be common with the 12V supply ground |

Tach (GPIO4) is left unconnected: with low-side switching each fan's ground
swings toward 12V during the off-phase, which both corrupts the reading and
would push ~12V into a 3.3V pin. `RPM` reports 0; nothing depends on it.

### Wiring Notes

- **Common ground is mandatory.** ESP GND, MOSFET source, and the 12V supply
  ground must all tie together or nothing switches. Powering the fans from a
  spare PSU 12V connector (rather than a mobo header) keeps the board from doing
  its own control on that rail. If you use a header, set it to 100%/full in BIOS.
- **Use a logic-level MOSFET** (fully on at a 3.3V gate): AO3400 or IRLZ44N. A
  plain IRF540 will not work.
- The 10 kΩ gate pulldown holds the fans off during the second or two between
  power-on and firmware init, then they spin up. Harmless.
- **No firmware change needed.** GPIO5's existing 25kHz PWM drives the gate
  as-is: higher duty = more power = faster, and the lost-link watchdog (100%)
  now means full airflow on failure. Tach is unused, so `RPM` always reports 0.

### USB Connection

Connect ESP32-S3 to internal motherboard USB 2.0 header using a **USB-C to USB 2.0 header cable**:

1. Plug USB-C end into ESP32-S3 COM port
2. Connect header end to motherboard (e.g., JUSB2)
3. Route cable inside case

### Fan Fan-Out (Splitters)

All four 3-pin fans run off one switched rail, so they wire in parallel:

1. Bundle every fan **+ (12V)** wire to the +12V rail (3-pin splitters, one per GPU
   or a single 4-way, whatever fits the routing).
2. Bundle every fan **− (ground)** wire together into "node A" and run that to the
   MOSFET drain.
3. Leave every fan **tach** wire disconnected (see the pinout note above).
4. Tie ESP32 GND, MOSFET source, and the 12V supply ground together.

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
- 4× 30mm 3-pin fans (2 per GPU)
- Logic-level N-channel MOSFET (AO3400 or IRLZ44N)
- 220 Ω gate resistor + 10 kΩ gate pulldown
- 1N5819 (or 1N400x) flyback diode
- 3-pin fan splitters to fan out the 12V rail
- Jumper wires / Dupont connectors
- (Optional) 3D printed enclosure to mount inside case
