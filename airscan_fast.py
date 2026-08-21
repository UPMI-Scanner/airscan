#!/usr/bin/env python3
"""
AirScan Fast: High-Speed Multi-Channel VHF Aviation SDR Scanner
Monitors multiple channels simultaneously via 2 MHz wideband baseband decimation.
"""

import os
import sys
import time
import math
import ctypes
import ctypes.util
import argparse
import curses
import subprocess
import queue
import collections
from dataclasses import dataclass
from typing import List, Optional, Tuple
import numpy as np
import sounddevice as sd

# =====================================================================
# SYSTEM & DSP CONSTANTS
# =====================================================================
SDR_SAMPLE_RATE: int = 2048000
AUDIO_RATE: int = 32000
DECIMATION: int = SDR_SAMPLE_RATE // AUDIO_RATE  # 64
CHUNK_SAMPLES: int = 16384
PREROLL_CHUNKS: int = 8

def _generate_fir_filter(taps: int = 31, cutoff_hz: float = 3200.0, fs: float = SDR_SAMPLE_RATE) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2.0
    fc = cutoff_hz / fs
    h = np.sinc(2.0 * fc * n) * (2.0 * fc)
    h *= np.hamming(taps)
    return (h / np.sum(h)).astype(np.float32)

FIR_TAPS: np.ndarray = _generate_fir_filter(31, 3200.0, SDR_SAMPLE_RATE)

@dataclass
class Channel:
    freq_hz: int
    name: str
    hits: int = 0
    total_airtime_sec: float = 0.0
    last_heard: str = ""
    noise_floor_db: float = -90.0
    noise_initialized: bool = False
    consecutive_hits: int = 0

class CStderrSilencer:
    def __enter__(self):
        sys.stderr.flush()
        self.orig_stderr_fd = sys.stderr.fileno()
        self.saved_stderr_fd = os.dup(self.orig_stderr_fd)
        self.devnull_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(self.devnull_fd, self.orig_stderr_fd)
        os.close(self.devnull_fd)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        sys.stderr.flush()
        os.dup2(self.saved_stderr_fd, self.orig_stderr_fd)
        os.close(self.saved_stderr_fd)

class LibRTLSDR:
    def __init__(self, device_index: int = 0):
        lib_path = ctypes.util.find_library("rtlsdr") or "librtlsdr.so.0" or "librtlsdr.so"
        try:
            self.lib = ctypes.CDLL(lib_path)
        except OSError as err:
            raise RuntimeError(f"Unable to load librtlsdr: {err}. Please run: sudo apt install librtlsdr-dev")

        self._setup_prototypes()
        self.dev = ctypes.c_void_p()
        with CStderrSilencer():
            ret = self.lib.rtlsdr_open(ctypes.byref(self.dev), device_index)
        if ret != 0 or not self.dev:
            raise RuntimeError(f"Failed to open RTL-SDR device index {device_index}.")

        self.supported_gains = self._get_gains()

    def _setup_prototypes(self):
        self.lib.rtlsdr_open.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32]
        self.lib.rtlsdr_open.restype = ctypes.c_int
        self.lib.rtlsdr_close.argtypes = [ctypes.c_void_p]
        self.lib.rtlsdr_close.restype = ctypes.c_int
        self.lib.rtlsdr_set_center_freq.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.lib.rtlsdr_set_center_freq.restype = ctypes.c_int
        self.lib.rtlsdr_set_sample_rate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.lib.rtlsdr_set_sample_rate.restype = ctypes.c_int
        self.lib.rtlsdr_set_tuner_gain_mode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.rtlsdr_set_tuner_gain_mode.restype = ctypes.c_int
        self.lib.rtlsdr_set_tuner_gain.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.rtlsdr_set_tuner_gain.restype = ctypes.c_int
        self.lib.rtlsdr_set_freq_correction.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.rtlsdr_set_freq_correction.restype = ctypes.c_int
        self.lib.rtlsdr_reset_buffer.argtypes = [ctypes.c_void_p]
        self.lib.rtlsdr_reset_buffer.restype = ctypes.c_int
        self.lib.rtlsdr_read_sync.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self.lib.rtlsdr_read_sync.restype = ctypes.c_int
        self.lib.rtlsdr_get_tuner_gains.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        self.lib.rtlsdr_get_tuner_gains.restype = ctypes.c_int

    def _get_gains(self) -> List[float]:
        num_gains = self.lib.rtlsdr_get_tuner_gains(self.dev, None)
        if num_gains <= 0:
            return [0.0, 15.0, 28.0, 36.0, 42.1, 49.6]
        gains_arr = (ctypes.c_int * num_gains)()
        self.lib.rtlsdr_get_tuner_gains(self.dev, gains_arr)
        return [g / 10.0 for g in gains_arr]

    def set_sample_rate(self, rate: int):
        self.lib.rtlsdr_set_sample_rate(self.dev, rate)

    def set_center_freq(self, freq_hz: int):
        self.lib.rtlsdr_set_center_freq(self.dev, freq_hz)

    def set_gain(self, gain_db: float):
        self.lib.rtlsdr_set_tuner_gain_mode(self.dev, 1)
        self.lib.rtlsdr_set_tuner_gain(self.dev, int(gain_db * 10))

    def set_ppm(self, ppm: int):
        self.lib.rtlsdr_set_freq_correction(self.dev, ppm)

    def reset_buffer(self):
        self.lib.rtlsdr_reset_buffer(self.dev)

    def read_samples(self, num_samples: int) -> np.ndarray:
        raw_bytes = (ctypes.c_ubyte * (num_samples * 2))()
        n_read = ctypes.c_int()
        ret = self.lib.rtlsdr_read_sync(self.dev, ctypes.byref(raw_bytes), num_samples * 2, ctypes.byref(n_read))
        if ret != 0:
            return np.zeros(num_samples, dtype=np.complex64)
        data = np.frombuffer(raw_bytes, dtype=np.uint8).astype(np.float32)
        data = (data - 127.5) / 127.5
        return data[0::2] + 1j * data[1::2]

    def close(self):
        if self.dev:
            with CStderrSilencer():
                self.lib.rtlsdr_close(self.dev)
            self.dev = None

class VoiceBandpassFilter:
    def __init__(self, sample_rate: int = AUDIO_RATE, low_cut: float = 300.0, high_cut: float = 3500.0):
        self.enabled = True
        self.sr = sample_rate
        self.a1, self.a2 = 0.0, 0.0
        self.b0, self.b1, self.b2 = 1.0, 0.0, 0.0
        self.z1, self.z2 = 0.0, 0.0
        self._calculate_coefficients(low_cut, high_cut)

    def _calculate_coefficients(self, low: float, high: float):
        w0 = 2.0 * math.pi * math.sqrt(low * high) / self.sr
        bw = (high - low) / math.sqrt(low * high)
        alpha = math.sin(w0) * math.sinh(math.log(2.0) / 2.0 * bw * w0 / math.sin(w0))
        b0 = alpha
        b1 = 0.0
        b2 = -alpha
        a0 = 1.0 + alpha
        a1 = -2.0 * math.cos(w0)
        a2 = 1.0 - alpha
        self.b0, self.b1, self.b2 = b0 / a0, b1 / a0, b2 / a0
        self.a1, self.a2 = a1 / a0, a2 / a0

    def process(self, audio: np.ndarray) -> np.ndarray:
        if not self.enabled or len(audio) == 0:
            return audio
        out = np.empty_like(audio)
        z1, z2 = self.z1, self.z2
        b0, b1, b2 = self.b0, self.b1, self.b2
        a1, a2 = self.a1, self.a2
        for i in range(len(audio)):
            x = audio[i]
            y = b0 * x + z1
            z1 = b1 * x - a1 * y + z2
            z2 = b2 * x - a2 * y
            out[i] = y
        self.z1, self.z2 = z1, z2
        return out

class AudioRecorder:
    def __init__(self, record_dir: str = ""):
        self.record_dir = os.path.expanduser("~/airscan/recordings") if not record_dir else os.path.expanduser(record_dir)
        os.makedirs(self.record_dir, exist_ok=True)
        self.process: Optional[subprocess.Popen] = None
        self.current_filename: str = ""
        self.samples_written: int = 0

    def start(self, freq_hz: int, name: str, preroll: List[np.ndarray]):
        self.stop()
        ts = time.strftime("%Y%m%d_%H%M%S")
        clean_name = "".join(c if c.isalnum() else "_" for c in name).strip("_")
        self.current_filename = os.path.join(
            self.record_dir, f"Airband_{ts}_{freq_hz/1e6:.3f}MHz_{clean_name}.mp3"
        )
        self.samples_written = 0
        cmd = [
            "ffmpeg", "-y", "-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", "1",
            "-i", "pipe:0", "-af", "dynaudnorm=f=150:g=15", "-q:a", "2",
            self.current_filename
        ]
        self.process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        for chunk in preroll:
            self.write(chunk)

    def write(self, audio: np.ndarray):
        if self.process and self.process.stdin:
            pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
            try:
                self.process.stdin.write(pcm)
                self.process.stdin.flush()
                self.samples_written += len(audio)
            except (BrokenPipeError, OSError):
                self.stop()

    def stop(self):
        if self.process:
            if self.process.stdin:
                try:
                    self.process.stdin.close()
                except OSError:
                    pass
            self.process.wait()
            self.process = None

            duration_sec = self.samples_written / AUDIO_RATE
            if duration_sec < 0.3 and self.current_filename and os.path.exists(self.current_filename):
                try:
                    os.remove(self.current_filename)
                except OSError:
                    pass

class FastAirScanEngine:
    def __init__(self, channels: List[Channel], gain: float = 36.0, squelch_db: float = -45.0,
                 ppm: int = 0, dev_idx: int = 0, no_audio: bool = False, raw_filter: bool = False):
        self.channels = channels
        self.gain = gain
        self.manual_squelch_db = squelch_db
        self.auto_squelch = True
        self.snr_threshold_db = 6.5
        self.recording_enabled = False
        self.no_audio = no_audio

        self.sdr = LibRTLSDR(dev_idx)
        self.sdr.set_sample_rate(SDR_SAMPLE_RATE)
        self.sdr.set_gain(self.gain)
        self.sdr.set_ppm(ppm)

        self.voice_filter = VoiceBandpassFilter()
        self.voice_filter.enabled = not raw_filter
        self.recorder = AudioRecorder()

        self.preroll_buffers: collections.defaultdict = collections.defaultdict(
            lambda: collections.deque(maxlen=PREROLL_CHUNKS)
        )

        self.current_channel: Optional[Channel] = None
        self.hold_channel: Optional[Channel] = None
        self.selected_idx: int = 0

        self.running = True
        self.squelch_open = False
        self.hang_count = 0
        self.max_dwell_count = 0
        self.cluster_dwell_ticks = 0
        self.agc_peak = 0.05

        self.current_rssi = -90.0
        self.current_snr = 0.0
        self.total_vox_samples = 0
        self.total_session_samples = 0

        self.audio_queue = queue.Queue(maxsize=30)
        self.playback_proc: Optional[subprocess.Popen] = None
        self.latest_recording_path: str = ""

        self.clusters = self._build_clusters()
        self.cluster_idx = 0
        self.current_center_freq = 0

        if not self.no_audio:
            self.stream = sd.OutputStream(
                samplerate=AUDIO_RATE, channels=1, dtype='float32',
                callback=self._audio_callback, blocksize=512
            )
            self.stream.start()
        else:
            self.stream = None

    def _build_clusters(self) -> List[Tuple[int, List[Channel]]]:
        if not self.channels:
            return []
        sorted_chans = sorted(self.channels, key=lambda c: c.freq_hz)
        clusters = []
        cur_list = [sorted_chans[0]]
        cur_min = sorted_chans[0].freq_hz

        for ch in sorted_chans[1:]:
            if ch.freq_hz - cur_min <= 1600000:
                cur_list.append(ch)
            else:
                center = (cur_min + cur_list[-1].freq_hz) // 2
                clusters.append((center, cur_list))
                cur_list = [ch]
                cur_min = ch.freq_hz

        if cur_list:
            center = (cur_min + cur_list[-1].freq_hz) // 2
            clusters.append((center, cur_list))
        return clusters

    def _audio_callback(self, outdata, frames, time_info, status):
        try:
            chunk = self.audio_queue.get_nowait()
            n = min(len(chunk), frames)
            outdata[:n, 0] = chunk[:n]
            if n < frames:
                outdata[n:, 0] = 0.0
        except queue.Empty:
            outdata.fill(0.0)

    def demodulate_am(self, chan_iq: np.ndarray) -> np.ndarray:
        mag = np.abs(chan_iq)
        audio = mag - np.mean(mag)
        chunk_peak = float(np.max(np.abs(audio))) if len(audio) > 0 else 0.01
        self.agc_peak = max(chunk_peak, (self.agc_peak * 0.96) + (chunk_peak * 0.04))
        target_gain = min(0.85 / max(self.agc_peak, 0.01), 12.0)
        return np.clip(audio * target_gain, -1.0, 1.0).astype(np.float32)

    def evaluate_signal(self, chan_iq: np.ndarray, ch: Channel) -> Tuple[float, float, bool, bool]:
        pwr = float(np.mean(np.abs(chan_iq) ** 2))
        rssi_db = 10.0 * math.log10(max(pwr, 1e-12))

        if not ch.noise_initialized:
            ch.noise_floor_db = rssi_db
            ch.noise_initialized = True

        snr_db = max(0.0, rssi_db - ch.noise_floor_db)

        if self.auto_squelch:
            raw_trigger = (snr_db >= self.snr_threshold_db)
            maintain = (snr_db >= max(2.0, self.snr_threshold_db - 3.0))
        else:
            raw_trigger = (rssi_db >= self.manual_squelch_db)
            maintain = (rssi_db >= (self.manual_squelch_db - 3.0))

        if raw_trigger:
            ch.consecutive_hits += 1
        else:
            ch.consecutive_hits = 0

        debounced_trigger = (ch.consecutive_hits >= 2)

        if not self.squelch_open and not raw_trigger:
            if rssi_db < ch.noise_floor_db:
                ch.noise_floor_db = (ch.noise_floor_db * 0.85) + (rssi_db * 0.15)
            else:
                ch.noise_floor_db = (ch.noise_floor_db * 0.96) + (rssi_db * 0.04)

        return rssi_db, snr_db, debounced_trigger, maintain

    def toggle_playback(self):
        if self.playback_proc and self.playback_proc.poll() is None:
            self.playback_proc.terminate()
            self.playback_proc = None
            return

        target_file = ""
        rec_dir = os.path.expanduser("~/airscan/recordings")
        if self.selected_idx < len(self.channels):
            sel_ch = self.channels[self.selected_idx]
            freq_str = f"{sel_ch.freq_hz / 1e6:.3f}MHz"
            files = sorted(
                [os.path.join(rec_dir, f) for f in os.listdir(rec_dir) if freq_str in f and f.endswith(".mp3")],
                key=os.path.getmtime, reverse=True
            ) if os.path.exists(rec_dir) else []
            if files:
                target_file = files[0]

        if not target_file and self.latest_recording_path and os.path.exists(self.latest_recording_path):
            target_file = self.latest_recording_path

        if target_file:
            self.playback_proc = subprocess.Popen(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", target_file],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )

    def cycle_gain(self):
        gains = self.sdr.supported_gains
        if not gains:
            return
        idx = 0
        for i, g in enumerate(gains):
            if abs(g - self.gain) < 0.2:
                idx = (i + 1) % len(gains)
                break
        self.gain = gains[idx]
        self.sdr.set_gain(self.gain)
        for ch in self.channels:
            ch.noise_initialized = False

    def step(self):
        if not self.clusters:
            return

        if self.hold_channel:
            target_center = self.hold_channel.freq_hz
            active_list = [self.hold_channel]
        else:
            target_center, active_list = self.clusters[self.cluster_idx]

        if self.current_center_freq != target_center:
            self.current_center_freq = target_center
            self.sdr.set_center_freq(target_center)
            self.sdr.reset_buffer()
            self.sdr.read_samples(4096)

        raw_iq = self.sdr.read_samples(CHUNK_SAMPLES)
        t = np.arange(len(raw_iq)) / SDR_SAMPLE_RATE

        found_active = False
        peak_rssi = -90.0
        peak_snr = 0.0
        active_filt_audio = None

        for ch in active_list:
            offset_hz = ch.freq_hz - self.current_center_freq
            if abs(offset_hz) > 100:
                rotator = np.exp(-1j * 2.0 * np.pi * offset_hz * t).astype(np.complex64)
                shifted_iq = raw_iq * rotator
            else:
                shifted_iq = raw_iq

            i_filt = np.convolve(shifted_iq.real, FIR_TAPS, mode='same')[::DECIMATION]
            q_filt = np.convolve(shifted_iq.imag, FIR_TAPS, mode='same')[::DECIMATION]
            chan_iq = i_filt + 1j * q_filt

            audio = self.demodulate_am(chan_iq)
            filt_audio = self.voice_filter.process(audio)
            self.preroll_buffers[ch.freq_hz].append(filt_audio)

            rssi, snr, trigger, maintain = self.evaluate_signal(chan_iq, ch)
            if rssi > peak_rssi:
                peak_rssi = rssi
                peak_snr = snr

            if trigger or (self.squelch_open and self.current_channel == ch and maintain):
                found_active = True
                self.current_rssi = rssi
                self.current_snr = snr
                active_filt_audio = filt_audio

                if self.current_channel != ch or not self.squelch_open:
                    self.current_channel = ch
                    ch.hits += 1
                    ch.last_heard = time.strftime("%H:%M:%S")
                    if self.recording_enabled:
                        preroll_data = list(self.preroll_buffers[ch.freq_hz])
                        self.recorder.start(ch.freq_hz, ch.name, preroll_data)
                        self.latest_recording_path = self.recorder.current_filename
                    self.total_vox_samples = 0

                self.squelch_open = True
                self.hang_count = 90
                self.max_dwell_count += 1
                ch.total_airtime_sec += (len(chan_iq) / AUDIO_RATE)

                if self.recording_enabled:
                    self.recorder.write(filt_audio)
                    self.total_vox_samples += len(filt_audio)
                    self.total_session_samples += len(filt_audio)

                if not self.no_audio:
                    try:
                        self.audio_queue.put_nowait(filt_audio)
                    except queue.Full:
                        pass
                break

        if not found_active:
            self.current_rssi = peak_rssi
            self.current_snr = peak_snr
            if self.hang_count > 0 and self.squelch_open and active_filt_audio is not None:
                self.hang_count -= 1
                if self.recording_enabled:
                    self.recorder.write(active_filt_audio)
                    self.total_vox_samples += len(active_filt_audio)
                if not self.no_audio:
                    try:
                        self.audio_queue.put_nowait(active_filt_audio)
                    except queue.Full:
                        pass
            else:
                if self.squelch_open and self.recording_enabled:
                    self.recorder.stop()
                self.squelch_open = False
                self.max_dwell_count = 0
                if not self.hold_channel:
                    self.cluster_dwell_ticks += 1
                    if self.cluster_dwell_ticks >= 4:
                        self.cluster_dwell_ticks = 0
                        self.cluster_idx = (self.cluster_idx + 1) % len(self.clusters)

        if self.max_dwell_count > 1800:
            self.squelch_open = False
            self.hang_count = 0
            if self.recording_enabled:
                self.recorder.stop()

    def close(self):
        self.running = False
        if self.recorder:
            self.recorder.stop()
        if self.playback_proc:
            self.playback_proc.terminate()
        if self.stream:
            self.stream.stop()
            self.stream.close()
        self.sdr.close()

def safe_addstr(win, y: int, x: int, text: str, attr: int = 0):
    max_y, max_x = win.getmaxyx()
    if 0 <= y < max_y and 0 <= x < max_x:
        win.addstr(y, x, text[:max_x - x - 1], attr)

def curses_main(stdscr, scanner: FastAirScanEngine):
    curses.curs_set(0)
    curses.use_default_colors()
    stdscr.keypad(True)
    stdscr.nodelay(True)
    stdscr.timeout(40)

    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_RED, -1)
    curses.init_pair(3, curses.COLOR_CYAN, -1)
    curses.init_pair(4, curses.COLOR_YELLOW, -1)
    curses.init_pair(5, curses.COLOR_MAGENTA, -1)
    curses.init_pair(6, curses.COLOR_WHITE, -1)
    curses.init_pair(7, curses.COLOR_BLUE, -1)

    while scanner.running:
        try:
            key = stdscr.getch()
        except curses.error:
            key = -1

        if key in (ord('q'), ord('Q')):
            break
        elif key == ord(' '):
            if scanner.hold_channel:
                scanner.hold_channel = None
            else:
                scanner.hold_channel = scanner.channels[scanner.selected_idx]
        elif key in (ord('f'), ord('F')):
            scanner.voice_filter.enabled = not scanner.voice_filter.enabled
        elif key in (ord('a'), ord('A')):
            scanner.auto_squelch = not scanner.auto_squelch
        elif key in (ord('r'), ord('R')):
            scanner.recording_enabled = not scanner.recording_enabled
            if not scanner.recording_enabled:
                scanner.recorder.stop()
        elif key in (ord('p'), ord('P')):
            scanner.toggle_playback()
        elif key in (ord('c'), ord('C')):
            scanner.total_session_samples = 0
            scanner.total_vox_samples = 0
            for ch in scanner.channels:
                ch.hits = 0
                ch.total_airtime_sec = 0.0
                ch.last_heard = ""
        elif key in (ord('g'), ord('G')):
            scanner.cycle_gain()
        elif key in (ord('+'), ord('=')):
            if scanner.auto_squelch:
                scanner.snr_threshold_db = min(25.0, scanner.snr_threshold_db + 0.5)
            else:
                scanner.manual_squelch_db = min(-10.0, scanner.manual_squelch_db + 1.0)
        elif key in (ord('-'), ord('_')):
            if scanner.auto_squelch:
                scanner.snr_threshold_db = max(3.0, scanner.snr_threshold_db - 0.5)
            else:
                scanner.manual_squelch_db = max(-80.0, scanner.manual_squelch_db - 1.0)
        elif key in (curses.KEY_UP, ord('k')):
            scanner.selected_idx = (scanner.selected_idx - 1) % len(scanner.channels)
        elif key in (curses.KEY_DOWN, ord('j')):
            scanner.selected_idx = (scanner.selected_idx + 1) % len(scanner.channels)
        elif key in (10, 13, curses.KEY_ENTER):
            scanner.hold_channel = scanner.channels[scanner.selected_idx]

        scanner.step()

        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()

        title = " AIRSCAN FAST // WIDEBAND HYBRID SCANNER & RECORDER "
        safe_addstr(stdscr, 0, 0, "╔" + "═" * (max_x - 2) + "╗", curses.color_pair(7))
        safe_addstr(stdscr, 1, max(2, (max_x - len(title)) // 2), title, curses.color_pair(6) | curses.A_BOLD)
        safe_addstr(stdscr, 2, 0, "╠" + "═" * (max_x - 2) + "╣", curses.color_pair(7))

        sq_str = f"AUTO ({scanner.snr_threshold_db:.1f} dB SNR)" if scanner.auto_squelch else f"MAN ({scanner.manual_squelch_db:.0f} dBFS)"
        flt_str = "BPF (300-3.5k)" if scanner.voice_filter.enabled else "RAW (FULL AM)"
        stat_l1 = f" SQUELCH: [{sq_str:<18}]  GAIN: [{scanner.gain:>4.1f} dB]  FILTER: [{flt_str:<14}]"
        safe_addstr(stdscr, 3, 2, stat_l1, curses.color_pair(6))

        sig_bars = max(0, min(15, int((scanner.current_rssi + 75.0) / 3.0)))
        smeter = "■" * sig_bars + "─" * (15 - sig_bars)
        act_col = curses.color_pair(1) | curses.A_BOLD if scanner.squelch_open else curses.A_DIM
        act_str = "ACTIVE VOICE" if scanner.squelch_open else f"CLUSTER {scanner.cluster_idx + 1}/{len(scanner.clusters)}"

        call_sec = int(scanner.total_vox_samples / AUDIO_RATE)
        tot_sec = int(scanner.total_session_samples / AUDIO_RATE)
        call_time = f"{call_sec // 60:02d}:{call_sec % 60:02d}"
        tot_time = f"{tot_sec // 60:02d}:{tot_sec % 60:02d}"

        if scanner.recording_enabled:
            rec_str = f"● REC {call_time}" if scanner.squelch_open else f"○ WAIT ({tot_time})"
            rec_col = curses.color_pair(2) | curses.A_BOLD if scanner.squelch_open else curses.color_pair(4)
        else:
            rec_str = "STANDBY"
            rec_col = curses.A_DIM

        sig_str = f" SIGNAL: [{smeter}] {scanner.current_rssi:>5.1f} dBFS (SNR: {scanner.current_snr:>4.1f} dB)"
        safe_addstr(stdscr, 4, 2, sig_str, act_col)
        safe_addstr(stdscr, 4, 56, f"STATE: [{act_str:<14}]", act_col)
        safe_addstr(stdscr, 4, 78, f"VOX: [{rec_str:<12}]", rec_col)

        safe_addstr(stdscr, 5, 0, "╠" + "═" * (max_x - 2) + "╣", curses.color_pair(7))
        table_hdr = "    FREQUENCY   CHANNEL NAME             HITS   AIRTIME   NOISE FLR   LAST HEARD "
        safe_addstr(stdscr, 6, 2, table_hdr, curses.color_pair(6) | curses.A_BOLD)

        table_start_row = 7
        footer_height = 3
        avail_table_rows = max(1, max_y - table_start_row - footer_height)
        total_chans = len(scanner.channels)
        sel_idx = scanner.selected_idx

        scroll_offset = 0
        if total_chans > avail_table_rows:
            if sel_idx >= scroll_offset + avail_table_rows:
                scroll_offset = sel_idx - avail_table_rows + 1
            elif sel_idx < scroll_offset:
                scroll_offset = sel_idx
            scroll_offset = max(0, min(scroll_offset, total_chans - avail_table_rows))

        visible_chans = scanner.channels[scroll_offset : scroll_offset + avail_table_rows]
        active_cluster_chans = scanner.clusters[scanner.cluster_idx][1] if scanner.clusters else []

        for i, ch in enumerate(visible_chans):
            actual_idx = scroll_offset + i
            r = table_start_row + i
            is_active = (scanner.current_channel and scanner.current_channel.freq_hz == ch.freq_hz and scanner.squelch_open)
            is_held = (scanner.hold_channel and scanner.hold_channel.freq_hz == ch.freq_hz)
            is_selected = (actual_idx == sel_idx)
            is_in_cluster = (ch in active_cluster_chans and not scanner.squelch_open)

            if is_active:
                prefix = "*"
                attr = curses.color_pair(1) | curses.A_BOLD
            elif is_held:
                prefix = "H"
                attr = curses.color_pair(5) | curses.A_BOLD
            elif is_in_cluster:
                prefix = "~"
                attr = curses.color_pair(4) | curses.A_BOLD
            elif is_selected:
                prefix = ">"
                attr = curses.color_pair(3) | curses.A_BOLD
            else:
                prefix = " "
                attr = curses.color_pair(6)

            mhz = ch.freq_hz / 1e6
            last_hd = ch.last_heard if ch.last_heard else "--:--:--"
            tot_air = f"{int(ch.total_airtime_sec)}s"
            nfloor = f"{ch.noise_floor_db:>5.1f} dBFS" if ch.noise_initialized else "  CALIB "

            line_str = f" {prefix} {mhz:7.3f} MHz  {ch.name:<22} {ch.hits:>5}  {tot_air:>7}  {nfloor:>9}   {last_hd:>8} "
            safe_addstr(stdscr, r, 2, line_str, attr)

        footer_border_row = max(table_start_row + 1, max_y - 3)
        footer_keys_row = max(table_start_row + 2, max_y - 2)
        bottom_row = max(table_start_row + 3, max_y - 1)

        safe_addstr(stdscr, footer_border_row, 0, "╠" + "═" * (max_x - 2) + "╣", curses.color_pair(7))
        help_text = " [SPACE] Hold  [F] Filter  [A] Auto-Sq  [+/-] Sens  [R] Rec  [P] Play  [C] Clear  [G] Gain  [Q] Quit "
        safe_addstr(stdscr, footer_keys_row, 2, help_text, curses.color_pair(6) | curses.A_BOLD)
        if bottom_row < max_y:
            safe_addstr(stdscr, bottom_row, 0, "╚" + "═" * (max_x - 2) + "╝", curses.color_pair(7))

        stdscr.refresh()

def load_frequencies(csv_path: str) -> List[Channel]:
    home_airscan_csv = os.path.expanduser("~/airscan/frequencies.csv")
    if csv_path == "frequencies.csv" or not os.path.isabs(csv_path):
        if os.path.exists(home_airscan_csv):
            csv_path = home_airscan_csv
        elif not os.path.exists(csv_path):
            script_real = os.path.realpath(__file__)
            candidate = os.path.join(os.path.dirname(script_real), "frequencies.csv")
            csv_path = candidate if os.path.exists(candidate) else home_airscan_csv

    if not os.path.exists(csv_path):
        return []

    channels: List[Channel] = []
    with open(csv_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line in (",", ";", "\t"):
                continue

            delim = "," if "," in line else ("\t" if "\t" in line else ";")
            parts = [p.strip().strip('"').strip("'") for p in line.split(delim)]

            if len(parts) >= 2:
                try:
                    raw_val = float(parts[0])
                    freq_hz = int(round(raw_val * 1e6)) if raw_val < 1000.0 else int(round(raw_val))
                    if 24000000 <= freq_hz <= 1700000000:
                        name = parts[1]
                        channels.append(Channel(freq_hz=freq_hz, name=name))
                except ValueError:
                    continue

    return channels

def main():
    parser = argparse.ArgumentParser(description="AirScan Fast: High-Speed Multi-Channel VHF Aviation Scanner")
    parser.add_argument("-c", "--config", default="frequencies.csv", help="Path to frequency CSV file")
    parser.add_argument("-g", "--gain", type=float, default=36.0, help="Hardware RF gain in dB")
    parser.add_argument("-s", "--squelch", type=float, default=-45.0, help="Manual squelch threshold in dBFS")
    parser.add_argument("-p", "--ppm", type=int, default=0, help="Crystal frequency correction in PPM")
    parser.add_argument("-d", "--device", type=int, default=0, help="RTL-SDR USB device index")
    parser.add_argument("--no-audio", action="store_true", help="Disable live speaker audio output")
    parser.add_argument("--raw", action="store_true", help="Start with raw audio (disable BPF voice filter on launch)")
    args = parser.parse_args()

    channels = load_frequencies(args.config)
    if not channels:
        print(f"[-] Error: No valid frequencies found in {args.config}")
        sys.exit(1)

    try:
        sys.stdout.write("\x1b[8;34;102t")
        sys.stdout.flush()
        time.sleep(0.05)
    except Exception:
        pass

    scanner = None
    try:
        scanner = FastAirScanEngine(
            channels=channels, gain=args.gain, squelch_db=args.squelch,
            ppm=args.ppm, dev_idx=args.device, no_audio=args.no_audio,
            raw_filter=args.raw
        )
        curses.wrapper(curses_main, scanner)
    except KeyboardInterrupt:
        pass
    except Exception as err:
        print(f"\n[-] Runtime exception: {err}")
    finally:
        if scanner:
            scanner.close()
        print("\n[✓] AirScan Fast terminated cleanly. Audio streams and SDR hardware released.")

if __name__ == "__main__":
    main()
