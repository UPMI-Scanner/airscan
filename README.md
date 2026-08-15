# AirScan - Terminal SDR Airband Scanner

A lightweight, interactive Terminal UI (TUI) VHF airband scanner for RTL-SDR dongles on Linux. Built with direct Ctypes bindings, real-time AM envelope demodulation, and automated VOX MP3 recording.

---

## Features
* **Fast Channel Hopping:** Rapidly scans user-defined VHF aviation frequencies.
* **Interactive TUI:** Real-time signal strength (RSSI) meter, live squelch adjustment, and channel hit counters.
* **VOX MP3 Recording:** One-key recording captures only active speech without dead air, with automatic volume normalization (AGC).
* **Persistent CSV Management:** Add channels on the fly in-terminal (`A` key) or edit `frequencies.csv`.
* **Zero Heavy GUI Overhead:** Runs cleanly over SSH or on low-power devices.

---

## Requirements
* **OS:** Linux (Linux Mint, Debian, BunsenLabs, MX Linux, Ubuntu)
* **Hardware:** RTL-SDR USB dongle (R820T/R820T2/R860/v4)
* **System Packages:** `rtl-sdr`, `librtlsdr-dev`, `ffmpeg`, `python3`, `python3-numpy`, `python3-pip`
* **Python Library:** `sounddevice`

---

## Quick Install (All Machines)

Run the following commands on any Linux machine to install dependencies, configure udev permissions, and create the global `airscan` shortcut:

```bash
git clone [https://github.com/UPMI-Scanner/airscan.git](https://github.com/UPMI-Scanner/airscan.git) ~/airscan
cd ~/airscan
chmod +x install-airscan.sh
./install-airscan.sh
source ~/.bashrc
