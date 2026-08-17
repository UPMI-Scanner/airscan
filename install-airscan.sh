#!/usr/bin/env bash
set -e

echo "=== Installing System Dependencies ==="
sudo apt update
# Try installing rtl-sdr packages, but continue if they are held/custom compiled
sudo apt install -y rtl-sdr librtlsdr-dev 2>/dev/null || sudo apt install -y --allow-change-held-packages rtl-sdr librtlsdr-dev 2>/dev/null || echo "Using existing librtlsdr installation..."
sudo apt install -y ffmpeg python3 python3-numpy python3-pip libportaudio2 portaudio19-dev wget

echo "=== Installing Python sounddevice ==="
pip3 install --break-system-packages sounddevice 2>/dev/null || pip3 install sounddevice

echo "=== Setting RTL-SDR udev Rules ==="
if [ ! -f /etc/udev/rules.d/20-rtlsdr.rules ]; then
    sudo wget -q -O /etc/udev/rules.d/20-rtlsdr.rules https://raw.githubusercontent.com/osmocom/rtl-sdr/master/rtl-sdr.rules
    sudo udevadm control --reload-rules
    sudo udevadm trigger
fi

if [ ! -f /etc/modprobe.d/blacklist-rtl.conf ]; then
    echo "blacklist dvb_usb_rtl28xxu" | sudo tee /etc/modprobe.d/blacklist-rtl.conf > /dev/null
    sudo rmmod dvb_usb_rtl28xxu 2>/dev/null || true
fi

echo "=== Creating Global 'airscan' Command ==="
sudo ln -sf "$HOME/airscan/airscan.py" /usr/local/bin/airscan
chmod +x "$HOME/airscan/airscan.py"

echo "=== Installation Complete! Run 'airscan' to start ==="
