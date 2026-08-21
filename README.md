# AirScan // VHF AM Aviation Radio Scanner & Recorder

A lightweight, real-time VHF aviation SDR scanner and VOX audio recorder built in Python with direct `librtlsdr` C-bindings, NumPy DSP demodulation, auto-squelch SNR tracking, and an interactive NCurses terminal UI.

---

## Which Engine Should I Use?

| Feature | `airscan.py` (Standard) | `airscan_fast.py` (Wideband Fast) |
| :--- | :--- | :--- |
| **Architecture** | Sequential hardware retuning (1.024 MSPS) | Wideband software channelizer (2.048 MSPS) |
| **Channel Capacity** | 1 channel at a time | Simultaneous multi-channel listening |
| **Sweep Latency** | Retunes per channel (~30–50ms hop) | Baseband decimation (0ms latency inside cluster) |
| **Missed Calls** | Possible during long sweeps | Near zero for co-located tower/approach traffic |
| **CPU Usage** | Minimal (~2–4% on a single core) | Low-to-Moderate (~6–10%) |
| **Target Hardware** | Laptops, Raspberry Pi, low-power systems | Dedicated desktop stations, busy airspace hubs |

---

## Key Features

* **Direct `librtlsdr` C-Bindings:** Low-overhead IQ sample retrieval with zero intermediate framework bloat.
* **400ms Audio Preroll Ring Buffer:** Stores pre-trigger audio in memory so opening words of pilot transmissions are never clipped.
* **Hardware Squelch Debounce:** Multi-frame verification filters out tuner clicks during frequency hops.
* **Dead-Air Auto-Purge:** Momentary sub-0.3s noise bursts are automatically purged so recordings contain genuine voice conversations.
* **Adaptive Noise Floor Calibration:** Per-channel dynamic baseline calibration tracks RF noise levels while freezing baseline updates during active transmissions.
* **Voice-Band Filtering (BPF):** Integrated 300 Hz – 3,500 Hz bandpass filter isolates human speech from VHF background hum.
* **Automatic Normalization:** Live audio streams directly to speakers while recorded transmissions pipe through `ffmpeg` dynamic volume normalization (`dynaudnorm`).

---

## Installation & Prerequisites (BunsenLabs / Debian / Ubuntu / Mint)

### 1. Install System Dependencies
```bash
sudo apt update
sudo apt install -y git rtl-sdr librtlsdr-dev ffmpeg libportaudio2 python3-numpy python3-sounddevice
```

### 2. Configure USB Permissions & Kernel Modules
Prevent the default DVB TV tuner driver from claiming the RTL-SDR dongle:
```bash
# Add user to hardware access group
sudo usermod -aG plugdev,audio eddie

# Blacklist default kernel TV drivers
sudo bash -c 'cat << "BL_EOF" > /etc/modprobe.d/blacklist-rtlsdr.conf
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
BL_EOF'
```

### 3. Clone & Set Global Symlinks
```bash
git clone https://github.com/UPMI-Scanner/airscan.git ~/airscan
cd ~/airscan
chmod +x airscan.py airscan_fast.py

# Optional: Add system-wide commands
sudo ln -sf "/home/eddie/airscan/airscan.py" /usr/local/bin/airscan
sudo ln -sf "/home/eddie/airscan/airscan_fast.py" /usr/local/bin/airscan-fast
```

---

## Configuration (`frequencies.csv`)

Add local airport CTAF, tower, approach, and air-to-air frequencies in comma-delimited format:

```csv
122.700,Houghton CTAF
122.800,Ironwood CTAF
122.900,MULTICOM
123.450,Air-to-Air
123.725,Sector 13 Sawyer Hi
133.550,ZMP West UP
```

---

## Usage

* **Run Standard Scanner:**
  ```bash
  airscan
  ```
* **Run Wideband Fast Scanner:**
  ```bash
  airscan-fast
  ```

### CLI Options
* `-c, --config`: Custom frequency CSV path (default: `frequencies.csv`)
* `-g, --gain`: Hardware RF tuner gain in dB (default: `36.0`)
* `-s, --squelch`: Manual squelch threshold in dBFS (default: `-45.0`)
* `-p, --ppm`: RTL-SDR crystal frequency correction (default: `0`)
* `-d, --device`: USB device index (default: `0`)
* `--no-audio`: Headless operation (records without speaker playback)
* `--raw`: Disable speech bandpass filter

---

## Keyboard Controls

| Key | Function | Description |
| :---: | :--- | :--- |
| **`[SPACE]`** | **Hold Channel** | Locks scanner on selected frequency to follow conversations. |
| **`[F]`** | **Voice Filter** | Toggles between `BPF (300-3.5k)` speech isolation and raw AM. |
| **`[A]`** | **Auto Squelch** | Toggles dynamic SNR auto-squelch tracking. |
| **`[+]` / `[-]`** | **Sensitivity** | Adjusts SNR voice threshold in 0.5 dB increments. |
| **`[R]`** | **VOX Record** | Arms/disarms MP3 recording to `~/airscan/recordings/`. |
| **`[P]`** | **Quick Play** | Replays the most recently recorded audio file. |
| **`[C]`** | **Clear Log** | Resets channel hit counts, timers, and timestamps. |
| **`[G]`** | **Cycle Gain** | Steps through supported tuner gain stages. |
| **`[Q]`** | **Quit** | Cleanly exits and releases USB device locks. |
