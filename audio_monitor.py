"""
Audio Volume Auto-Leveler — Analysis + Player + Auto Volume Control
Pure Python — embedded VLC player with automatic volume adjustment.

1. Browse/select a movie file
2. Extract audio via ffmpeg (audio-only, no video decode)
3. Compute per-second loudness (RMS → dB)
4. Plot the full movie loudness + planned volume adjustments
5. Play movie in embedded VLC player
6. Automatically adjust VLC volume in real-time based on the plan
"""

import os
import sys
import subprocess
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import numpy as np

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

import imageio_ffmpeg

# Ensure python-vlc can find libvlc.dll
_vlc_dir = None
for _candidate in [
    os.path.join(os.environ.get("ProgramFiles", ""), "VideoLAN", "VLC"),
    os.path.join(os.environ.get("ProgramFiles(x86)", ""), "VideoLAN", "VLC"),
    os.environ.get("VLC_PATH", ""),
]:
    if _candidate and os.path.isfile(os.path.join(_candidate, "libvlc.dll")):
        _vlc_dir = _candidate
        break

if _vlc_dir:
    os.environ["PATH"] = _vlc_dir + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(_vlc_dir)

import vlc

FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
FFPROBE_BIN = os.path.join(os.path.dirname(FFMPEG_BIN),
                            FFMPEG_BIN.replace("ffmpeg", "ffprobe"))

# Analysis settings
ANALYSIS_SR = 4000
CHUNK_SEC = 1.0
CHUNK_SAMPLES = int(ANALYSIS_SR * CHUNK_SEC)

WIN_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# ------------------------------------------------------------------
# Utility functions
# ------------------------------------------------------------------
def get_duration(file_path):
    ffprobe = FFPROBE_BIN
    if not os.path.isfile(ffprobe):
        d = os.path.dirname(FFMPEG_BIN)
        for name in os.listdir(d):
            if "ffprobe" in name.lower():
                ffprobe = os.path.join(d, name)
                break

    if os.path.isfile(ffprobe):
        try:
            r = subprocess.run(
                [ffprobe, "-v", "error",
                 "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1",
                 file_path],
                capture_output=True, text=True, creationflags=WIN_FLAGS, timeout=10)
            val = r.stdout.strip()
            if val and val != "N/A":
                return float(val)
        except Exception:
            pass

    try:
        r = subprocess.run(
            [FFMPEG_BIN, "-i", file_path, "-hide_banner"],
            capture_output=True, text=True, creationflags=WIN_FLAGS, timeout=10)
        for line in r.stderr.split("\n"):
            if "Duration:" in line:
                t = line.split("Duration:")[1].split(",")[0].strip()
                parts = t.split(":")
                return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    except Exception:
        pass
    return 0.0


def extract_audio(file_path, progress_cb=None):
    fd, tmp = tempfile.mkstemp(suffix=".raw")
    os.close(fd)

    try:
        duration = get_duration(file_path)
        expected_bytes = max(int(duration * ANALYSIS_SR * 2), 1)

        if progress_cb:
            progress_cb(0.02, f"Extracting audio ({duration / 60:.1f} min)...")

        cmd = [
            FFMPEG_BIN, "-nostdin",
            "-i", file_path,
            "-vn", "-sn", "-dn",
            "-map", "0:a:0",
            "-ac", "1",
            "-ar", str(ANALYSIS_SR),
            "-f", "s16le",
            "-y", "-loglevel", "error",
            tmp
        ]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 creationflags=WIN_FLAGS)

        import time as _time
        while proc.poll() is None:
            try:
                sz = os.path.getsize(tmp)
                frac = min(sz / expected_bytes, 0.95)
                if progress_cb:
                    progress_cb(frac, f"Extracting audio... {frac * 100:.0f}%")
            except OSError:
                pass
            _time.sleep(0.2)

        if proc.returncode != 0:
            err = proc.stderr.read().decode(errors="replace")[:300]
            raise RuntimeError(f"ffmpeg error: {err}")

        audio = np.fromfile(tmp, dtype=np.int16).astype(np.float32) / 32768.0
        return audio, duration

    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def compute_loudness(audio):
    n = len(audio) // CHUNK_SAMPLES
    if n == 0:
        return np.array([]), np.array([])

    trimmed = audio[:n * CHUNK_SAMPLES].reshape(n, CHUNK_SAMPLES)
    rms = np.sqrt(np.mean(trimmed ** 2, axis=1))

    with np.errstate(divide="ignore"):
        db = np.where(rms < 1e-10, -100.0, 20.0 * np.log10(rms))

    return np.arange(n) * CHUNK_SEC, db


def plan_volume(db, target_db=-25.0, attack_ms=200, release_ms=1500,
                max_adj=25.0, lookahead_sec=3.0):
    n = len(db)
    la = int(lookahead_sec / CHUNK_SEC)
    att = 1.0 - np.exp(-1.0 / max(attack_ms / (CHUNK_SEC * 1000), 0.01))
    rel = 1.0 - np.exp(-1.0 / max(release_ms / (CHUNK_SEC * 1000), 0.01))

    eff = db.copy().astype(np.float64)
    for i in range(n):
        end = min(i + la, n)
        mx = np.max(db[i:end])
        if mx > eff[i]:
            eff[i] = mx

    desired = np.where(eff < -90, 0.0, target_db - eff)
    desired = np.clip(desired, -max_adj, max_adj)

    out = np.zeros(n)
    cur = 0.0
    for i in range(n):
        c = att if desired[i] < cur else rel
        cur += c * (desired[i] - cur)
        out[i] = cur

    vol_pct = np.clip(100.0 * (10.0 ** (out / 20.0)), 0, 100)
    return out, vol_pct


# ------------------------------------------------------------------
# GUI Application
# ------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Audio Auto-Leveler — Player + Volume Control")
        self.root.geometry("1400x900")
        self.root.configure(bg="#1a1a2e")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.times = None
        self.db = None
        self.gain = None
        self.vol = None
        self.duration = 0.0
        self.file_path = None

        # VLC player
        self.vlc_instance = vlc.Instance("--no-xlib")
        self.player = self.vlc_instance.media_player_new()
        self.is_playing = False
        self.auto_volume = False
        self.seeking = False

        self._build_ui()

    def _build_ui(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure("D.TFrame", background="#1a1a2e")
        s.configure("D.TLabel", background="#1a1a2e", foreground="#ccc",
                     font=("Segoe UI", 10))
        s.configure("H.TLabel", background="#1a1a2e", foreground="#00d2ff",
                     font=("Segoe UI", 13, "bold"))
        s.configure("S.TLabel", background="#1a1a2e", foreground="#fff",
                     font=("Segoe UI", 14, "bold"))
        s.configure("D.TLabelframe", background="#1a1a2e")
        s.configure("D.TLabelframe.Label", background="#1a1a2e",
                     foreground="#00d2ff", font=("Segoe UI", 10, "bold"))

        # ===== Main horizontal split: LEFT (player) | RIGHT (graphs) =====
        main = ttk.Frame(self.root, style="D.TFrame")
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=5)

        # ---------- LEFT COLUMN: Player ----------
        left = ttk.Frame(main, style="D.TFrame", width=540)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 5))
        left.pack_propagate(False)

        ttk.Label(left, text="MOVIE PLAYER", style="H.TLabel").pack(pady=(5, 3))

        # Video embed
        self.video_frame = tk.Frame(left, bg="black", width=520, height=320)
        self.video_frame.pack(padx=5, pady=3)
        self.video_frame.pack_propagate(False)

        # Buttons row
        btn_row = ttk.Frame(left, style="D.TFrame")
        btn_row.pack(fill=tk.X, padx=5, pady=3)

        self.btn_browse = tk.Button(btn_row, text="Browse", bg="#00c853",
                                     fg="white", font=("Segoe UI", 9, "bold"),
                                     padx=8, command=self._browse)
        self.btn_browse.pack(side=tk.LEFT, padx=2)

        self.btn_play = tk.Button(btn_row, text="\u25b6 Play", bg="#2196f3",
                                   fg="white", font=("Segoe UI", 9, "bold"),
                                   padx=8, command=self._play, state=tk.DISABLED)
        self.btn_play.pack(side=tk.LEFT, padx=2)

        self.btn_pause = tk.Button(btn_row, text="\u23f8 Pause", bg="#ff9800",
                                    fg="white", font=("Segoe UI", 9, "bold"),
                                    padx=8, command=self._pause, state=tk.DISABLED)
        self.btn_pause.pack(side=tk.LEFT, padx=2)

        self.btn_stop = tk.Button(btn_row, text="\u23f9 Stop", bg="#f44336",
                                   fg="white", font=("Segoe UI", 9, "bold"),
                                   padx=8, command=self._stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=2)

        # Auto-volume toggle
        self.var_autovol = tk.BooleanVar(value=False)
        self.chk_autovol = tk.Checkbutton(
            btn_row, text="Auto-Volume", variable=self.var_autovol,
            bg="#1a1a2e", fg="#66bb6a", selectcolor="#333",
            activebackground="#1a1a2e", activeforeground="#66bb6a",
            font=("Segoe UI", 10, "bold"), command=self._toggle_autovol,
            state=tk.DISABLED)
        self.chk_autovol.pack(side=tk.LEFT, padx=8)

        # Seek bar
        self.seek_var = tk.DoubleVar(value=0)
        self.seek_scale = tk.Scale(left, from_=0, to=100, resolution=0.1,
                                    orient=tk.HORIZONTAL, variable=self.seek_var,
                                    bg="#16213e", fg="#ccc", troughcolor="#333",
                                    highlightthickness=0, showvalue=False)
        self.seek_scale.pack(fill=tk.X, padx=5, pady=2)
        self.seek_scale.bind("<ButtonPress-1>", lambda e: setattr(self, 'seeking', True))
        self.seek_scale.bind("<ButtonRelease-1>", self._seek_release)

        # Position / volume labels
        info = ttk.Frame(left, style="D.TFrame")
        info.pack(fill=tk.X, padx=5, pady=2)

        self.lbl_pos = ttk.Label(info, text="00:00:00 / 00:00:00", style="D.TLabel")
        self.lbl_pos.pack(side=tk.LEFT)
        self.lbl_curvol = ttk.Label(info, text="Vol: 100%", style="S.TLabel")
        self.lbl_curvol.pack(side=tk.RIGHT)
        self.lbl_curadj = ttk.Label(info, text="Adj: off", style="D.TLabel")
        self.lbl_curadj.pack(side=tk.RIGHT, padx=10)

        # Progress / status
        self.pvar = tk.DoubleVar()
        ttk.Progressbar(left, variable=self.pvar, maximum=100).pack(
            fill=tk.X, padx=5, pady=2)
        self.lbl_status = ttk.Label(left, text="Browse a movie file to start",
                                     style="D.TLabel")
        self.lbl_status.pack(padx=5)

        self.lbl_file = ttk.Label(left, text="", style="D.TLabel", wraplength=500)
        self.lbl_file.pack(padx=5, pady=2)

        self.lbl_stats = ttk.Label(left, text="", style="D.TLabel", wraplength=500)
        self.lbl_stats.pack(padx=5, pady=2)

        # ---------- RIGHT COLUMN: Graphs + Controls ----------
        right = ttk.Frame(main, style="D.TFrame")
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        gf = ttk.Frame(right, style="D.TFrame")
        gf.pack(fill=tk.BOTH, expand=True)

        self.fig = Figure(figsize=(8, 5), dpi=100, facecolor="#1a1a2e")

        # Graph 1: Movie loudness
        self.ax1 = self.fig.add_subplot(3, 1, 1)
        self.ax1.set_facecolor("#16213e")
        self.ax1.set_title("Movie Audio Level (dBFS)", color="#00d2ff", fontsize=10)
        self.ax1.set_ylabel("dBFS", color="#888")
        self.ax1.set_ylim(-80, 0)
        self.ax1.tick_params(colors="#888")
        self.ln1, = self.ax1.plot([], [], color="#00d2ff", lw=0.6, alpha=0.85)
        self.target_line = self.ax1.axhline(y=-25, color="#ff9800", ls="--", lw=1,
                                             label="Target")
        self.cursor1 = self.ax1.axvline(x=0, color="#ff5722", lw=1.5, alpha=0.9,
                                          label="Now")
        self.ax1.legend(loc="upper right", fontsize=7, facecolor="#16213e",
                         edgecolor="#444", labelcolor="#ccc")

        # Graph 2: Gain adjustment
        self.ax2 = self.fig.add_subplot(3, 1, 2)
        self.ax2.set_facecolor("#16213e")
        self.ax2.set_title("Volume Adjustment (dB)", color="#66bb6a", fontsize=10)
        self.ax2.set_ylabel("dB", color="#888")
        self.ax2.tick_params(colors="#888")
        self.ax2.axhline(y=0, color="#444", lw=0.5)
        self.ln2, = self.ax2.plot([], [], color="#66bb6a", lw=0.7)
        self.cursor2 = self.ax2.axvline(x=0, color="#ff5722", lw=1.5, alpha=0.9)

        # Graph 3: Volume %
        self.ax3 = self.fig.add_subplot(3, 1, 3)
        self.ax3.set_facecolor("#16213e")
        self.ax3.set_title("Applied Volume %", color="#ffeb3b", fontsize=10)
        self.ax3.set_ylabel("Vol %", color="#888")
        self.ax3.set_xlabel("Time (minutes)", color="#888")
        self.ax3.set_ylim(0, 100)
        self.ax3.tick_params(colors="#888")
        self.ln3, = self.ax3.plot([], [], color="#ffeb3b", lw=0.7)
        self.cursor3 = self.ax3.axvline(x=0, color="#ff5722", lw=1.5, alpha=0.9)

        self.fig.tight_layout(pad=1.5)
        self.canvas = FigureCanvasTkAgg(self.fig, master=gf)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # Compressor sliders
        cf = ttk.LabelFrame(right, text="Compressor Parameters",
                             style="D.TLabelframe")
        cf.pack(fill=tk.X, pady=(0, 5))
        ci = ttk.Frame(cf, style="D.TFrame")
        ci.pack(fill=tk.X, padx=6, pady=4)

        self.pvars = {}
        sliders = [
            ("Target dB", -50, -5, 1, -25, 0),
            ("Attack ms", 10, 500, 10, 200, 1),
            ("Release ms", 100, 5000, 100, 1500, 2),
            ("Max Adj dB", 5, 50, 1, 25, 3),
            ("Lookahead s", 0, 10, 1, 3, 4),
        ]
        for label, lo, hi, res, default, col in sliders:
            ttk.Label(ci, text=label, style="D.TLabel").grid(
                row=0, column=col * 2, padx=3, sticky=tk.W)
            v = tk.DoubleVar(value=default)
            self.pvars[label] = v
            tk.Scale(ci, from_=lo, to=hi, resolution=res, orient=tk.HORIZONTAL,
                     variable=v, length=120, bg="#16213e", fg="#ccc",
                     troughcolor="#333", highlightthickness=0,
                     command=lambda _: self._replan()).grid(
                row=0, column=col * 2 + 1, padx=3)

    # ----------------------------------------------------------------
    # File selection & analysis
    # ----------------------------------------------------------------
    def _browse(self):
        path = filedialog.askopenfilename(
            title="Select Movie File",
            filetypes=[
                ("Video", "*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v *.ts"),
                ("All", "*.*")])
        if path:
            self.file_path = path
            self.lbl_file.config(text=os.path.basename(path))
            self._analyze(path)

    def _analyze(self, path):
        self.pvar.set(0)
        self.lbl_status.config(text="Starting analysis...")

        def run():
            try:
                audio, dur = extract_audio(path, progress_cb=self._progress)
                self.root.after(0, lambda: self._progress(0.97, "Computing loudness..."))
                times, db = compute_loudness(audio)
                del audio
                self.times = times
                self.db = db
                self.duration = dur
                self.root.after(0, self._on_done)
            except Exception as e:
                self.root.after(0, lambda: self._on_error(str(e)))

        threading.Thread(target=run, daemon=True).start()

    def _progress(self, frac, msg=""):
        self.root.after(0, lambda: self.pvar.set(frac * 100))
        if msg:
            self.root.after(0, lambda: self.lbl_status.config(text=msg))

    def _on_done(self):
        self.pvar.set(100)
        n = len(self.db)
        self.lbl_status.config(
            text=f"Done! {n} pts, {self.duration / 60:.1f} min. Ready to play.")

        mask = self.db > -80
        avg = np.mean(self.db[mask]) if np.any(mask) else -80
        peak = np.max(self.db)
        low = np.min(self.db[mask]) if np.any(mask) else -80
        self.lbl_stats.config(
            text=f"Avg: {avg:.1f}dB | Peak: {peak:.1f}dB | Range: {peak - low:.1f}dB")

        t_min = self.times / 60.0
        self.ln1.set_data(t_min, self.db)
        self.ax1.set_xlim(0, t_min[-1] if len(t_min) > 0 else 1)

        self._replan()
        self.canvas.draw()

        # Enable player
        self.btn_play.config(state=tk.NORMAL)
        self.btn_pause.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.NORMAL)
        self.chk_autovol.config(state=tk.NORMAL)
        self.seek_scale.config(to=self.duration)

    def _on_error(self, msg):
        self.lbl_status.config(text=f"Error: {msg}")
        messagebox.showerror("Error", msg)

    # ----------------------------------------------------------------
    # Compressor re-plan
    # ----------------------------------------------------------------
    def _replan(self):
        if self.db is None:
            return
        gain, vol = plan_volume(
            self.db,
            target_db=self.pvars["Target dB"].get(),
            attack_ms=self.pvars["Attack ms"].get(),
            release_ms=self.pvars["Release ms"].get(),
            max_adj=self.pvars["Max Adj dB"].get(),
            lookahead_sec=self.pvars["Lookahead s"].get(),
        )
        self.gain = gain
        self.vol = vol

        t_min = self.times / 60.0
        self.ln2.set_data(t_min, gain)
        g_lim = max(abs(np.min(gain)), abs(np.max(gain)), 5) * 1.1
        self.ax2.set_ylim(-g_lim, g_lim)
        self.ax2.set_xlim(0, t_min[-1] if len(t_min) > 0 else 1)

        self.ln3.set_data(t_min, vol)
        self.ax3.set_xlim(0, t_min[-1] if len(t_min) > 0 else 1)

        self.target_line.set_ydata([self.pvars["Target dB"].get()] * 2)
        self.canvas.draw_idle()

    # ----------------------------------------------------------------
    # Player controls
    # ----------------------------------------------------------------
    def _play(self):
        if not self.file_path:
            return
        state = self.player.get_state()
        if state == vlc.State.Paused:
            self.player.pause()  # unpause
        else:
            media = self.vlc_instance.media_new(self.file_path)
            self.player.set_media(media)
            if sys.platform == "win32":
                self.player.set_hwnd(self.video_frame.winfo_id())
            else:
                self.player.set_xwindow(self.video_frame.winfo_id())
            self.player.play()

        self.is_playing = True
        self._playback_loop()

    def _pause(self):
        if self.player.is_playing():
            self.player.pause()

    def _stop(self):
        self.player.stop()
        self.is_playing = False
        self.auto_volume = False
        self.var_autovol.set(False)

    def _toggle_autovol(self):
        self.auto_volume = self.var_autovol.get()
        if not self.auto_volume:
            self.player.audio_set_volume(100)

    def _seek_release(self, event):
        self.seeking = False
        if self.player.get_media():
            pos = self.seek_var.get() / max(self.duration, 1)
            self.player.set_position(max(0.0, min(pos, 1.0)))

    # ----------------------------------------------------------------
    # Playback loop — cursor + volume control
    # ----------------------------------------------------------------
    def _playback_loop(self):
        if not self.is_playing:
            return

        state = self.player.get_state()
        if state in (vlc.State.Ended, vlc.State.Stopped, vlc.State.Error):
            self.is_playing = False
            return

        length_ms = self.player.get_length()
        current_ms = self.player.get_time()

        if length_ms > 0 and current_ms >= 0:
            pos_sec = current_ms / 1000.0
            total_sec = length_ms / 1000.0

            # Update seek bar
            if not self.seeking:
                self.seek_var.set(pos_sec)

            # Time display
            self.lbl_pos.config(text=f"{self._fmt(pos_sec)} / {self._fmt(total_sec)}")

            # Move graph cursors
            pos_min = pos_sec / 60.0
            self.cursor1.set_xdata([pos_min, pos_min])
            self.cursor2.set_xdata([pos_min, pos_min])
            self.cursor3.set_xdata([pos_min, pos_min])
            self.canvas.draw_idle()

            # Apply volume
            if self.auto_volume and self.vol is not None:
                ci = int(pos_sec / CHUNK_SEC)
                ci = max(0, min(ci, len(self.vol) - 1))
                target_vol = int(round(self.vol[ci]))
                target_vol = max(0, min(target_vol, 200))  # VLC supports 0-200
                self.player.audio_set_volume(target_vol)

                adj = self.gain[ci]
                self.lbl_curvol.config(text=f"Vol: {target_vol}%")
                self.lbl_curadj.config(text=f"Adj: {adj:+.1f} dB")
                if adj < -3:
                    self.lbl_curadj.config(foreground="#f44336")
                elif adj > 3:
                    self.lbl_curadj.config(foreground="#66bb6a")
                else:
                    self.lbl_curadj.config(foreground="#ccc")
            else:
                self.lbl_curvol.config(text="Vol: Manual")
                self.lbl_curadj.config(text="Adj: off", foreground="#ccc")

        self.root.after(200, self._playback_loop)

    @staticmethod
    def _fmt(s):
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        sec = int(s % 60)
        return f"{h:02d}:{m:02d}:{sec:02d}"

    def on_close(self):
        self.player.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
