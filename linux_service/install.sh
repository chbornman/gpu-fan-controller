#!/bin/bash
# GPU Fan Controller - Installation Script

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== GPU Fan Controller Installation ==="

# Check for root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (sudo ./install.sh)"
    exit 1
fi

# Install Python dependencies
echo "Installing Python dependencies..."
pip3 install pyserial pyyaml

# Create directories
echo "Creating directories..."
mkdir -p /opt/gpu_fan_ctrl
mkdir -p /etc/gpu_fan_ctrl

# Copy files
echo "Copying files..."
cp "$SCRIPT_DIR/gpu_fan_ctrl.py" /opt/gpu_fan_ctrl/
chmod +x /opt/gpu_fan_ctrl/gpu_fan_ctrl.py

# Copy config if it doesn't exist
if [ ! -f /etc/gpu_fan_ctrl/config.yaml ]; then
    cp "$SCRIPT_DIR/config.yaml" /etc/gpu_fan_ctrl/
    echo "Installed default config to /etc/gpu_fan_ctrl/config.yaml"
    echo ">>> EDIT THIS FILE to adjust fan curve and serial port <<<"
else
    echo "Config already exists at /etc/gpu_fan_ctrl/config.yaml (not overwritten)"
fi

# Install systemd service
echo "Installing systemd service..."
cp "$SCRIPT_DIR/gpu-fan-ctrl.service" /etc/systemd/system/
systemctl daemon-reload

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit config:    sudo nano /etc/gpu_fan_ctrl/config.yaml"
echo "  2. Check serial:   ls -la /dev/ttyACM*"
echo "  3. Test manually:  sudo python3 /opt/gpu_fan_ctrl/gpu_fan_ctrl.py -v"
echo "  4. Enable service: sudo systemctl enable gpu-fan-ctrl"
echo "  5. Start service:  sudo systemctl start gpu-fan-ctrl"
echo "  6. Check status:   sudo systemctl status gpu-fan-ctrl"
echo "  7. View logs:      journalctl -u gpu-fan-ctrl -f"
echo ""
