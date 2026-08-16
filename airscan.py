#!/usr/bin/env python3
import curses
import os
import sys
import time
import csv
import threading
import subprocess
import ctypes
import ctypes.util
import numpy as np
import sounddevice as sd
from datetime import datetime

# -------------------------------------------------------------
# BASE DIRECTORY & FILE PATHS (Resolves Symlinks)
# -------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.realpath(__file__))
CSV_FILE = os.path.join(BASE_DIR, "frequencies.csv")
RECORDINGS_DIR = os.path.join(BASE_DIR, "recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)

# -------------------------------------------------------------
# DIRECT LIBRTLSDR CTYPES BINDING WITH EXPLICIT SIGNATURES
# -------------------------------------------------------------
lib_path = ctypes.util.find_library('rtlsdr')
if not lib_path:
    for candidate in [
        '/lib/x86_64-linux-gnu/librtlsdr.so.0',
        '/lib/x86_64-linux-gnu/librtlsdr.so',
        '/usr/lib/x86_64-linux-gnu/librtlsdr.so',
        'librtlsdr.so.0',
        'librtlsdr.so'
    ]:
        if os.path.exists(candidate):
            lib_path = candidate
            break

try:
    librtlsdr = ctypes.CDLL(lib_path)
except Exception as e:
    sys.exit(f"Error loading librtlsdr: {e}")

librtlsdr.rtlsdr_open.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32]
librtlsdr.rtlsdr_open.restype = ctypes.c_int

librtlsdr.rtlsdr_close.argtypes = [ctypes.c_void_p]
librtlsdr.rtlsdr_close.restype = ctypes.c_int

librtlsdr.rtlsdr_set_center_freq.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
librtlsdr.rtlsdr_set_center_freq.restype = ctypes.c_int

librtlsdr.rtlsdr_set_sample_rate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
librtlsdr.rtlsdr_set_sample_rate.restype = ctypes.c_int

librtlsdr.rtlsdr_set_tuner_gain_mode.argtypes = [ctypes.c_void_p, ctypes.c_int]
librtlsdr.rtlsdr_set_tuner_gain_mode.restype = ctypes.c_int

librtlsdr.rtlsdr_set_tuner_gain.argtypes = [ctypes.c_void_p, ctypes.c_int]
librtlsdr.rtlsdr_set_tuner_gain.restype = ctypes.c_int

librtlsdr.rtlsdr_get_tuner_gains.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
librtlsdr.rtlsdr_get_tuner_gains.restype = ctypes.c_int

librtlsdr.rtlsdr_reset_buffer.argtypes = [ctypes.c_void_p]
librtlsdr.rtlsdr_reset_buffer.restype = ctypes.c_int

librtlsdr.rtlsdr_read_sync.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_ubyte),
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_int)
]
librtlsdr.rtlsdr_read_sync.restype = ctypes.c_int


class NativeRtlSdr:
    def __init__(self, device_index=0):
        self.dev = ctypes.c_void_p()
        if librtlsdr.rtlsdr_open(ctypes.byref(self.dev), device_index) < 0:
            raise RuntimeError("Failed to open RTL-SDR. Unplug and replug the dongle.")
        
        num_gains = librtlsdr.rtlsdr_get_tuner_gains(self.dev, None)
        if num_gains > 0:
            gains_array = (ctypes.c_int * num_gains)()
            librtlsdr.rtlsdr_get_tuner_gains(self.dev, gains_array)
            self.valid_gains_db = [g / 10.0 for g in gains_array]
        else:
            self.valid_gains_db = [0.0, 9.0, 14.0, 20.7, 28.0, 38.0, 42.1, 49.6]

        librtlsdr.rtlsdr_set_tuner_gain_mode(self.dev, 1)
        self.gain = 38.0
        self.set_gain(self.gain)

    def set_sample_rate(self, rate):
        if self.dev:
            librtlsdr.rtlsdr_set_sample_rate(self.dev, int(rate))

    def set_center_freq(self, freq_hz):
        if self.dev:
            librtlsdr.rtlsdr_set_center_freq(self.dev, int(freq_hz))

    def set_gain(self, gain_db):
        self.gain = float(gain_db)
        if self.dev:
            librtlsdr.rtlsdr_set_tuner_gain(self.dev, int(gain_db * 10))

    def reset_buffer(self):
        if self.dev:
            librtlsdr.rtlsdr_reset_buffer(self.dev)

    def read_samples(self, num_samples=16384):
        if not self.dev:
            return np.zeros(num_samples, dtype=np.complex64)
        num_bytes = num_samples * 2
        buf = (ctypes.c_ubyte * num_bytes)()
        n_read = ctypes.c_int()
        
        result = librtlsdr.rtlsdr_read_sync(self.dev, buf, num_bytes, ctypes.byref(n_read))
        if result < 0 or n_read.value < num_bytes:
            return np.zeros(num_samples, dtype=np.complex64)
            
        raw = np.ctypeslib.as_array(buf)
        iq = (raw.astype(np.float32) - 127.5) / 127.5
        return iq[0::2] + 1j * iq[1::2]

    def close(self):
        if self.dev:
            d = self.dev
            self.dev = None
            librtlsdr.rtlsdr_close(d)


# -------------------------------------------------------------
# FREQUENCY CSV MANAGEMENT
# -------------------------------------------------------------
DEFAULT_CHANNELS = [
    (123.025, "Helo Air-to-Air"),
    (123.050, "Heliport UNICOM"),
    (122.900, "MULTICOM"),
    (119.975, "Tower / CTAF"),
    (121.650, "Ground Control"),
    (119.100, "Approach / Radar"),
    (122.700, "Airport CTAF"),
    (133.550, "Enroute Center"),
    (122.800, "UNICOM / Advisory")
]

def load_channels():
    channels = []
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            for freq, name in DEFAULT_CHANNELS:
                writer.writerow([f"{freq:.3f}", name])

    with open(CSV_FILE, mode='r', encoding='utf-8') as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or row[0].startswith('#'):
                continue
            try:
                freq = float(row[0].strip())
                name = row[1].strip() if len(row) > 1 else f"Ch {len(channels)+1}"
                channels.append({"freq": freq, "name": name, "hits": 0, "last": "Never"})
            except ValueError:
                continue

    return channels if channels else [{"freq": 122.800, "name": "UNICOM", "hits": 0, "last": "Never"}]

def append_channel_to_csv(freq, name):
    with open(CSV_FILE, mode='a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([f"{freq:.3f}", name])


SAMPLE_RATE = 1024000
AUDIO_RATE = 32000
DECIMATION = int(SAMPLE_RATE / AUDIO_RATE)  # 32

class AirbandScanner:
    def __init__(self):
        self.channels = load_channels()
        self.sdr = NativeRtlSdr()
        self.sdr.set_sample_rate(SAMPLE_RATE)
        
        self.squelch = -28.0
        self.volume = 1.0
        self.running = True
        self.held = False
        
        self.agc_peak = 0.05
        
        # Recording state & timing
        self.recording = False
        self.vox_active = False
        self.ffmpeg_proc = None
        self.rec_filename = ""
        self.rec_start_time = 0.0
        self.total_vox_samples = 0
        self.rec_lock = threading.Lock()
        self.channel_lock = threading.Lock()
        
        self.current_idx = 0
        self.selected_row = 0
        self.current_rssi = -100.0
        self.status = "SCANNING"
        
        self.stream = sd.OutputStream(
            samplerate=AUDIO_RATE,
            channels=1,
            dtype='float32',
            blocksize=1024
        )
        self.stream.start()

    def set_frequency(self, freq_mhz):
        hz = int(float(freq_mhz) * 1e6)
        if 24000000 <= hz <= 1766000000:
            self.sdr.set_center_freq(hz)

    def calculate_rssi(self, samples):
        p = np.mean(np.abs(samples)**2)
        return float(10 * np.log10(p + 1e-12))

    def demodulate_am(self, samples):
        mag = np.abs(samples)
        n_blocks = len(mag) // DECIMATION
        audio = mag[:n_blocks * DECIMATION].reshape(n_blocks, DECIMATION).mean(axis=1)
        audio = audio - np.mean(audio)
        
        chunk_peak = float(np.max(np.abs(audio))) if len(audio) > 0 else 0.01
        self.agc_peak = max(chunk_peak, (self.agc_peak * 0.96) + (chunk_peak * 0.04))
        
        target_gain = 0.85 / max(self.agc_peak, 0.01)
        target_gain = min(target_gain, 12.0)
        audio = audio * target_gain
        return np.clip(audio, -1.0, 1.0).astype(np.float32)

    def add_channel(self, freq, name):
        with self.channel_lock:
            self.channels.append({"freq": freq, "name": name, "hits": 0, "last": "Never"})
            append_channel_to_csv(freq, name)

    def clear_hits(self):
        with self.channel_lock:
            for ch in self.channels:
                ch["hits"] = 0
                ch["last"] = "Never"

    def toggle_record(self):
        with self.rec_lock:
            self.recording = not self.recording
            if self.recording:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                self.rec_filename = os.path.join(RECORDINGS_DIR, f"Airband_VOX_{ts}.mp3")
                self.total_vox_samples = 0
                self.rec_start_time = time.time()
                
                cmd = [
                    'ffmpeg', '-y',
                    '-f', 's16le',
                    '-ar', str(AUDIO_RATE),
                    '-ac', '1',
                    '-i', '-',
                    '-codec:a', 'libmp3lame',
                    '-b:a', '64k',
                    self.rec_filename
                ]
                self.ffmpeg_proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            else:
                if self.ffmpeg_proc:
                    try:
                        self.ffmpeg_proc.stdin.close()
                        self.ffmpeg_proc.wait(timeout=1.5)
                    except Exception:
                        self.ffmpeg_proc.kill()
                    self.ffmpeg_proc = None

    def get_duration_str(self):
        if not self.recording:
            return "00:00"
        vox_sec = int(self.total_vox_samples / AUDIO_RATE)
        v_mins = (vox_sec % 3600) // 60
        v_secs = vox_sec % 60
        return f"{v_mins:02d}:{v_secs:02d}"

    def worker_loop(self):
        while self.running:
            with self.channel_lock:
                if not self.channels:
                    time.sleep(0.05)
                    continue
                self.current_idx = self.current_idx % len(self.channels)
                ch = self.channels[self.current_idx]
                freq = ch["freq"]

            self.sdr.reset_buffer()
            self.set_frequency(freq)
            time.sleep(0.015)

            samples = self.sdr.read_samples(16384)
            self.current_rssi = self.calculate_rssi(samples)

            if self.current_rssi > self.squelch or self.held:
                self.status = "LOCKED" if not self.held else "HOLD"
                with self.channel_lock:
                    ch["hits"] += 1
                    ch["last"] = datetime.now().strftime("%H:%M:%S")

                hang_counter = 0
                while self.running and (self.held or hang_counter < 6):
                    samples = self.sdr.read_samples(16384)
                    self.current_rssi = self.calculate_rssi(samples)
                    
                    if self.current_rssi < self.squelch and not self.held:
                        hang_counter += 1
                        self.vox_active = False
                    else:
                        hang_counter = 0
                        self.vox_active = True

                    audio = self.demodulate_am(samples)
                    play_audio = np.clip(audio * self.volume, -1.0, 1.0)
                    try:
                        self.stream.write(play_audio)
                    except Exception:
                        pass

                    if self.recording and self.vox_active:
                        with self.rec_lock:
                            if self.ffmpeg_proc and self.ffmpeg_proc.stdin:
                                pcm16 = (audio * 32767).astype(np.int16).tobytes()
                                try:
                                    self.ffmpeg_proc.stdin.write(pcm16)
                                    self.ffmpeg_proc.stdin.flush()
                                    self.total_vox_samples += len(audio)
                                except (BrokenPipeError, OSError, ValueError):
                                    pass
            else:
                self.status = "SCANNING"
                self.vox_active = False
                if not self.held:
                    with self.channel_lock:
                        if self.channels:
                            self.current_idx = (self.current_idx + 1) % len(self.channels)

    def close(self):
        self.running = False
        with self.rec_lock:
            if self.ffmpeg_proc:
                try:
                    self.ffmpeg_proc.stdin.close()
                    self.ffmpeg_proc.wait(timeout=1.0)
                except Exception:
                    self.ffmpeg_proc.kill()
                self.ffmpeg_proc = None
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass
        self.sdr.close()


def draw_meter(rssi, squelch, width=18):
    norm_val = int(np.clip((rssi + 60) / 60 * width, 0, width))
    norm_sq = int(np.clip((squelch + 60) / 60 * width, 0, width))
    bar = list(" " * width)
    for i in range(norm_val):
        bar[i] = "■"
    if 0 <= norm_sq < width:
        bar[norm_sq] = "│"
    return "".join(bar)


def safe_addstr(stdscr, y, x, text, attr=0):
    h, w = stdscr.getmaxyx()
    if 0 <= y < h and 0 <= x < w:
        try:
            stdscr.addstr(y, x, text[:w - x - 1], attr)
        except curses.error:
            pass


def prompt_user_input(stdscr, prompt_text, y, x, max_len=25):
    curses.echo()
    curses.curs_set(1)
    stdscr.nodelay(False)
    safe_addstr(stdscr, y, x, prompt_text + " " * (max_len + 5), curses.color_pair(4) | curses.A_BOLD)
    safe_addstr(stdscr, y, x, prompt_text, curses.color_pair(4) | curses.A_BOLD)
    stdscr.refresh()
    
    val = stdscr.getstr(y, min(x + len(prompt_text), stdscr.getmaxyx()[1] - 2), max_len).decode('utf-8').strip()
    
    curses.noecho()
    curses.curs_set(0)
    stdscr.nodelay(False)
    stdscr.timeout(60)
    return val


def gui(stdscr, scanner):
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.nodelay(False)
    stdscr.timeout(60)

    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_RED, -1)
    curses.init_pair(4, curses.COLOR_YELLOW, -1)
    curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_CYAN)

    MIN_LINES = 14
    MIN_COLS = 60

    while scanner.running:
        max_y, max_x = stdscr.getmaxyx()
        with scanner.channel_lock:
            num_channels = len(scanner.channels)

        # Dynamic size check (prevents crashes on narrow/short terminals)
        if max_y < MIN_LINES or max_x < MIN_COLS:
            stdscr.erase()
            w_title = "! TERMINAL WINDOW TOO SMALL !"
            w_stats = f"Size: {max_x}x{max_y}  (Min: {MIN_COLS}x{MIN_LINES})"
            w_hint  = "Expand window or press 'Q' to exit"
            
            mid_y = max_y // 2
            if mid_y - 1 >= 0 and max_x > len(w_title):
                safe_addstr(stdscr, mid_y - 1, (max_x - len(w_title)) // 2, w_title, curses.A_BOLD)
            if mid_y >= 0 and max_x > len(w_stats):
                safe_addstr(stdscr, mid_y, (max_x - len(w_stats)) // 2, w_stats)
            if mid_y + 1 < max_y and max_x > len(w_hint):
                safe_addstr(stdscr, mid_y + 1, (max_x - len(w_hint)) // 2, w_hint, curses.A_DIM)
            
            stdscr.refresh()
            
            try:
                k = stdscr.getch()
                if k in [ord('q'), ord('Q')]:
                    scanner.running = False
                    break
            except Exception:
                pass
            continue

        stdscr.erase()
        h, w = max_y, max_x

        # Generic Header Box (Clean, non-regional title)
        safe_addstr(stdscr, 1, 2, "┌" + "─" * (w - 6) + "┐", curses.color_pair(1) | curses.A_BOLD)
        safe_addstr(stdscr, 2, 2, ("│  AIRSCAN - SDR AIRBAND MONITOR & SCANNER").ljust(w - 5) + "│", curses.color_pair(1) | curses.A_BOLD)
        safe_addstr(stdscr, 3, 2, "└" + "─" * (w - 6) + "┘", curses.color_pair(1) | curses.A_BOLD)

        # Status Line
        safe_addstr(stdscr, 4, 4, "STATUS: ")
        if scanner.status == "LOCKED":
            safe_addstr(stdscr, 4, 12, "[ LOCKED ]  ", curses.color_pair(4) | curses.A_BOLD)
        elif scanner.status == "HOLD":
            safe_addstr(stdscr, 4, 12, "[  HOLD  ]  ", curses.color_pair(4) | curses.A_BOLD)
        else:
            safe_addstr(stdscr, 4, 12, "[ SCANNING ]", curses.color_pair(2))

        # VOX Recording & Duration
        safe_addstr(stdscr, 4, 28, "MP3 VOX REC: ")
        if scanner.recording:
            if scanner.vox_active:
                safe_addstr(stdscr, 4, 41, "● CAPTURING", curses.color_pair(3) | curses.A_BOLD)
            else:
                safe_addstr(stdscr, 4, 41, "○ WAITING  ", curses.color_pair(3))
        else:
            safe_addstr(stdscr, 4, 41, "OFF/STANDBY", curses.A_DIM)

        dur_str = scanner.get_duration_str()
        safe_addstr(stdscr, 4, 55, f"AUDIO: [{dur_str}]", curses.A_BOLD if scanner.recording else curses.A_DIM)

        # Telemetry & S-Meter
        meter_str = draw_meter(scanner.current_rssi, scanner.squelch, width=18)
        safe_addstr(stdscr, 5, 4, f"SIGNAL: [{meter_str}] {scanner.current_rssi:5.1f} dBFS")
        safe_addstr(stdscr, 5, 48, f"SQUELCH: {scanner.squelch:5.1f} dBFS")
        safe_addstr(stdscr, 6, 4, f"GAIN: {scanner.sdr.gain:4.1f} dB    VOL: {int(scanner.volume * 100):3d}%")
        
        if scanner.recording and scanner.rec_filename:
            fname = os.path.basename(scanner.rec_filename)
            safe_addstr(stdscr, 6, 32, f"FILE: {fname}"[:w - 34], curses.A_DIM)
        else:
            safe_addstr(stdscr, 6, 32, " " * (w - 34))

        # Channel Table Header
        safe_addstr(stdscr, 8, 2, f"  {'CH':<4} {'FREQ (MHz)':<12} {'CHANNEL / AGENCY':<20} {'HITS':<8} {'LAST HEARD':<12}", curses.color_pair(1) | curses.A_BOLD)
        safe_addstr(stdscr, 9, 2, "  " + "─" * (w - 6), curses.color_pair(1) | curses.A_BOLD)

        with scanner.channel_lock:
            max_visible = max(1, h - 14)
            scroll_offset = max(0, min(scanner.selected_row - max_visible + 1, num_channels - max_visible))
            
            for idx in range(max_visible):
                row_y = 10 + idx
                i = scroll_offset + idx
                if i < num_channels:
                    ch = scanner.channels[i]
                    is_active = (i == scanner.current_idx)
                    is_selected = (i == scanner.selected_row)

                    prefix = "► " if is_active else "  "
                    line = f"{prefix}{i+1:<4} {ch['freq']:<12.3f} {ch['name']:<20} {ch['hits']:<8} {ch['last']:<12}"

                    if is_selected:
                        safe_addstr(stdscr, row_y, 2, line.ljust(w - 6), curses.color_pair(5))
                    elif is_active:
                        color = curses.color_pair(4) if scanner.status in ["LOCKED", "HOLD"] else curses.color_pair(2)
                        safe_addstr(stdscr, row_y, 2, line.ljust(w - 6), color | curses.A_BOLD)
                    else:
                        safe_addstr(stdscr, row_y, 2, line.ljust(w - 6))
                else:
                    safe_addstr(stdscr, row_y, 2, " " * (w - 6))

        # Bottom Footer Controls (Includes [C] Clear Hits)
        footer_y = max(11 + max_visible, h - 3)
        safe_addstr(stdscr, footer_y, 2, "─" * (w - 4), curses.color_pair(1))
        safe_addstr(stdscr, footer_y + 1, 2, " [A] Add  [C] Clr Hits  [R] Rec  [+/-] Squelch  [SPACE] Hold  [G] Gain  [Q] Quit ", curses.color_pair(1))

        try:
            key = stdscr.getch()
            if key == curses.KEY_RESIZE:
                curses.update_lines_cols()
                stdscr.clear()
                stdscr.refresh()
                continue
        except Exception:
            key = -1

        if key in [ord('q'), ord('Q')]:
            scanner.running = False
            break
        elif key in [ord('c'), ord('C')]:
            scanner.clear_hits()
        elif key in [ord('a'), ord('A')]:
            prompt_y = footer_y + 1
            f_str = prompt_user_input(stdscr, "Enter Frequency in MHz (e.g. 122.950): ", prompt_y, 2, max_len=10)
            if f_str:
                try:
                    f_val = float(f_str)
                    lbl_str = prompt_user_input(stdscr, "Enter Agency/Channel Name: ", prompt_y, 2, max_len=20)
                    if not lbl_str:
                        lbl_str = f"{f_val:.3f} MHz"
                    scanner.add_channel(f_val, lbl_str)
                except ValueError:
                    pass
        elif key in [ord('r'), ord('R')]:
            scanner.toggle_record()
        elif key in [ord('+'), ord('=')]:
            scanner.squelch = min(0.0, scanner.squelch + 1.0)
        elif key in [ord('-'), ord('_')]:
            scanner.squelch = max(-80.0, scanner.squelch - 1.0)
        elif key == ord(' '):
            scanner.held = not scanner.held
            if scanner.held:
                scanner.current_idx = scanner.selected_row
        elif key in [curses.KEY_UP, ord('k'), ord('K')]:
            if num_channels > 0:
                scanner.selected_row = (scanner.selected_row - 1) % num_channels
        elif key in [curses.KEY_DOWN, ord('j'), ord('J')]:
            if num_channels > 0:
                scanner.selected_row = (scanner.selected_row + 1) % num_channels
        elif key in [10, 13, curses.KEY_ENTER]:
            scanner.current_idx = scanner.selected_row
            scanner.held = True
        elif key in [ord('g'), ord('G')]:
            gains = scanner.sdr.valid_gains_db
            curr = scanner.sdr.gain
            next_gains = [g for g in gains if g > curr]
            scanner.sdr.set_gain(next_gains[0] if next_gains else gains[0])
        elif key == ord(']'):
            scanner.volume = min(3.0, scanner.volume + 0.1)
        elif key == ord('['):
            scanner.volume = max(0.0, scanner.volume - 0.1)

        stdscr.refresh()


def main():
    # Request standard 90x28 terminal geometry on startup
    sys.stdout.write("\x1b[8;28;90t")
    sys.stdout.flush()

    scanner = AirbandScanner()
    worker = threading.Thread(target=scanner.worker_loop, daemon=True)
    worker.start()

    try:
        curses.wrapper(gui, scanner)
    except KeyboardInterrupt:
        pass
    finally:
        scanner.close()
        worker.join(timeout=1.0)

if __name__ == "__main__":
    main()
