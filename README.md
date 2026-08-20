# AirScan ✈️📻

**AirScan** is a high-performance, terminal-based VHF AM aviation radio scanner and automated VOX audio recorder built in Python. Utilizing direct `librtlsdr` C-bindings, NumPy DSP demodulation, and an interactive NCurses TUI, AirScan delivers fast channel sweeping, dynamic auto-squelch, live RF signal metering, per-transmission MP3 recording, and instant audio playback.

---

## Features

* **Interactive NCurses Dashboard:** Real-time channel activity logging, live RF S-meter, SNR/RSSI readouts, and automatic window geometry management.
* **Dual Squelch System:**
  * **Dynamic Relative SNR Auto-Squelch:** Automatically tracks background noise variations and triggers only on active voice transmissions.
  * **Manual dBFS Mode:** Traditional threshold squelch for high-RF interference environments.
* **Selectable Voice Bandpass Filtering:** Toggle instantly between full-bandwidth AM (`RAW`) and a 2nd-order transposed IIR bandpass filter (`BPF`, 300 Hz – 3.5 kHz) for enhanced speech intelligibility.
* **Per-Call VOX MP3 Recording:** Automatic transmission capture segmented into individual MP3 files with `dynaudnorm` dynamic volume leveling.
* **Integrated In-TUI Playback:** Instant one-key playback (`[P]`) for the selected channel or latest overall recording.
* **Direct Hardware C-Bindings:** Interacts directly with `librtlsdr` via `ctypes` for minimum latency and zero GNU Radio dependencies.

---

## Prerequisites & Installation

### 1. System Requirements
* **Operating System:** Linux (Debian, Ubuntu, Linux Mint, Raspberry Pi OS, Arch Linux)
* **Hardware:** Any standard RTL2832U-based RTL-SDR USB dongle with an appropriate VHF antenna.

### 2. Install System Packages
```bash
sudo apt update && sudo apt install -y python3-numpy python3-sounddevice librtlsdr-dev ffmpeg
```

### 3. Clone Repository
```bash
git clone https://github.com/UPMI-Scanner/airscan.git
cd airscan
chmod +x airscan.py
```

---

## Frequency Configuration

Frequencies are managed via a simple CSV file (`frequencies.csv`). If no file exists, AirScan creates a default list on first launch.

Create or edit `frequencies.csv`:
```csv
118.000,Tower
121.500,Emergency Guard
121.900,Ground Control
122.700,UNICOM
122.800,CTAF Local
122.900,Multicom
123.025,Helicopter Air-Air
127.200,Minneapolis Center
134.100,Approach / Departure
```

---

## Usage

### Quick Start
```bash
python3 airscan.py
```

### Command-Line Options

| Option | Description | Default |
| :--- | :--- | :--- |
| `-c, --config <file>` | Path to frequency CSV file | `frequencies.csv` |
| `-g, --gain <dB>` | Tuner hardware RF gain in dB | `36.0` |
| `-s, --squelch <dBFS>` | Manual squelch threshold in dBFS | `-45.0` |
| `-p, --ppm <int>` | Frequency correction in PPM | `0` |
| `-d, --device <index>` | RTL-SDR USB device index | `0` |
| `--no-audio` | Headless mode: disable sound output | `False` |

---

## Interactive Keybindings

While running, manage scanning, audio, and recordings using the following keys:

| Key | Action | Description |
| :--- | :--- | :--- |
| `[SPACE]` | **Hold / Scan** | Pauses scanning on the current channel or resumes sweeping. |
| `[F]` | **Filter Toggle** | Cycles between `RAW` full-bandwidth AM and `BPF` (300–3500 Hz). |
| `[A]` | **Auto Squelch** | Toggles between dynamic relative SNR and manual dBFS squelch. |
| `[R]` | **Record VOX** | Toggles automated MP3 per-call recording on/off. |
| `[P]` | **Instant Play** | Plays the latest recording for highlighted channel; press again to stop. |
| `[C]` | **Clear Stats** | Resets `HITS`, `AIRTIME`, and `LAST HEARD` table counters. |
| `[G]` | **Cycle Gain** | Steps through supported hardware gain values. |
| `[+]` / `[-]` | **Adjust Squelch** | Adjusts SNR threshold (Auto mode) or dBFS level (Manual mode). |
| `[↑]` / `[↓]` or `[k]` / `[j]` | **Navigate** | Moves the selection cursor through the channel list. |
| `[ENTER]` | **Direct Hold** | Immediately tunes to and holds the highlighted channel. |
| `[Q]` | **Quit** | Gracefully closes the tuner, flushes audio, and exits. |

---

## Audio Recordings

When recording (`[R]`) is active, audio files are saved automatically into the `recordings/` directory using the following naming format:
```text
recordings/Airband_YYYYMMDD_HHMMSS_<Freq>MHz_<Channel_Name>.mp3
```

All recordings are processed through FFmpeg's `dynaudnorm` filter to equalize quiet pilots and strong local transmitters to a consistent listening volume.

---

## License

This project is licensed under the MIT License.
