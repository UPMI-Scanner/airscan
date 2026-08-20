#!/usr/bin/env python3
"""
AirScan: VHF AM Aviation Radio Scanner & VOX Audio Recorder
===========================================================
A high-performance SDR aviation receiver built on Python, NumPy, librtlsdr,
and Curses. Features real-time RSSI/SNR auto-squelch, AM demodulation, dynamic
audio normalization, per-transmission MP3 recording, and integrated playback.

Author: UPMI-Scanner
License: MIT
"""

import argparse
import csv
import ctypes
import ctypes.util
from datetime import datetime
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import curses
import numpy as np
import sounddevice as sd

# -------------------------------------------------------------
# OS & C-LEVEL ENVIRONMENT CONFIGURATION
# -------------------------------------------------------------
def set_terminal_size(rows: int, cols: int = 82) -> None:
    """Sets standard ANSI window geometry if supported by the terminal emulator."""
    try:
        sys.stdout.write(f"\x1b[8;{rows};{cols}t")
        sys.stdout.flush()
        time.sleep(0.05)
    except Exception:
        pass


def silence_c_stderr() -> None:
    """Redirects OS-level stderr (fd 2) to /dev/null to protect Curses rendering."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 2)
        os.close(devnull)
    except Exception:
        pass


# -------------------------------------------------------------
# CONSTANTS & DSP CONFIGURATION
# -------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.realpath(__file__))
DEFAULT_CSV_FILE = os.path.join(BASE_DIR, "frequencies.csv")
RECORDINGS_DIR = os.path.join(BASE_DIR, "recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)

SAMPLE_RATE = 1024000
AUDIO_RATE = 32000
DECIMATION = int(SAMPLE_RATE / AUDIO_RATE)  # 32
SDR_CHUNK_SAMPLES = 16384
HANG_CHUNKS = 22                            # ~350 ms voice hang-time
MAX_DWELL_SECONDS = 30.0                    # Safety timeout against stuck carriers

# 31-Tap Channel Anti-Aliasing FIR Filter (6 kHz cutoff at 1.024 MSPS)
_t = np.arange(-15, 16)
_sinc = np.sinc(2 * 6000 / SAMPLE_RATE * _t)
_window = np.hanning(31)
FIR_TAPS = (_sinc * _window / np.sum(_sinc * _window)).astype(np.float32)

DEFAULT_CHANNELS = [
    (118.000, "Tower"),
    (121.500, "Emergency Guard"),
    (121.900, "Ground Control"),
    (122.700, "UNICOM"),
    (122.800, "CTAF Local"),
    (122.900, "Multicom"),
    (123.025, "Helicopter Air-Air"),
    (127.200, "Minneapolis Center"),
    (134.100, "Approach / Departure")
]


# -------------------------------------------------------------
# AUDIO BUFFER & DSP FILTERS
# -------------------------------------------------------------
class AudioRingBuffer:
    """Thread-safe circular ring buffer for real-time soundcard streaming."""

    def __init__(self, capacity: int = 48000):
        self.capacity = capacity
        self.buffer = np.zeros(capacity, dtype=np.float32)
        self.write_pos = 0
        self.read_pos = 0
        self.size = 0
        self.lock = threading.Lock()

    def write(self, data: np.ndarray) -> None:
        n = len(data)
        if n == 0:
            return
        with self.lock:
            if n >= self.capacity:
                self.buffer[:] = data[-self.capacity:]
                self.write_pos = 0
                self.read_pos = 0
                self.size = self.capacity
                return
            end_pos = (self.write_pos + n) % self.capacity
            if self.write_pos + n <= self.capacity:
                self.buffer[self.write_pos:self.write_pos + n] = data
            else:
                first = self.capacity - self.write_pos
                self.buffer[self.write_pos:] = data[:first]
                self.buffer[:n - first] = data[first:]
            self.write_pos = end_pos
            self.size = min(self.capacity, self.size + n)
            if self.size == self.capacity:
                self.read_pos = self.write_pos

    def read(self, outdata: np.ndarray, frames: int) -> None:
        with self.lock:
            if self.size == 0:
                outdata.fill(0)
                return
            n = min(frames, self.size)
            if self.read_pos + n <= self.capacity:
                outdata[:n, 0] = self.buffer[self.read_pos:self.read_pos + n]
            else:
                first = self.capacity - self.read_pos
                outdata[:first, 0] = self.buffer[self.read_pos:]
                outdata[first:n, 0] = self.buffer[:n - first]
            if n < frames:
                outdata[n:, 0].fill(0)
            self.read_pos = (self.read_pos + n) % self.capacity
            self.size -= n


class VoiceBandpassFilter:
    """Direct Form II Transposed IIR bandpass filter (300 Hz - 3500 Hz)."""

    def __init__(self):
        self.b_hp = np.array([0.959203, -1.918406, 0.959203], dtype=np.float32)
        self.a_hp = np.array([1.0, -1.916742, 0.920071], dtype=np.float32)
        self.b_lp = np.array([0.078356, 0.156712, 0.078356], dtype=np.float32)
        self.a_lp = np.array([1.0, -1.067214, 0.380638], dtype=np.float32)
        self.reset()

    def reset(self) -> None:
        self.hp_z1 = self.hp_z2 = 0.0
        self.lp_z1 = self.lp_z2 = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        if len(x) == 0:
            return x
        hp_out = np.empty_like(x)
        b0, b1, b2 = self.b_hp
        a1, a2 = self.a_hp[1], self.a_hp[2]
        z1, z2 = self.hp_z1, self.hp_z2
        for i in range(len(x)):
            xi = x[i]
            yi = b0 * xi + z1
            z1 = b1 * xi - a1 * yi + z2
            z2 = b2 * xi - a2 * yi
            hp_out[i] = yi
        self.hp_z1, self.hp_z2 = z1, z2

        lp_out = np.empty_like(hp_out)
        b0, b1, b2 = self.b_lp
        a1, a2 = self.a_lp[1], self.a_lp[2]
        z1, z2 = self.lp_z1, self.lp_z2
        for i in range(len(hp_out)):
            xi = hp_out[i]
            yi = b0 * xi + z1
            z1 = b1 * xi - a1 * yi + z2
            z2 = b2 * xi - a2 * yi
            lp_out[i] = yi
        self.lp_z1, self.lp_z2 = z1, z2
        return lp_out


# -------------------------------------------------------------
# RTL-SDR CTYPES DRIVER BINDINGS
# -------------------------------------------------------------
def load_rtlsdr():
    """Locates and links the native librtlsdr shared library."""
    lib_path = ctypes.util.find_library('rtlsdr')
    if not lib_path:
        search_paths = [
            '/usr/lib/x86_64-linux-gnu/librtlsdr.so.0',
            '/usr/lib/aarch64-linux-gnu/librtlsdr.so.0',
            '/usr/lib/arm-linux-gnueabihf/librtlsdr.so.0',
            '/usr/lib/librtlsdr.so',
            '/usr/local/lib/librtlsdr.so'
        ]
        for p in search_paths:
            if os.path.exists(p):
                lib_path = p
                break
    if not lib_path:
        return None
    try:
        lib = ctypes.CDLL(lib_path)
        lib.rtlsdr_get_device_count.restype = ctypes.c_uint32
        lib.rtlsdr_open.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32]
        lib.rtlsdr_open.restype = ctypes.c_int
        lib.rtlsdr_close.argtypes = [ctypes.c_void_p]
        lib.rtlsdr_close.restype = ctypes.c_int
        lib.rtlsdr_set_center_freq.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.rtlsdr_set_center_freq.restype = ctypes.c_int
        lib.rtlsdr_set_sample_rate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.rtlsdr_set_sample_rate.restype = ctypes.c_int
        lib.rtlsdr_set_tuner_gain_mode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.rtlsdr_set_tuner_gain_mode.restype = ctypes.c_int
        lib.rtlsdr_set_tuner_gain.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.rtlsdr_set_tuner_gain.restype = ctypes.c_int
        lib.rtlsdr_get_tuner_gains.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        lib.rtlsdr_get_tuner_gains.restype = ctypes.c_int
        lib.rtlsdr_reset_buffer.argtypes = [ctypes.c_void_p]
        lib.rtlsdr_reset_buffer.restype = ctypes.c_int
        lib.rtlsdr_read_sync.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.rtlsdr_read_sync.restype = ctypes.c_int
        return lib
    except Exception:
        return None


class NativeRtlSdr:
    """Thread-safe direct hardware wrapper for librtlsdr."""

    def __init__(self, device_index: int = 0, ppm: int = 0, initial_gain: float = 36.0):
        self.lib = load_rtlsdr()
        if not self.lib:
            raise RuntimeError("Native librtlsdr library not found. Install via: sudo apt install librtlsdr-dev")
        self.dev = ctypes.c_void_p()
        if self.lib.rtlsdr_open(ctypes.byref(self.dev), int(device_index)) < 0:
            raise RuntimeError(f"Failed to open RTL-SDR device index #{device_index}")

        self.lib.rtlsdr_set_sample_rate(self.dev, SAMPLE_RATE)
        if ppm != 0:
            self.lib.rtlsdr_set_freq_correction(self.dev, int(ppm))

        num_gains = self.lib.rtlsdr_get_tuner_gains(self.dev, None)
        if num_gains > 0:
            arr = (ctypes.c_int * num_gains)()
            self.lib.rtlsdr_get_tuner_gains(self.dev, arr)
            self.valid_gains_db = [g / 10.0 for g in arr]
        else:
            self.valid_gains_db = [0.0, 9.0, 14.0, 20.7, 28.0, 36.4, 42.1, 49.6]

        self.lib.rtlsdr_set_tuner_gain_mode(self.dev, 1)
        self.gain = float(initial_gain)
        self.lib.rtlsdr_set_tuner_gain(self.dev, int(self.gain * 10))

        self.buf_size = SDR_CHUNK_SAMPLES * 2
        self.raw_buf = (ctypes.c_uint8 * self.buf_size)()
        self.n_read = ctypes.c_int()
        self.lock = threading.Lock()
        self.lib.rtlsdr_reset_buffer(self.dev)

    def set_frequency(self, freq_hz: float) -> None:
        with self.lock:
            if self.dev:
                self.lib.rtlsdr_set_center_freq(self.dev, int(freq_hz))
                self.lib.rtlsdr_reset_buffer(self.dev)

    def set_gain(self, gain_db: float) -> None:
        with self.lock:
            self.gain = float(gain_db)
            if self.dev:
                self.lib.rtlsdr_set_tuner_gain(self.dev, int(gain_db * 10))

    def read_samples(self) -> np.ndarray:
        with self.lock:
            if not self.dev:
                return None
            ret = self.lib.rtlsdr_read_sync(self.dev, self.raw_buf, self.buf_size, ctypes.byref(self.n_read))
            if ret < 0 or self.n_read.value < self.buf_size:
                return None
            raw = np.frombuffer(self.raw_buf, dtype=np.uint8).astype(np.float32)
            iq = (raw - 127.5) / 127.5
            return (iq[0::2] + 1j * iq[1::2]).astype(np.complex64)

    def close(self) -> None:
        with self.lock:
            if self.dev:
                self.lib.rtlsdr_close(self.dev)
                self.dev = None


# -------------------------------------------------------------
# CORE SCANNING ENGINE
# -------------------------------------------------------------
class AirbandScanner:
    """Core sequential VHF airband scanning and audio demodulation pipeline."""

    def __init__(self, args):
        self.csv_path = args.config
        self.device_index = args.device
        self.ppm = args.ppm
        self.initial_gain = args.gain
        self.squelch = args.squelch
        self.auto_squelch = True
        self.filter_enabled = False
        self.snr_threshold = 4.0
        self.volume = 1.0
        self.no_audio = args.no_audio

        self.running = True
        self.held = False
        self.status = "SCANNING"
        self.current_idx = 0
        self.selected_row = 0
        self.current_rssi = -48.0
        self.current_snr = 0.0
        self.scan_rate = 0.0
        self.agc_peak = 0.05

        self.channels = []
        self._load_csv()

        self.sdr = NativeRtlSdr(self.device_index, self.ppm, self.initial_gain)
        self.voice_filter = VoiceBandpassFilter()
        self.ring_buffer = AudioRingBuffer(capacity=48000)

        self.recording = False
        self.vox_active = False
        self.ffmpeg_proc = None
        self.rec_filename = ""
        self.total_vox_samples = 0
        self.playback_proc = None
        self.playback_file = ""

        self.ffmpeg_available = bool(shutil.which("ffmpeg"))
        self.ffplay_available = bool(shutil.which("ffplay"))

        self.rec_lock = threading.Lock()
        self.channel_lock = threading.Lock()
        self.stream = None

        if not self.no_audio:
            try:
                def audio_cb(outdata, frames, time_info, status):
                    self.ring_buffer.read(outdata, frames)
                self.stream = sd.OutputStream(samplerate=AUDIO_RATE, channels=1, dtype='float32', callback=audio_cb)
                self.stream.start()
            except Exception:
                self.stream = None

    def _load_csv(self) -> None:
        if not os.path.exists(self.csv_path):
            with open(self.csv_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                for freq, name in DEFAULT_CHANNELS:
                    writer.writerow([f"{freq:.3f}", name])
        idx = 0
        with open(self.csv_path, 'r', encoding='utf-8') as f:
            for row in csv.reader(f):
                if row and not row[0].startswith('#'):
                    try:
                        self.channels.append({
                            "id": idx,
                            "freq": float(row[0]),
                            "name": row[1] if len(row) > 1 else "Unknown",
                            "hits": 0, "active_sec": 0.0, "last": "0", "noise_floor": -48.0
                        })
                        idx += 1
                    except ValueError:
                        pass

    def demodulate_am(self, iq_samples: np.ndarray) -> np.ndarray:
        i_filt = np.convolve(iq_samples.real, FIR_TAPS, mode='same')[::DECIMATION]
        q_filt = np.convolve(iq_samples.imag, FIR_TAPS, mode='same')[::DECIMATION]
        iq_channel = i_filt + 1j * q_filt

        mag = np.abs(iq_channel)
        audio = mag - np.mean(mag)

        chunk_peak = float(np.max(np.abs(audio))) if len(audio) > 0 else 0.01
        self.agc_peak = max(chunk_peak, (self.agc_peak * 0.96) + (chunk_peak * 0.04))
        target_gain = min(0.85 / max(self.agc_peak, 0.01), 12.0)
        return np.clip(audio * target_gain, -1.0, 1.0).astype(np.float32)

    def toggle_record(self) -> None:
        if not self.ffmpeg_available:
            return
        with self.rec_lock:
            self.recording = not self.recording
            if not self.recording:
                self._stop_ffmpeg()

    def _start_call_recording(self, freq_mhz: float, name: str) -> None:
        if not self.recording or not self.ffmpeg_available or self.ffmpeg_proc:
            return
        clean_name = re.sub(r'[^a-zA-Z0-9_-]', '_', name.strip())
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.rec_filename = os.path.join(RECORDINGS_DIR, f"Airband_{ts}_{freq_mhz:.3f}MHz_{clean_name}.mp3")
        self.total_vox_samples = 0
        cmd = [
            'ffmpeg', '-y', '-f', 's16le', '-ar', str(AUDIO_RATE), '-ac', '1', '-i', '-',
            '-af', 'dynaudnorm=f=75:g=15:p=0.9',
            '-codec:a', 'libmp3lame', '-b:a', '64k', self.rec_filename
        ]
        try:
            self.ffmpeg_proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            self.ffmpeg_proc = None

    def _stop_ffmpeg(self) -> None:
        if self.ffmpeg_proc:
            try:
                if self.ffmpeg_proc.stdin:
                    self.ffmpeg_proc.stdin.close()
                self.ffmpeg_proc.wait(timeout=1.0)
            except Exception:
                try:
                    self.ffmpeg_proc.kill()
                except Exception:
                    pass
            self.ffmpeg_proc = None
        if self.rec_filename and os.path.exists(self.rec_filename):
            try:
                if os.path.getsize(self.rec_filename) < 8000:
                    os.remove(self.rec_filename)
            except Exception:
                pass
        self.rec_filename = ""

    def toggle_playback(self) -> None:
        if not self.ffplay_available:
            return
        if self.playback_proc and self.playback_proc.poll() is None:
            try:
                self.playback_proc.terminate()
                self.playback_proc.wait(timeout=0.5)
            except Exception:
                try:
                    self.playback_proc.kill()
                except Exception:
                    pass
            self.playback_proc = None
            self.playback_file = ""
            return

        if not os.path.exists(RECORDINGS_DIR):
            return

        files = [os.path.join(RECORDINGS_DIR, f) for f in os.listdir(RECORDINGS_DIR) if f.endswith('.mp3')]
        if not files:
            return

        target_file = None
        with self.channel_lock:
            if 0 <= self.selected_row < len(self.channels):
                sel_freq = self.channels[self.selected_row]["freq"]
                ch_matches = [f for f in files if f"_{sel_freq:.3f}MHz_" in f]
                if ch_matches:
                    ch_matches.sort(key=os.path.getmtime, reverse=True)
                    target_file = ch_matches[0]

        if not target_file:
            files.sort(key=os.path.getmtime, reverse=True)
            target_file = files[0]

        self.playback_file = os.path.basename(target_file)
        cmd = ['ffplay', '-nodisp', '-autoexit', '-loglevel', 'quiet', target_file]
        try:
            self.playback_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            self.playback_proc = None
            self.playback_file = ""

    def worker_loop(self) -> None:
        chunk_time = SDR_CHUNK_SAMPLES / SAMPLE_RATE
        scanned_count = 0
        rate_timer = time.time()

        while self.running:
            try:
                if not self.channels:
                    time.sleep(0.05)
                    continue

                with self.channel_lock:
                    ch = self.channels[self.current_idx]

                self.sdr.set_frequency(ch["freq"] * 1e6)
                time.sleep(0.012)

                iq = self.sdr.read_samples()
                if iq is None:
                    self.sdr.lib.rtlsdr_reset_buffer(self.sdr.dev)
                    time.sleep(0.005)
                    iq = self.sdr.read_samples()
                    if iq is None:
                        if not self.held:
                            self.current_idx = (self.current_idx + 1) % len(self.channels)
                        continue

                scanned_count += 1

                chan_iq = iq.reshape(-1, DECIMATION).mean(axis=1)
                p = float(np.mean(chan_iq.real**2 + chan_iq.imag**2))
                rssi = float(10 * np.log10(p + 1e-12))
                self.current_rssi = rssi

                ch["noise_floor"] = (ch["noise_floor"] * 0.96) + (min(rssi, ch["noise_floor"] + 2.0) * 0.04)
                snr = rssi - ch["noise_floor"]
                self.current_snr = max(0.0, snr)

                is_active = (self.current_snr >= self.snr_threshold) if self.auto_squelch else (rssi > self.squelch)

                if is_active or self.held:
                    self.status = "LOCKED" if not self.held else "HOLD"
                    with self.channel_lock:
                        ch["hits"] += 1
                        ch["last"] = datetime.now().strftime("%I:%M:%S %p")

                    with self.rec_lock:
                        if self.recording:
                            self._start_call_recording(ch["freq"], ch["name"])

                    hang_counter = 0
                    dwell_start = time.time()

                    while self.running and (self.held or (hang_counter < HANG_CHUNKS and (time.time() - dwell_start < MAX_DWELL_SECONDS))):
                        iq = self.sdr.read_samples()
                        if iq is None:
                            time.sleep(0.005)
                            continue

                        chan_iq = iq.reshape(-1, DECIMATION).mean(axis=1)
                        p = float(np.mean(chan_iq.real**2 + chan_iq.imag**2))
                        self.current_rssi = float(10 * np.log10(p + 1e-12))
                        self.current_snr = max(0.0, self.current_rssi - ch["noise_floor"])

                        chan_active = (self.current_snr >= self.snr_threshold - 1.5) if self.auto_squelch else (self.current_rssi >= self.squelch)
                        if not chan_active and not self.held:
                            hang_counter += 1
                        else:
                            hang_counter = 0

                        is_voice = chan_active or (hang_counter < HANG_CHUNKS)
                        self.vox_active = is_voice

                        if is_voice:
                            with self.channel_lock:
                                ch["active_sec"] += chunk_time

                        raw_audio = self.demodulate_am(iq)
                        filt_audio = self.voice_filter.process(raw_audio) if self.filter_enabled else raw_audio

                        if self.stream and not self.no_audio:
                            self.ring_buffer.write(np.clip(filt_audio * self.volume, -1.0, 1.0))

                        if self.recording and self.ffmpeg_proc:
                            with self.rec_lock:
                                if self.ffmpeg_proc and self.ffmpeg_proc.stdin:
                                    pcm16 = (filt_audio * 32767).astype(np.int16).tobytes()
                                    try:
                                        self.ffmpeg_proc.stdin.write(pcm16)
                                        self.ffmpeg_proc.stdin.flush()
                                        self.total_vox_samples += len(filt_audio)
                                    except Exception:
                                        pass

                    with self.rec_lock:
                        if self.recording:
                            self._stop_ffmpeg()

                    self.status = "SCANNING"
                    self.vox_active = False

                if not self.held:
                    self.current_idx = (self.current_idx + 1) % len(self.channels)

                if time.time() - rate_timer >= 1.0:
                    self.scan_rate = scanned_count / (time.time() - rate_timer)
                    scanned_count = 0
                    rate_timer = time.time()

            except Exception:
                time.sleep(0.01)

    def close(self) -> None:
        self.running = False
        with self.rec_lock:
            self._stop_ffmpeg()
        if self.playback_proc:
            try:
                self.playback_proc.kill()
            except Exception:
                pass
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
        if self.sdr:
            self.sdr.close()


# -------------------------------------------------------------
# NCURSES DASHBOARD INTERFACE
# -------------------------------------------------------------
def safe_addstr(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
    """Safe string printing with explicit terminal boundary clipping."""
    try:
        h, w = stdscr.getmaxyx()
        if 0 <= y < h and 0 <= x < w:
            stdscr.addstr(y, x, text[:w - x - 1], attr)
    except curses.error:
        pass


def draw_meter(rssi: float, snr: float = 0.0, is_locked: bool = False, width: int = 10) -> str:
    """Renders real-time dynamic RF S-meter bar."""
    try:
        if np.isnan(rssi) or np.isinf(rssi):
            rssi = -70.0
        if is_locked:
            ratio = max(0.0, min(1.0, float(snr) / 20.0))
        else:
            ratio = max(0.0, min(1.0, (float(rssi) - (-70.0)) / 45.0))
        filled = int(round(ratio * width))
        filled = max(0, min(width, filled))
        return "█" * filled + "░" * (width - filled)
    except Exception:
        return "░" * width


def curses_main(stdscr, scanner: AirbandScanner) -> None:
    curses.curs_set(0)
    curses.use_default_colors()
    for i in range(1, 8):
        curses.init_pair(i, i, -1)
    stdscr.nodelay(True)
    stdscr.timeout(50)

    worker_thread = threading.Thread(target=scanner.worker_loop, daemon=True)
    worker_thread.start()

    while scanner.running:
        h, w = stdscr.getmaxyx()
        if curses.is_term_resized(h, w):
            curses.resizeterm(*stdscr.getmaxyx())
            stdscr.clear()

        if h < 14 or w < 65:
            stdscr.erase()
            safe_addstr(stdscr, 0, 0, "Terminal too small (min 65x14)", curses.A_BOLD)
            stdscr.refresh()
            time.sleep(0.05)
            continue

        stdscr.erase()

        # Banner Header
        safe_addstr(stdscr, 1, 2, "╔" + "═" * (w - 6) + "╗", curses.color_pair(6) | curses.A_BOLD)
        title = " AIRSCAN // VHF AVIATION SCANNER "
        safe_addstr(stdscr, 1, (w - len(title)) // 2, title, curses.color_pair(6) | curses.A_BOLD)

        # Status Bar (Row 3)
        st_col = curses.color_pair(2) | curses.A_BOLD if scanner.status in ["LOCKED", "HOLD"] else curses.color_pair(4)
        safe_addstr(stdscr, 3, 4, f"STATUS: [{scanner.status}]", st_col)
        safe_addstr(stdscr, 3, 23, f"SPEED: {scanner.scan_rate:3.0f} ch/s", curses.color_pair(3) | curses.A_BOLD)
        safe_addstr(stdscr, 3, 40, f"GAIN: {scanner.sdr.gain:4.1f}dB", curses.A_DIM)

        sq_str = f"AUTO (+{scanner.snr_threshold:.1f}dB)" if scanner.auto_squelch else f"MAN ({scanner.squelch:5.1f}dBFS)"
        safe_addstr(stdscr, 3, 56, f"SQ: {sq_str}", curses.color_pair(5))

        # Telemetry & Meter (Row 4)
        is_active_lock = scanner.status in ["LOCKED", "HOLD"]
        meter_str = draw_meter(scanner.current_rssi, scanner.current_snr, is_locked=is_active_lock, width=10)
        meter_col = curses.color_pair(2) | curses.A_BOLD if is_active_lock else curses.color_pair(6)
        safe_addstr(stdscr, 4, 4, f"SIG: [{meter_str}] {scanner.current_rssi:5.1f} dBFS", meter_col)

        snr_attr = curses.color_pair(2) | curses.A_BOLD if scanner.current_snr >= scanner.snr_threshold else curses.A_DIM
        safe_addstr(stdscr, 4, 34, f"SNR: {scanner.current_snr:+5.1f} dB", snr_attr)

        rec_sec = int(scanner.total_vox_samples / AUDIO_RATE)
        rec_time = f"{rec_sec // 60:02d}:{rec_sec % 60:02d}"

        if scanner.recording:
            if scanner.vox_active:
                rec_str = f"● REC {rec_time}"
                rec_col = curses.color_pair(1) | curses.A_BOLD
            else:
                rec_str = f"○ WAIT {rec_time}"
                rec_col = curses.color_pair(3)
        else:
            rec_str = "STANDBY"
            rec_col = curses.A_DIM

        safe_addstr(stdscr, 4, 52, f"MP3 VOX: [{rec_str:<11}]", rec_col)

        safe_addstr(stdscr, 5, 2, "╟" + "─" * (w - 6) + "╢", curses.color_pair(6))

        # Channel Table
        header = f"  {'CH':<4} {'FREQ (MHz)':<12} {'CHANNEL / AGENCY':<24} {'HITS':<6} {'AIRTIME':<9} {'LAST HEARD':<12}"
        safe_addstr(stdscr, 6, 2, header[:w-4], curses.A_BOLD | curses.color_pair(6))

        table_rows = max(1, h - 11)
        with scanner.channel_lock:
            n_ch = len(scanner.channels)
            top = max(0, min(scanner.selected_row - table_rows // 2, n_ch - table_rows))
            for i in range(top, min(n_ch, top + table_rows)):
                row_y = 7 + (i - top)
                ch = scanner.channels[i]
                is_cur = (i == scanner.current_idx)
                is_sel = (i == scanner.selected_row)
                marker = "► " if is_cur else "  "
                airtime = f"{int(ch['active_sec']//60):02d}:{int(ch['active_sec']%60):02d}"
                line = f"{marker}{i+1:<4} {ch['freq']:<12.3f} {ch['name']:<24} {ch['hits']:<6} {airtime:<9} {ch['last']:<12}"
                attr = curses.A_REVERSE if is_sel else (curses.color_pair(2) | curses.A_BOLD if is_cur else 0)
                safe_addstr(stdscr, row_y, 2, line[:w-4], attr)

        # Footer
        safe_addstr(stdscr, h - 3, 2, "╟" + "─" * (w - 6) + "╢", curses.color_pair(6))
        flt_txt = "BPF" if scanner.filter_enabled else "RAW"
        is_playing = scanner.playback_proc is not None and scanner.playback_proc.poll() is None
        p_txt = "Stop" if is_playing else "Play"
        help_bar = f" [SPACE] Hold  [F] {flt_txt}  [A] Auto  [R] Rec  [P] {p_txt}  [C] Clear  [G] Gain  [Q] Quit "
        bar_col = curses.color_pair(2) | curses.A_BOLD if is_playing else (curses.color_pair(7) | curses.A_BOLD)
        safe_addstr(stdscr, h - 2, 3, help_bar[:w-4], bar_col)
        stdscr.refresh()

        # Keyboard Controls
        try:
            k = stdscr.getch()
        except curses.error:
            k = -1

        if k in [ord('q'), ord('Q')]:
            scanner.running = False
            break
        elif k == ord(' '):
            scanner.held = not scanner.held
        elif k in [ord('f'), ord('F')]:
            scanner.filter_enabled = not scanner.filter_enabled
        elif k in [ord('a'), ord('A')]:
            scanner.auto_squelch = not scanner.auto_squelch
        elif k in [ord('r'), ord('R')]:
            scanner.toggle_record()
        elif k in [ord('p'), ord('P')]:
            scanner.toggle_playback()
        elif k in [curses.KEY_UP, ord('k')]:
            scanner.selected_row = max(0, scanner.selected_row - 1)
        elif k in [curses.KEY_DOWN, ord('j')]:
            scanner.selected_row = min(len(scanner.channels) - 1, scanner.selected_row + 1)
        elif k in [10, 13]:
            scanner.current_idx = scanner.selected_row
            scanner.held = True
        elif k in [ord('+'), ord('=')]:
            if scanner.auto_squelch:
                scanner.snr_threshold = min(20.0, scanner.snr_threshold + 0.5)
            else:
                scanner.squelch = min(0.0, scanner.squelch + 1.0)
        elif k in [ord('-'), ord('_')]:
            if scanner.auto_squelch:
                scanner.snr_threshold = max(1.5, scanner.snr_threshold - 0.5)
            else:
                scanner.squelch = max(-80.0, scanner.squelch - 1.0)
        elif k in [ord('g'), ord('G')]:
            gains = scanner.sdr.valid_gains_db
            cur = scanner.sdr.gain
            idx = gains.index(cur) if cur in gains else 0
            scanner.sdr.set_gain(gains[(idx + 1) % len(gains)])
        elif k in [ord('c'), ord('C')]:
            with scanner.channel_lock:
                for c in scanner.channels:
                    c["hits"], c["active_sec"], c["last"] = 0, 0.0, "0"


# -------------------------------------------------------------
# APPLICATION ENTRYPOINT
# -------------------------------------------------------------
def main():
    silence_c_stderr()
    parser = argparse.ArgumentParser(
        description="AirScan: Professional VHF AM Aviation Scanner",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("-d", "--device", type=int, default=0, help="RTL-SDR device index")
    parser.add_argument("-g", "--gain", type=float, default=36.0, help="Hardware RF gain in dB")
    parser.add_argument("-s", "--squelch", type=float, default=-45.0, help="Manual squelch threshold in dBFS")
    parser.add_argument("-p", "--ppm", type=int, default=0, help="Oscillator frequency correction in PPM")
    parser.add_argument("-c", "--config", type=str, default=DEFAULT_CSV_FILE, help="Path to channel CSV frequency file")
    parser.add_argument("--no-audio", action="store_true", help="Disable local soundcard audio output")
    args = parser.parse_args()

    scanner = AirbandScanner(args)
    needed_rows = max(24, len(scanner.channels) + 11)
    set_terminal_size(needed_rows, 82)

    def handle_sig(sig, frame):
        scanner.running = False

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    try:
        curses.wrapper(curses_main, scanner)
    finally:
        scanner.close()


if __name__ == "__main__":
    main()
