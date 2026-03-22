"""
Audio Auto-Leveler — Movie Player + Real-Time Volume Control
Embedded VLC player with pre-analyzed audio loudness normalization.

Keyboard shortcuts:
  Space        Play / Pause
  S            Stop
  Left/Right   Seek ±5 s
  Shift+L/R    Seek ±30 s
  Up / Down    Volume ±5 %
  M            Mute / Unmute
  F / F11      Toggle fullscreen
  Esc          Exit fullscreen
  [ / ]        Speed −0.25 / +0.25
  A            Toggle auto-volume
  G            Toggle graphs panel
  Ctrl+O       Browse file
"""

__version__ = "1.0.0"
APP_NAME = "Audio Auto-Leveler"

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

ANALYSIS_SR = 4000
CHUNK_SEC = 1.0
CHUNK_SAMPLES = int(ANALYSIS_SR * CHUNK_SEC)
WIN_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# ── Colour palette ─────────────────────────────────────────────
BG       = "#0f0f1a"
BG2      = "#16213e"
ACCENT   = "#00d2ff"
GREEN    = "#66bb6a"
YELLOW   = "#ffeb3b"
RED      = "#f44336"
ORANGE   = "#ff9800"
WHITE    = "#e0e0e0"
GREY     = "#888"
CTRL_BG  = "#111126"


# ==================================================================
# Audio analysis helpers (unchanged from Phase 3)
# ==================================================================
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
            FFMPEG_BIN, "-nostdin", "-i", file_path,
            "-vn", "-sn", "-dn", "-map", "0:a:0",
            "-ac", "1", "-ar", str(ANALYSIS_SR),
            "-f", "s16le", "-y", "-loglevel", "error", tmp
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 creationflags=WIN_FLAGS)
        import time as _time
        while proc.poll() is None:
            try:
                sz = os.path.getsize(tmp)
                frac = min(sz / expected_bytes, 0.95)
                if progress_cb:
                    progress_cb(frac, f"Extracting audio… {frac * 100:.0f}%")
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


# ==================================================================
# GUI Application
# ==================================================================
class App:
    SPEEDS = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]

    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME}  v{__version__}")
        self.root.geometry("1500x900")
        self.root.minsize(900, 500)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── state ──
        self.times = self.db = self.gain = self.vol = None
        self.duration = 0.0
        self.file_path = None
        self.is_playing = False
        self.auto_volume = False
        self.seeking = False
        self.is_fullscreen = False
        self.muted = False
        self.manual_vol = 100          # 0-200
        self.pre_mute_vol = 100
        self.speed_idx = 3             # 1.0x
        self.graphs_visible = True
        self._hide_cursor_id = None
        self._controls_visible = True

        # ── VLC ──
        self.vlc_instance = vlc.Instance("--no-xlib")
        self.player = self.vlc_instance.media_player_new()

        self._build_ui()
        self._bind_keys()

    # ==============================================================
    #  UI construction
    # ==============================================================
    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("D.TFrame",   background=BG)
        style.configure("D.TLabel",   background=BG, foreground=WHITE,
                         font=("Segoe UI", 10))
        style.configure("H.TLabel",   background=BG, foreground=ACCENT,
                         font=("Segoe UI", 12, "bold"))
        style.configure("Big.TLabel", background=BG, foreground=WHITE,
                         font=("Segoe UI", 13, "bold"))
        style.configure("Dim.TLabel", background=BG, foreground=GREY,
                         font=("Segoe UI", 9))
        style.configure("D.TLabelframe",       background=BG)
        style.configure("D.TLabelframe.Label",  background=BG,
                         foreground=ACCENT, font=("Segoe UI", 10, "bold"))

        # Custom trough style for progress bar
        style.configure("pointed.Horizontal.TProgressbar",
                         troughcolor=BG2, background=ACCENT)

        # ═══════ Top-level containers ═══════
        # Middle area: video (left) + graphs (right)
        self.mid = ttk.Frame(self.root, style="D.TFrame")
        self.mid.pack(fill=tk.BOTH, expand=True)

        # ----- Video panel -----
        self.video_panel = ttk.Frame(self.mid, style="D.TFrame")
        self.video_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.video_frame = tk.Frame(self.video_panel, bg="black")
        self.video_frame.pack(fill=tk.BOTH, expand=True)
        self.video_frame.bind("<Double-Button-1>", lambda e: self._toggle_fullscreen())
        self.video_frame.bind("<Button-1>", lambda e: self._toggle_play())
        self.video_frame.bind("<Button-3>", self._show_context_menu)

        # Splash label shown before any file is loaded
        self.splash = tk.Label(
            self.video_frame, text=f"{APP_NAME}  v{__version__}\n\nCtrl+O  or  drag a file",
            bg="black", fg="#444", font=("Segoe UI", 18), justify=tk.CENTER)
        self.splash.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

        # ----- Graphs panel (right side) -----
        self.graph_panel = ttk.Frame(self.mid, style="D.TFrame", width=560)
        self.graph_panel.pack(side=tk.RIGHT, fill=tk.BOTH)
        self.graph_panel.pack_propagate(False)

        self._build_graphs(self.graph_panel)

        # ═══════ Bottom control bar ═══════
        self.bottom = tk.Frame(self.root, bg=CTRL_BG, height=90)
        self.bottom.pack(fill=tk.X, side=tk.BOTTOM)
        self.bottom.pack_propagate(False)

        self._build_seek_bar(self.bottom)
        self._build_controls(self.bottom)

    # ---------- seek bar ----------
    def _build_seek_bar(self, parent):
        seek_frame = tk.Frame(parent, bg=CTRL_BG, height=22)
        seek_frame.pack(fill=tk.X, padx=10, pady=(6, 0))

        self.seek_canvas = tk.Canvas(seek_frame, bg=CTRL_BG, height=16,
                                      highlightthickness=0, cursor="hand2")
        self.seek_canvas.pack(fill=tk.X)
        self.seek_canvas.bind("<ButtonPress-1>", self._seek_press)
        self.seek_canvas.bind("<B1-Motion>", self._seek_drag)
        self.seek_canvas.bind("<ButtonRelease-1>", self._seek_release)
        self.seek_canvas.bind("<Motion>", self._seek_hover)
        self.seek_canvas.bind("<Leave>", self._seek_leave)

        # hover time tooltip
        self.seek_tooltip = tk.Label(self.root, bg="#222", fg=WHITE,
                                      font=("Segoe UI", 9), padx=4, pady=1)

    def _draw_seek_bar(self, fraction=0.0):
        c = self.seek_canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 2:
            return
        # track
        y = h // 2
        c.create_rectangle(0, y - 2, w, y + 2, fill="#333", outline="")
        # fill
        fx = int(w * fraction)
        c.create_rectangle(0, y - 2, fx, y + 2, fill=ACCENT, outline="")
        # knob
        c.create_oval(fx - 6, y - 6, fx + 6, y + 6, fill=ACCENT, outline="white",
                       width=1)
        # mini loudness preview (faint)
        if self.db is not None and len(self.db) > 0:
            n = len(self.db)
            step = max(1, n // w)
            for i in range(0, n, step):
                x = int(i / n * w)
                norm = max(0, min(1, (self.db[i] + 80) / 80))
                bar_h = int(norm * (h // 2 - 2))
                alpha_hex = format(int(norm * 60 + 20), "02x")
                c.create_line(x, y - 3 - bar_h, x, y - 3,
                              fill="#00d2ff", width=1)

    def _seek_press(self, event):
        self.seeking = True
        self._seek_to_event(event)

    def _seek_drag(self, event):
        self._seek_to_event(event)

    def _seek_release(self, event):
        self._seek_to_event(event)
        self.seeking = False

    def _seek_to_event(self, event):
        w = self.seek_canvas.winfo_width()
        if w < 1 or self.duration <= 0:
            return
        frac = max(0.0, min(1.0, event.x / w))
        if self.player.get_media():
            self.player.set_position(frac)

    def _seek_hover(self, event):
        w = self.seek_canvas.winfo_width()
        if w < 1 or self.duration <= 0:
            return
        frac = max(0.0, min(1.0, event.x / w))
        t = frac * self.duration
        self.seek_tooltip.config(text=self._fmt(t))
        # position tooltip above the seek bar
        sx = self.seek_canvas.winfo_rootx() + event.x
        sy = self.seek_canvas.winfo_rooty() - 22
        self.seek_tooltip.place(x=sx - self.root.winfo_rootx(),
                                 y=sy - self.root.winfo_rooty())

    def _seek_leave(self, event):
        self.seek_tooltip.place_forget()

    # ---------- control buttons ----------
    def _build_controls(self, parent):
        bar = tk.Frame(parent, bg=CTRL_BG)
        bar.pack(fill=tk.X, padx=10, pady=(4, 6))

        # — Left group: playback buttons —
        left_grp = tk.Frame(bar, bg=CTRL_BG)
        left_grp.pack(side=tk.LEFT)

        btn_cfg = dict(bg="#222", fg=WHITE, activebackground="#444",
                       activeforeground=WHITE, bd=0, padx=6, pady=2,
                       font=("Segoe UI", 11), relief=tk.FLAT, cursor="hand2")

        tk.Button(left_grp, text="\U0001f4c2", command=self._browse, **btn_cfg).pack(
            side=tk.LEFT, padx=2)

        self.btn_play = tk.Button(left_grp, text="\u25b6", command=self._toggle_play,
                                   **btn_cfg)
        self.btn_play.pack(side=tk.LEFT, padx=2)

        tk.Button(left_grp, text="\u23f9", command=self._stop, **btn_cfg).pack(
            side=tk.LEFT, padx=2)
        tk.Button(left_grp, text="\u23ea", command=lambda: self._seek_rel(-10),
                  **btn_cfg).pack(side=tk.LEFT, padx=2)
        tk.Button(left_grp, text="\u23e9", command=lambda: self._seek_rel(10),
                  **btn_cfg).pack(side=tk.LEFT, padx=2)

        # Time label
        self.lbl_time = tk.Label(left_grp, text="00:00:00 / 00:00:00",
                                  bg=CTRL_BG, fg=WHITE, font=("Consolas", 10))
        self.lbl_time.pack(side=tk.LEFT, padx=(12, 0))

        # — Right group: volume, speed, tracks, toggles —
        right_grp = tk.Frame(bar, bg=CTRL_BG)
        right_grp.pack(side=tk.RIGHT)

        # Graph toggle
        self.btn_graphs = tk.Button(
            right_grp, text="\U0001f4ca", command=self._toggle_graphs,
            bg="#222", fg=ACCENT, activebackground="#444", bd=0, padx=5,
            font=("Segoe UI", 11), relief=tk.FLAT, cursor="hand2")
        self.btn_graphs.pack(side=tk.RIGHT, padx=4)

        # Fullscreen
        tk.Button(right_grp, text="\u26f6", command=self._toggle_fullscreen,
                  bg="#222", fg=WHITE, activebackground="#444", bd=0, padx=5,
                  font=("Segoe UI", 11), relief=tk.FLAT, cursor="hand2").pack(
            side=tk.RIGHT, padx=2)

        # Auto-volume
        self.var_autovol = tk.BooleanVar(value=False)
        self.chk_autovol = tk.Checkbutton(
            right_grp, text="Auto-Vol", variable=self.var_autovol,
            command=self._toggle_autovol,
            bg=CTRL_BG, fg=GREEN, selectcolor="#333",
            activebackground=CTRL_BG, activeforeground=GREEN,
            font=("Segoe UI", 9, "bold"), cursor="hand2")
        self.chk_autovol.pack(side=tk.RIGHT, padx=6)

        # Subtitle track
        tk.Label(right_grp, text="Sub", bg=CTRL_BG, fg=GREY,
                 font=("Segoe UI", 8)).pack(side=tk.RIGHT)
        self.sub_var = tk.StringVar(value="Off")
        self.sub_menu = ttk.Combobox(right_grp, textvariable=self.sub_var,
                                      values=["Off"], width=8, state="readonly")
        self.sub_menu.pack(side=tk.RIGHT, padx=(0, 6))
        self.sub_menu.bind("<<ComboboxSelected>>", self._on_sub_change)

        # Audio track
        tk.Label(right_grp, text="Audio", bg=CTRL_BG, fg=GREY,
                 font=("Segoe UI", 8)).pack(side=tk.RIGHT)
        self.audio_var = tk.StringVar(value="Default")
        self.audio_menu = ttk.Combobox(right_grp, textvariable=self.audio_var,
                                        values=["Default"], width=10, state="readonly")
        self.audio_menu.pack(side=tk.RIGHT, padx=(0, 6))
        self.audio_menu.bind("<<ComboboxSelected>>", self._on_audio_change)

        # Speed
        tk.Label(right_grp, text="Speed", bg=CTRL_BG, fg=GREY,
                 font=("Segoe UI", 8)).pack(side=tk.RIGHT)
        self.speed_var = tk.StringVar(value="1.0x")
        self.speed_menu = ttk.Combobox(
            right_grp, textvariable=self.speed_var,
            values=[f"{s}x" for s in self.SPEEDS], width=5, state="readonly")
        self.speed_menu.current(3)
        self.speed_menu.pack(side=tk.RIGHT, padx=(0, 6))
        self.speed_menu.bind("<<ComboboxSelected>>", self._on_speed_change)

        # Volume: mute button + slider + label
        self.btn_mute = tk.Button(
            right_grp, text="\U0001f50a", command=self._mute_toggle,
            bg="#222", fg=WHITE, activebackground="#444", bd=0, padx=4,
            font=("Segoe UI", 11), relief=tk.FLAT, cursor="hand2")
        self.btn_mute.pack(side=tk.RIGHT, padx=(0, 2))

        self.vol_scale = tk.Scale(
            right_grp, from_=0, to=200, orient=tk.HORIZONTAL,
            length=100, showvalue=False, bg=CTRL_BG, fg=WHITE,
            troughcolor="#333", highlightthickness=0, sliderrelief=tk.FLAT,
            command=self._on_vol_slider)
        self.vol_scale.set(100)
        self.vol_scale.pack(side=tk.RIGHT, padx=2)

        self.lbl_vol = tk.Label(right_grp, text="100%", bg=CTRL_BG, fg=WHITE,
                                 font=("Segoe UI", 9), width=4)
        self.lbl_vol.pack(side=tk.RIGHT)

        # — Centre group: status / adjustment info —
        centre_grp = tk.Frame(bar, bg=CTRL_BG)
        centre_grp.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=20)

        self.lbl_adj = tk.Label(centre_grp, text="", bg=CTRL_BG, fg=WHITE,
                                 font=("Segoe UI", 10))
        self.lbl_adj.pack(side=tk.LEFT)

        self.lbl_status = tk.Label(centre_grp, text="Ctrl+O to open a movie",
                                    bg=CTRL_BG, fg=GREY, font=("Segoe UI", 9))
        self.lbl_status.pack(side=tk.RIGHT)

        # Progress bar for analysis
        self.pvar = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(
            centre_grp, variable=self.pvar, maximum=100,
            style="pointed.Horizontal.TProgressbar")

    # ---------- graphs ----------
    def _build_graphs(self, parent):
        self.fig = Figure(figsize=(5.5, 4.5), dpi=100, facecolor=BG)
        self.fig.subplots_adjust(hspace=0.45, left=0.12, right=0.97,
                                 top=0.95, bottom=0.08)

        self.ax1 = self.fig.add_subplot(3, 1, 1)
        self.ax1.set_facecolor(BG2)
        self.ax1.set_title("Audio Level (dBFS)", color=ACCENT, fontsize=9, pad=4)
        self.ax1.set_ylabel("dBFS", color=GREY, fontsize=8)
        self.ax1.set_ylim(-80, 0)
        self.ax1.tick_params(colors=GREY, labelsize=7)
        self.ln1, = self.ax1.plot([], [], color=ACCENT, lw=0.6, alpha=0.85)
        self.target_line = self.ax1.axhline(y=-25, color=ORANGE, ls="--", lw=1,
                                             label="Target")
        self.cursor1 = self.ax1.axvline(x=0, color=RED, lw=1.5, alpha=0.9,
                                          label="Now")
        self.ax1.legend(loc="upper right", fontsize=6, facecolor=BG2,
                         edgecolor="#444", labelcolor=WHITE)

        self.ax2 = self.fig.add_subplot(3, 1, 2)
        self.ax2.set_facecolor(BG2)
        self.ax2.set_title("Volume Adjustment (dB)", color=GREEN, fontsize=9, pad=4)
        self.ax2.set_ylabel("dB", color=GREY, fontsize=8)
        self.ax2.tick_params(colors=GREY, labelsize=7)
        self.ax2.axhline(y=0, color="#444", lw=0.5)
        self.ln2, = self.ax2.plot([], [], color=GREEN, lw=0.7)
        self.cursor2 = self.ax2.axvline(x=0, color=RED, lw=1.5, alpha=0.9)

        self.ax3 = self.fig.add_subplot(3, 1, 3)
        self.ax3.set_facecolor(BG2)
        self.ax3.set_title("Applied Volume %", color=YELLOW, fontsize=9, pad=4)
        self.ax3.set_ylabel("Vol %", color=GREY, fontsize=8)
        self.ax3.set_xlabel("Time (min)", color=GREY, fontsize=8)
        self.ax3.set_ylim(0, 100)
        self.ax3.tick_params(colors=GREY, labelsize=7)
        self.ln3, = self.ax3.plot([], [], color=YELLOW, lw=0.7)
        self.cursor3 = self.ax3.axvline(x=0, color=RED, lw=1.5, alpha=0.9)

        self.canvas = FigureCanvasTkAgg(self.fig, master=parent)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # Compressor sliders
        cf = ttk.LabelFrame(parent, text="Compressor", style="D.TLabelframe")
        cf.pack(fill=tk.X, padx=4, pady=(0, 4))
        ci = ttk.Frame(cf, style="D.TFrame")
        ci.pack(fill=tk.X, padx=4, pady=3)

        self.pvars = {}
        sliders = [
            ("Target dB",  -50, -5,   1, -25, 0),
            ("Attack ms",   10, 500,  10, 200, 1),
            ("Release ms", 100, 5000, 100, 1500, 2),
            ("Max Adj dB",   5, 50,    1, 25,  3),
            ("Lookahead s",  0, 10,    1,  3,  4),
        ]
        for label, lo, hi, res, default, col in sliders:
            ttk.Label(ci, text=label, style="Dim.TLabel").grid(
                row=0, column=col * 2, padx=2, sticky=tk.W)
            v = tk.DoubleVar(value=default)
            self.pvars[label] = v
            tk.Scale(ci, from_=lo, to=hi, resolution=res, orient=tk.HORIZONTAL,
                     variable=v, length=100, bg=BG2, fg=WHITE,
                     troughcolor="#333", highlightthickness=0,
                     command=lambda _: self._replan()).grid(
                row=0, column=col * 2 + 1, padx=2)

        # Stats label at bottom of graph panel
        self.lbl_stats = tk.Label(parent, text="", bg=BG, fg=GREY,
                                   font=("Segoe UI", 8), anchor=tk.W)
        self.lbl_stats.pack(fill=tk.X, padx=6)

    # ==============================================================
    #  Keyboard & mouse bindings
    # ==============================================================
    def _bind_keys(self):
        r = self.root
        r.bind("<space>",           lambda e: self._toggle_play())
        r.bind("<s>",               lambda e: self._stop())
        r.bind("<Left>",            lambda e: self._seek_rel(-5))
        r.bind("<Right>",           lambda e: self._seek_rel(5))
        r.bind("<Shift-Left>",      lambda e: self._seek_rel(-30))
        r.bind("<Shift-Right>",     lambda e: self._seek_rel(30))
        r.bind("<Up>",              lambda e: self._vol_step(5))
        r.bind("<Down>",            lambda e: self._vol_step(-5))
        r.bind("<m>",               lambda e: self._mute_toggle())
        r.bind("<f>",               lambda e: self._toggle_fullscreen())
        r.bind("<F11>",             lambda e: self._toggle_fullscreen())
        r.bind("<Escape>",          lambda e: self._exit_fullscreen())
        r.bind("<bracketleft>",     lambda e: self._speed_step(-1))
        r.bind("<bracketright>",    lambda e: self._speed_step(1))
        r.bind("<a>",               lambda e: self._key_toggle_autovol())
        r.bind("<g>",               lambda e: self._toggle_graphs())
        r.bind("<Control-o>",       lambda e: self._browse())
        r.bind("<Control-O>",       lambda e: self._browse())

        # Drag-and-drop support (native tkinter doesn't fully support it,
        # but we handle command-line args and the Browse button)

    # ==============================================================
    #  File open & analysis
    # ==============================================================
    def _browse(self):
        path = filedialog.askopenfilename(
            title="Select Movie File",
            filetypes=[
                ("Video", "*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v *.ts"),
                ("All", "*.*")])
        if path:
            self._open_file(path)

    def _open_file(self, path):
        self._stop()
        self.file_path = path
        title = os.path.basename(path)
        self.root.title(f"{APP_NAME}  v{__version__} — {title}")
        self.splash.place_forget()
        self.lbl_status.config(text=f"Analysing: {title}")
        self.progress_bar.pack(fill=tk.X, pady=2)
        self._analyze(path)

    def _analyze(self, path):
        self.pvar.set(0)

        def run():
            try:
                audio, dur = extract_audio(path, progress_cb=self._progress)
                self.root.after(0, lambda: self._progress(0.97, "Computing loudness…"))
                times, db = compute_loudness(audio)
                del audio
                self.times = times
                self.db = db
                self.duration = dur
                self.root.after(0, self._on_analysis_done)
            except Exception as e:
                self.root.after(0, lambda: self._on_error(str(e)))

        threading.Thread(target=run, daemon=True).start()

    def _progress(self, frac, msg=""):
        self.root.after(0, lambda: self.pvar.set(frac * 100))
        if msg:
            self.root.after(0, lambda: self.lbl_status.config(text=msg))

    def _on_analysis_done(self):
        self.pvar.set(100)
        self.progress_bar.pack_forget()
        n = len(self.db)

        mask = self.db > -80
        avg = np.mean(self.db[mask]) if np.any(mask) else -80
        peak = np.max(self.db)
        low = np.min(self.db[mask]) if np.any(mask) else -80
        self.lbl_stats.config(
            text=f"Avg: {avg:.1f}dB  |  Peak: {peak:.1f}dB  |  Range: {peak - low:.1f}dB  |  {n} pts")
        self.lbl_status.config(text=f"Ready — {self.duration / 60:.1f} min")

        t_min = self.times / 60.0
        self.ln1.set_data(t_min, self.db)
        self.ax1.set_xlim(0, t_min[-1] if len(t_min) > 0 else 1)
        self._replan()
        self.canvas.draw()

        # Start playback automatically
        self._play_new()

    def _on_error(self, msg):
        self.progress_bar.pack_forget()
        self.lbl_status.config(text=f"Error: {msg}")
        messagebox.showerror("Error", msg)

    # ==============================================================
    #  Compressor
    # ==============================================================
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

    # ==============================================================
    #  Playback controls
    # ==============================================================
    def _play_new(self):
        """Load media and start fresh playback."""
        if not self.file_path:
            return
        media = self.vlc_instance.media_new(self.file_path)
        self.player.set_media(media)
        if sys.platform == "win32":
            self.player.set_hwnd(self.video_frame.winfo_id())
        else:
            self.player.set_xwindow(self.video_frame.winfo_id())
        self.player.play()
        self.is_playing = True
        self.btn_play.config(text="\u23f8")   # show pause icon
        # Populate tracks after a short delay (VLC needs time)
        self.root.after(1500, self._populate_tracks)
        self._playback_loop()

    def _toggle_play(self):
        state = self.player.get_state()
        if state == vlc.State.Paused:
            self.player.pause()
            self.is_playing = True
            self.btn_play.config(text="\u23f8")
            self._playback_loop()
        elif state in (vlc.State.Playing,):
            self.player.pause()
            self.is_playing = False
            self.btn_play.config(text="\u25b6")
        elif self.file_path:
            self._play_new()

    def _stop(self):
        self.player.stop()
        self.is_playing = False
        self.btn_play.config(text="\u25b6")
        self.lbl_time.config(text="00:00:00 / 00:00:00")

    def _seek_rel(self, seconds):
        if not self.player.get_media():
            return
        cur = self.player.get_time()
        length = self.player.get_length()
        if cur < 0 or length <= 0:
            return
        new_ms = max(0, min(cur + seconds * 1000, length))
        self.player.set_time(int(new_ms))

    # ── Volume ──
    def _vol_step(self, delta):
        new = max(0, min(200, self.manual_vol + delta))
        self.manual_vol = new
        self.vol_scale.set(new)
        if not self.auto_volume:
            self.player.audio_set_volume(new)
        self._update_vol_icon()

    def _on_vol_slider(self, val):
        self.manual_vol = int(float(val))
        if not self.auto_volume:
            self.player.audio_set_volume(self.manual_vol)
        self.muted = False
        self._update_vol_icon()

    def _mute_toggle(self):
        if self.muted:
            self.muted = False
            self.manual_vol = self.pre_mute_vol
            self.vol_scale.set(self.manual_vol)
            self.player.audio_set_volume(self.manual_vol)
        else:
            self.muted = True
            self.pre_mute_vol = self.manual_vol
            self.manual_vol = 0
            self.vol_scale.set(0)
            self.player.audio_set_volume(0)
        self._update_vol_icon()

    def _update_vol_icon(self):
        v = self.manual_vol
        self.lbl_vol.config(text=f"{v}%")
        if self.muted or v == 0:
            self.btn_mute.config(text="\U0001f507")  # muted icon
        elif v < 50:
            self.btn_mute.config(text="\U0001f509")
        else:
            self.btn_mute.config(text="\U0001f50a")

    # ── Auto-volume ──
    def _toggle_autovol(self):
        self.auto_volume = self.var_autovol.get()
        if not self.auto_volume:
            self.player.audio_set_volume(self.manual_vol)

    def _key_toggle_autovol(self):
        self.var_autovol.set(not self.var_autovol.get())
        self._toggle_autovol()

    # ── Speed ──
    def _on_speed_change(self, event=None):
        txt = self.speed_var.get().replace("x", "")
        try:
            spd = float(txt)
        except ValueError:
            return
        self.player.set_rate(spd)
        self.speed_idx = self.SPEEDS.index(spd) if spd in self.SPEEDS else 3

    def _speed_step(self, delta):
        self.speed_idx = max(0, min(len(self.SPEEDS) - 1, self.speed_idx + delta))
        spd = self.SPEEDS[self.speed_idx]
        self.speed_var.set(f"{spd}x")
        self.player.set_rate(spd)

    # ── Audio / Subtitle tracks ──
    def _populate_tracks(self):
        # Audio tracks
        try:
            tracks = self.player.audio_get_track_description()
            if tracks:
                names = [f"{t[0]}: {t[1].decode('utf-8', errors='replace')}"
                         for t in tracks]
                self.audio_menu["values"] = names
                if names:
                    self.audio_menu.current(min(1, len(names) - 1))
                self._audio_track_ids = [t[0] for t in tracks]
        except Exception:
            pass

        # Subtitle tracks
        try:
            subs = self.player.video_get_spu_description()
            if subs:
                names = [f"{s[0]}: {s[1].decode('utf-8', errors='replace')}"
                         for s in subs]
                self.sub_menu["values"] = names
                if names:
                    self.sub_menu.current(0)
                self._sub_track_ids = [s[0] for s in subs]
        except Exception:
            pass

    def _on_audio_change(self, event=None):
        idx = self.audio_menu.current()
        if hasattr(self, '_audio_track_ids') and idx < len(self._audio_track_ids):
            self.player.audio_set_track(self._audio_track_ids[idx])

    def _on_sub_change(self, event=None):
        idx = self.sub_menu.current()
        if hasattr(self, '_sub_track_ids') and idx < len(self._sub_track_ids):
            self.player.video_set_spu(self._sub_track_ids[idx])

    # ── Fullscreen ──
    def _toggle_fullscreen(self):
        if self.is_fullscreen:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self):
        if self.is_fullscreen:
            return
        self.is_fullscreen = True
        self._pre_fs_graphs = self.graphs_visible
        if self.graphs_visible:
            self._toggle_graphs()
        self.root.attributes("-fullscreen", True)

    def _exit_fullscreen(self):
        if not self.is_fullscreen:
            return
        self.is_fullscreen = False
        self.root.attributes("-fullscreen", False)
        if self._pre_fs_graphs and not self.graphs_visible:
            self._toggle_graphs()

    # ── Graphs show/hide ──
    def _toggle_graphs(self):
        if self.graphs_visible:
            self.graph_panel.pack_forget()
            self.graphs_visible = False
            self.btn_graphs.config(fg=GREY)
        else:
            self.graph_panel.pack(side=tk.RIGHT, fill=tk.BOTH, in_=self.mid)
            self.graphs_visible = True
            self.btn_graphs.config(fg=ACCENT)

    # ── Right-click context menu ──
    def _show_context_menu(self, event):
        menu = tk.Menu(self.root, tearoff=0, bg="#222", fg=WHITE,
                       activebackground="#444", activeforeground=WHITE,
                       font=("Segoe UI", 10))
        menu.add_command(label="\u25b6  Play / Pause", command=self._toggle_play)
        menu.add_command(label="\u23f9  Stop", command=self._stop)
        menu.add_separator()
        menu.add_command(label="\u23ea  Back 10s", command=lambda: self._seek_rel(-10))
        menu.add_command(label="\u23e9  Forward 10s", command=lambda: self._seek_rel(10))
        menu.add_command(label="\u23ea  Back 30s", command=lambda: self._seek_rel(-30))
        menu.add_command(label="\u23e9  Forward 30s", command=lambda: self._seek_rel(30))
        menu.add_separator()

        # Speed submenu
        speed_menu = tk.Menu(menu, tearoff=0, bg="#222", fg=WHITE,
                             activebackground="#444", font=("Segoe UI", 10))
        for spd in self.SPEEDS:
            speed_menu.add_command(
                label=f"{'> ' if spd == self.SPEEDS[self.speed_idx] else '   '}{spd}x",
                command=lambda s=spd: (self.speed_var.set(f"{s}x"),
                                       self.player.set_rate(s)))
        menu.add_cascade(label="Speed", menu=speed_menu)

        menu.add_separator()
        menu.add_checkbutton(label="Auto-Volume", variable=self.var_autovol,
                             command=self._toggle_autovol)
        menu.add_command(label="\U0001f4ca  Toggle Graphs", command=self._toggle_graphs)
        menu.add_command(label="\u26f6  Fullscreen", command=self._toggle_fullscreen)
        menu.add_separator()
        menu.add_command(label="Open File…", command=self._browse)

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ==============================================================
    #  Playback loop — seek bar, cursors, auto-volume
    # ==============================================================
    def _playback_loop(self):
        if not self.is_playing:
            return

        state = self.player.get_state()
        if state in (vlc.State.Ended, vlc.State.Stopped, vlc.State.Error):
            self.is_playing = False
            self.btn_play.config(text="\u25b6")
            return

        length_ms = self.player.get_length()
        current_ms = self.player.get_time()

        if length_ms > 0 and current_ms >= 0:
            pos_sec = current_ms / 1000.0
            total_sec = length_ms / 1000.0
            frac = pos_sec / total_sec if total_sec > 0 else 0

            # Seek bar
            if not self.seeking:
                self._draw_seek_bar(frac)

            # Time
            self.lbl_time.config(
                text=f"{self._fmt(pos_sec)} / {self._fmt(total_sec)}")

            # Graph cursors
            if self.graphs_visible and self.times is not None:
                pos_min = pos_sec / 60.0
                self.cursor1.set_xdata([pos_min, pos_min])
                self.cursor2.set_xdata([pos_min, pos_min])
                self.cursor3.set_xdata([pos_min, pos_min])
                self.canvas.draw_idle()

            # Auto-volume
            if self.auto_volume and self.vol is not None:
                ci = int(pos_sec / CHUNK_SEC)
                ci = max(0, min(ci, len(self.vol) - 1))
                target_vol = int(round(self.vol[ci]))
                target_vol = max(0, min(target_vol, 200))
                self.player.audio_set_volume(target_vol)
                adj = self.gain[ci]
                color = RED if adj < -3 else (GREEN if adj > 3 else WHITE)
                self.lbl_adj.config(
                    text=f"Adj: {adj:+.1f} dB  |  Vol: {target_vol}%", fg=color)
            else:
                self.lbl_adj.config(text="", fg=WHITE)

        self.root.after(200, self._playback_loop)

    # ==============================================================
    #  Helpers
    # ==============================================================
    @staticmethod
    def _fmt(s):
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        sec = int(s % 60)
        return f"{h:02d}:{m:02d}:{sec:02d}"

    def _on_close(self):
        self.player.stop()
        self.root.destroy()


# ==================================================================
def main():
    root = tk.Tk()
    app = App(root)

    # Accept file from command line
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        root.after(300, lambda: app._open_file(sys.argv[1]))

    root.mainloop()


if __name__ == "__main__":
    main()
