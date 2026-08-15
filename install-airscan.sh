#!/usr/bin/env bash
set -e

echo "=== Installing System Dependencies ==="
sudo apt update
sudo apt install -y rtl-sdr librtlsdr-dev ffmpeg python3 python3-numpy python3-pip libportaudio2 portaudio19-dev wget

echo "=== Installing Python sounddevice ==="
# Install sounddevice using pip with system-package override for Debian/BunsenLabs
pip3 install --break-system-packages sounddevice 2>/dev/null || pip3 install sounddevice

# Ensure udev rules allow non-root RTL-SDR access
echo "=== Setting RTL-SDR udev Rules ==="
if [ ! -f /etc/udev/rules.d/20-rtlsdr.rules ]; then
    sudo wget -q -O /etc/udev/rules.d/20-rtlsdr.rules https://raw.githubusercontent.com/osmocom/rtl-sdr/master/rtl-sdr.rules
    sudo udevadm control --reload-rules
    sudo udevadm trigger
fi

# Blacklist default DVB-T kernel driver so RTL-SDR is free for SDR use
if [ ! -f /etc/modprobe.d/blacklist-rtl.conf ]; then
    echo "blacklist dvb_usb_rtl28xxu" | sudo tee /etc/modprobe.d/blacklist-rtl.conf > /dev/null
    sudo rmmod dvb_usb_rtl28xxu 2>/dev/null || true
fi

# Create a system-wide launcher shortcut
echo "=== Creating Global 'airscan' Command ==="
sudo ln -sf "$HOME/airscan/airscan.py" /usr/local/bin/airscan
chmod +x "$HOME/airscan/airscan.py"

echo "=== Installation Complete! Run 'airscan' to start ==="
