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

```bash
git clone [https://github.com/UPMI-Scanner/airscan.git](https://github.com/UPMI-Scanner/airscan.git) ~/airscan
cd ~/airscan
chmod +x install-airscan.sh
./install-airscan.sh
source ~/.bashrc
```

To launch the scanner at any time:

```bash
airscan
```

---

## Interactive Keybindings

* **`A`** : **Add Channel** - Prompts for frequency (MHz) and agency name, inserting it into live scan and saving to CSV
* **`R`** : **Toggle VOX Recording** - Captures un-squelched speech directly into timestamped MP3 files in `./recordings/`
* **`+` / `-`** : **Adjust Squelch** - Increases or decreases threshold in real time (visible on S-Meter as `│`)
* **`SPACE`** : **Hold / Resume** - Toggles hold mode on the currently active or highlighted channel
* **`UP` / `DOWN` + `ENTER`** : **Manual Channel Select** - Scroll through frequency list and instantly jump to channel
* **`G`** : **Cycle SDR Gain** - Steps through valid RTL-SDR hardware tuner gain stages
* **`[` / `]`** : **Volume Control** - Increases or decreases system audio output amplification
* **`Q`** : **Quit** - Stops audio streams, closes active recording files, and exits cleanly

---

## Default Frequency Configuration

By default, AirScan initializes with Upper Peninsula (U.P.) regional aviation channels:

* `123.025 MHz` - Medical Helicopter Air-to-Air / Landing Zones
* `123.050 MHz` - Heliport UNICOM (Hospital Helipads)
* `122.900 MHz` - MULTICOM (Remote LZs & Helo Coordination)
* `119.975 MHz` - Sawyer Tower / CTAF (Marquette - KSAW)
* `121.650 MHz` - Sawyer Ground (Marquette - KSAW)
* `119.100 MHz` - Minneapolis Center (Gwinn RCAG / KSAW Approach)
* `122.700 MHz` - Houghton CTAF / UNICOM (KCMX)
* `133.550 MHz` - Minneapolis Center (Hancock & Ironwood RCAGs)
* `122.800 MHz` - Ironwood CTAF / UNICOM (KIWD)

You can customize this list anytime by pressing `A` in the app or directly editing `frequencies.csv`.

---

## License

This project is licensed under the GPL-3.0 License.
