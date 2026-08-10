#!/usr/bin/env python3
"""音频处理工具 — 裁剪 / 淡入淡出 / 均衡器 / 格式转换。

用法:
    python mp3_tool.py                      # GUI 模式
    python mp3_tool.py input.mp3            # CLI 单文件
    python mp3_tool.py "文件夹" --batch      # CLI 批量
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

VERSION = "2.1.0"

# ── DPI 感知 (高清显示) ──
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # Per-monitor DPI
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).parent

PRESETS_FILE = SCRIPT_DIR / "presets.json"

AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus"}

FORMAT_MAP = {
    "mp3":  {"ext": ".mp3",  "codec": "libmp3lame", "bitrate": "192k"},
    "aac":  {"ext": ".m4a",  "codec": "aac",        "bitrate": "192k"},
    "wav":  {"ext": ".wav",  "codec": "pcm_s16le",  "bitrate": None},
    "ogg":  {"ext": ".ogg",  "codec": "libvorbis",  "bitrate": "192k"},
    "flac": {"ext": ".flac", "codec": "flac",       "bitrate": None},
}

EQ_BANDS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]

# ═══════════════════════════════════════════════════════════════════
# ffmpeg 自动定位
# ═══════════════════════════════════════════════════════════════════

def _find_ffmpeg_dir() -> str | None:
    candidates = [
        r"D:\ffmpeg", r"C:\ffmpeg", r"C:\Program Files\ffmpeg",
        os.path.expandvars(r"%LOCALAPPDATA%\ffmpeg"),
        os.path.expandvars(r"%APPDATA%\ffmpeg"),
    ]
    for base in candidates:
        try:
            for entry in Path(base).iterdir():
                if entry.is_dir() and "ffmpeg" in entry.name.lower():
                    bin_dir = entry / "bin"
                    if (bin_dir / "ffmpeg.exe").exists():
                        return str(bin_dir)
        except OSError:
            continue
    return None


def _setup_ffmpeg() -> None:
    if shutil.which("ffmpeg"):
        return
    env_path = os.environ.get("FFMPEG_PATH", "")
    if env_path:
        os.environ["PATH"] = env_path + os.pathsep + os.environ.get("PATH", "")
        if shutil.which("ffmpeg"):
            return
    found = _find_ffmpeg_dir()
    if found:
        os.environ["PATH"] = found + os.pathsep + os.environ.get("PATH", "")
        return
    sys.exit(
        "错误: 找不到 ffmpeg。请安装 ffmpeg 并加入 PATH，\n"
        "或设置环境变量 FFMPEG_PATH 指向 ffmpeg 的 bin 目录。"
    )


_setup_ffmpeg()


# ═══════════════════════════════════════════════════════════════════
# 核心引擎
# ═══════════════════════════════════════════════════════════════════

def get_duration(input_path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", input_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"无法读取文件 '{input_path}'\n{result.stderr}")
    info = json.loads(result.stdout)
    return float(info["format"]["duration"])


def process_file(
    input_path: str,
    output_path: str,
    *,
    trim_mode: str = "duration",
    duration: float = 180.0,
    start_time: float = 0.0,
    end_time: float = 180.0,
    fade_in: float = 3.0,
    fade_out: float = 3.0,
    fade_type: str = "logarithmic",
    output_format: str = "mp3",
    eq_bands: dict[int, float] | None = None,
) -> None:
    """处理单个音频文件（可能抛出异常）。"""
    if not Path(input_path).exists():
        raise FileNotFoundError(f"找不到输入文件 '{input_path}'")

    total_sec = get_duration(input_path)

    # ── 确定截取范围 ──
    if trim_mode == "range":
        trim_start = max(0.0, start_time)
        trim_end = min(end_time, total_sec)
        trim_dur = trim_end - trim_start
    else:
        trim_start = 0.0
        trim_dur = min(duration, total_sec)
        trim_end = trim_dur

    if trim_dur <= 0:
        raise RuntimeError(f"截取时长无效 ({trim_dur:.1f}s)")

    # ── 淡入淡出 ──
    actual_fade_in = min(fade_in, trim_dur / 3)
    actual_fade_out = min(fade_out, trim_dur / 3)
    curve = "tri" if fade_type == "logarithmic" else "lin"
    fade_out_start = trim_dur - actual_fade_out

    # ── 构建滤镜链 ──
    filters = [f"atrim={trim_start:.6f}:{trim_end:.6f}"]

    # EQ
    if eq_bands:
        for freq in EQ_BANDS:
            gain = eq_bands.get(freq, 0.0)
            if abs(gain) > 0.1:
                filters.append(f"equalizer=f={freq}:t=q:w=1:g={gain:.1f}")

    # 淡入 (afade 的 st 相对于 atrim 输出，从 0 开始)
    if actual_fade_in > 0:
        filters.append(
            f"afade=t=in:st=0:d={actual_fade_in:.3f}:curve={curve}"
        )
    # 淡出
    if actual_fade_out > 0:
        filters.append(
            f"afade=t=out:st={fade_out_start:.6f}:d={actual_fade_out:.3f}:curve={curve}"
        )

    filter_str = ",".join(filters)

    # ── 输出编码参数 ──
    fmt = FORMAT_MAP.get(output_format, FORMAT_MAP["mp3"])

    cmd = ["ffmpeg", "-y", "-vn", "-i", input_path, "-af", filter_str]
    if fmt["bitrate"]:
        cmd += ["-c:a", fmt["codec"], "-b:a", fmt["bitrate"]]
    else:
        cmd += ["-c:a", fmt["codec"]]
    cmd.append(output_path)

    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(result.stderr)


def generate_preview(input_path: str, params: dict) -> str:
    """生成 15 秒预览文件，返回临时文件路径。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.close()

    total_sec = get_duration(input_path)
    start = params.get("start_time", 0.0)
    if params.get("trim_mode") == "range":
        preview_start = start
    else:
        preview_start = 0.0

    preview_end = min(preview_start + 15.0, total_sec)
    preview_dur = preview_end - preview_start

    preview_params = {
        **params,
        "trim_mode": "range",
        "start_time": preview_start,
        "end_time": preview_end,
        "duration": preview_dur,
    }
    process_file(input_path, tmp.name, **preview_params)
    return tmp.name


def batch_process(
    source_dir: str,
    output_dir: str | None = None,
    progress_callback=None,
    log_callback=None,
    cancel_event=None,
    **params,
) -> tuple[int, int]:
    """批量处理文件夹中的音频。返回 (成功数, 总数)。"""
    source = Path(source_dir)
    if not source.is_dir():
        raise FileNotFoundError(f"文件夹不存在 '{source_dir}'")

    audio_files = sorted(
        f for f in source.iterdir()
        if f.suffix.lower() in AUDIO_EXTENSIONS
    )
    total = len(audio_files)
    if total == 0:
        return 0, 0

    out_dir = Path(output_dir) if output_dir else (source / "output")
    out_dir.mkdir(parents=True, exist_ok=True)

    fmt_ext = FORMAT_MAP.get(params.get("output_format", "mp3"), FORMAT_MAP["mp3"])["ext"]

    success = 0
    for i, f in enumerate(audio_files, 1):
        if cancel_event and cancel_event.is_set():
            break
        out_path = out_dir / f"{f.stem}_processed{fmt_ext}"
        try:
            if log_callback:
                log_callback(f"[{i}/{total}] {f.name}")
            process_file(str(f), str(out_path), **params)
            success += 1
            if log_callback:
                log_callback(f"  OK ({i}/{total})")
        except Exception as e:
            if log_callback:
                log_callback(f"  FAIL: {e}")
        if progress_callback:
            progress_callback(i, total)

    return success, total


# ═══════════════════════════════════════════════════════════════════
# 预设系统
# ═══════════════════════════════════════════════════════════════════

def load_presets() -> dict:
    if PRESETS_FILE.exists():
        try:
            with open(PRESETS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_presets(presets: dict) -> None:
    with open(PRESETS_FILE, "w", encoding="utf-8") as f:
        json.dump(presets, f, ensure_ascii=False, indent=2)


def get_current_params(gui) -> dict:
    """从 GUI 控件收集当前所有参数。"""
    return {
        "trim_mode": gui._trim_mode.get(),
        "duration": gui._dur_var.get(),
        "start_time": gui._start_time_sec,
        "end_time": gui._end_time_sec,
        "fade_in": gui._fade_in_var.get(),
        "fade_out": gui._fade_out_var.get(),
        "fade_type": gui._curve_var.get(),
        "output_format": gui._fmt_var.get(),
        "eq_bands": {str(f): round(gui._eq_vars[f].get(), 1) for f in EQ_BANDS},
    }


def apply_params(gui, params: dict) -> None:
    """将预设参数回填到 GUI 控件。"""
    gui._trim_mode.set(params.get("trim_mode", "duration"))
    gui._dur_var.set(params.get("duration", 180))
    gui._fade_in_var.set(params.get("fade_in", params.get("fade_seconds", 3)))
    gui._fade_out_var.set(params.get("fade_out", params.get("fade_seconds", 3)))
    gui._curve_var.set(params.get("fade_type", "logarithmic"))
    gui._fmt_var.set(params.get("output_format", "mp3"))
    gui._start_h_var.set(int(params.get("start_time", 0) // 60))
    gui._start_s_var.set(int(params.get("start_time", 0) % 60))
    gui._end_h_var.set(int(params.get("end_time", 180) // 60))
    gui._end_s_var.set(int(params.get("end_time", 180) % 60))
    eq_bands = params.get("eq_bands", {})
    for f in EQ_BANDS:
        val = float(eq_bands.get(str(f), eq_bands.get(f, 0)))
        gui._eq_vars[f].set(val)
        gui._eq_labels_val[f].configure(text=f"{val:.1f}")


# ═══════════════════════════════════════════════════════════════════
# GUI
# ═══════════════════════════════════════════════════════════════════

class AudioToolGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"音频处理工具 v{VERSION}")
        self.root.geometry("760x740")
        self.root.resizable(True, True)
        self.root.minsize(640, 640)

        self._drag_files: list[str] = []
        self._cancel = threading.Event()
        self._log_queue: queue.Queue = queue.Queue()
        self._processing = False
        self._presets: dict = load_presets()
        self._out_dir: str = ""  # 输出目录，空表示 auto

        self._setup_dnd()
        self._build_ui()
        self._poll_log()

    # ── DnD ──

    def _setup_dnd(self) -> None:
        try:
            from tkinterdnd2 import DND_FILES, TkinterDnD  # noqa
            self._has_dnd = True
        except ImportError:
            self._has_dnd = False

    # ── UI 构建 ──

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # ── 输入区域 ──
        input_frame = ttk.LabelFrame(main, text="输入文件", padding=8)
        input_frame.pack(fill=tk.X, pady=(0, 6))

        btn_row = ttk.Frame(input_frame)
        btn_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(btn_row, text="添加文件", command=self._add_files).pack(
            side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_row, text="添加文件夹", command=self._add_folder).pack(
            side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_row, text="清空", command=self._clear_files).pack(side=tk.LEFT)

        list_container = ttk.Frame(input_frame)
        list_container.pack(fill=tk.BOTH, expand=True)
        self._file_list = tk.Listbox(list_container, height=3, selectmode=tk.EXTENDED)
        scrollbar = ttk.Scrollbar(list_container, command=self._file_list.yview)
        self._file_list.configure(yscrollcommand=scrollbar.set)
        self._file_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        if self._has_dnd:
            self._file_list.drop_target_register("DND_Files")
            self._file_list.dnd_bind("<<Drop>>", self._on_drop)

        # ── Notebook 设置 ──
        self._notebook = ttk.Notebook(main)
        self._notebook.pack(fill=tk.X, pady=(0, 6))
        self._build_trim_tab()
        self._build_eq_tab()
        self._build_output_tab()

        # ── 日志 ──
        log_frame = ttk.LabelFrame(main, text="日志", padding=4)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 6))
        self._log_text = tk.Text(log_frame, height=6, state=tk.DISABLED, wrap=tk.WORD,
                                  font=("Consolas", 9))
        log_scroll = ttk.Scrollbar(log_frame, command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=log_scroll.set)
        self._log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # ── 底部控制栏 ──
        bottom = ttk.Frame(main)
        bottom.pack(fill=tk.X)

        self._progress = ttk.Progressbar(bottom, mode="determinate")
        self._progress.pack(fill=tk.X, pady=(0, 6))

        btn_frame = ttk.Frame(bottom)
        btn_frame.pack(fill=tk.X)
        self._start_btn = ttk.Button(btn_frame, text="开始处理", command=self._start)
        self._start_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._stop_btn = ttk.Button(btn_frame, text="停止", command=self._stop,
                                     state=tk.DISABLED)
        self._stop_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._preview_btn = ttk.Button(btn_frame, text="预览", command=self._preview)
        self._preview_btn.pack(side=tk.LEFT)
        ttk.Label(btn_frame, text=f"v{VERSION}", foreground="gray").pack(side=tk.RIGHT)

    # ── Tab 1: 裁剪 ──

    def _build_trim_tab(self) -> None:
        tab = ttk.Frame(self._notebook, padding=8)
        self._notebook.add(tab, text="裁剪设置")

        # 裁剪模式
        mode_frame = ttk.Frame(tab)
        mode_frame.pack(fill=tk.X, pady=2)
        self._trim_mode = tk.StringVar(value="duration")
        ttk.Radiobutton(mode_frame, text="时长模式 (从头截取)", variable=self._trim_mode,
                         value="duration", command=self._on_mode_change).pack(
            side=tk.LEFT, padx=(0, 12))
        ttk.Radiobutton(mode_frame, text="范围模式 (起止点)", variable=self._trim_mode,
                         value="range", command=self._on_mode_change).pack(side=tk.LEFT)

        # 时长模式控件
        self._dur_frame = ttk.Frame(tab)
        self._dur_frame.pack(fill=tk.X, pady=2)
        ttk.Label(self._dur_frame, text="截取时长:", width=10).pack(side=tk.LEFT)
        self._dur_var = tk.IntVar(value=180)
        self._dur_scale = ttk.Scale(self._dur_frame, from_=10, to=600,
                                     variable=self._dur_var,
                                     command=self._on_dur_scale)
        self._dur_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        dur_spin = ttk.Spinbox(self._dur_frame, from_=10, to=600, width=5,
                                textvariable=self._dur_var, command=self._on_dur_spin)
        dur_spin.pack(side=tk.LEFT)
        self._dur_label = ttk.Label(self._dur_frame, text="秒 (3:00)", width=12)
        self._dur_label.pack(side=tk.LEFT, padx=4)
        self._dur_var.trace_add("write", self._update_dur_label)

        # 范围模式控件
        self._range_frame = ttk.Frame(tab)

        ttk.Label(self._range_frame, text="开始时间:", width=10).pack(side=tk.LEFT)
        self._start_h_var = tk.IntVar(value=0)
        self._start_s_var = tk.IntVar(value=0)
        ttk.Spinbox(self._range_frame, from_=0, to=599, width=4,
                     textvariable=self._start_h_var).pack(side=tk.LEFT)
        ttk.Label(self._range_frame, text="分").pack(side=tk.LEFT)
        ttk.Spinbox(self._range_frame, from_=0, to=59, width=4,
                     textvariable=self._start_s_var).pack(side=tk.LEFT)
        ttk.Label(self._range_frame, text="秒").pack(side=tk.LEFT)

        ttk.Label(self._range_frame, text="  结束时间:", width=10).pack(
            side=tk.LEFT, padx=(16, 0))
        self._end_h_var = tk.IntVar(value=3)
        self._end_s_var = tk.IntVar(value=0)
        ttk.Spinbox(self._range_frame, from_=0, to=599, width=4,
                     textvariable=self._end_h_var).pack(side=tk.LEFT)
        ttk.Label(self._range_frame, text="分").pack(side=tk.LEFT)
        ttk.Spinbox(self._range_frame, from_=0, to=59, width=4,
                     textvariable=self._end_s_var).pack(side=tk.LEFT)
        ttk.Label(self._range_frame, text="秒").pack(side=tk.LEFT)

        # 淡入
        fi_frame = ttk.Frame(tab)
        fi_frame.pack(fill=tk.X, pady=2)
        ttk.Label(fi_frame, text="淡入时长:", width=10).pack(side=tk.LEFT)
        self._fade_in_var = tk.IntVar(value=3)
        ttk.Scale(fi_frame, from_=0, to=15, variable=self._fade_in_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        ttk.Spinbox(fi_frame, from_=0, to=15, width=5,
                     textvariable=self._fade_in_var).pack(side=tk.LEFT)
        ttk.Label(fi_frame, text="秒").pack(side=tk.LEFT, padx=4)

        # 淡出
        fo_frame = ttk.Frame(tab)
        fo_frame.pack(fill=tk.X, pady=2)
        ttk.Label(fo_frame, text="淡出时长:", width=10).pack(side=tk.LEFT)
        self._fade_out_var = tk.IntVar(value=3)
        ttk.Scale(fo_frame, from_=0, to=15, variable=self._fade_out_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        ttk.Spinbox(fo_frame, from_=0, to=15, width=5,
                     textvariable=self._fade_out_var).pack(side=tk.LEFT)
        ttk.Label(fo_frame, text="秒").pack(side=tk.LEFT, padx=4)

        # 曲线
        curve_frame = ttk.Frame(tab)
        curve_frame.pack(fill=tk.X, pady=2)
        ttk.Label(curve_frame, text="曲线类型:", width=10).pack(side=tk.LEFT)
        self._curve_var = tk.StringVar(value="logarithmic")
        ttk.Combobox(curve_frame, textvariable=self._curve_var,
                      values=["logarithmic", "linear"], state="readonly",
                      width=14).pack(side=tk.LEFT)

    def _on_mode_change(self) -> None:
        if self._trim_mode.get() == "range":
            self._dur_frame.pack_forget()
            self._range_frame.pack(fill=tk.X, pady=2)
        else:
            self._range_frame.pack_forget()
            self._dur_frame.pack(fill=tk.X, pady=2)

    # ── Tab 2: EQ (竖滑条) ──

    def _build_eq_tab(self) -> None:
        tab = ttk.Frame(self._notebook, padding=8)
        self._notebook.add(tab, text="均衡器")

        self._eq_vars: dict[int, tk.DoubleVar] = {}
        self._eq_labels_val: dict[int, ttk.Label] = {}

        # 提示
        ttk.Label(tab, text="各频段增益 (-12dB ~ +12dB)，0 为直通",
                  foreground="gray").pack(anchor=tk.W, pady=(0, 6))

        # 竖滑条容器
        sliders_area = ttk.Frame(tab)
        sliders_area.pack(fill=tk.BOTH, expand=True, pady=4)

        for freq in EQ_BANDS:
            col = ttk.Frame(sliders_area, width=50)
            col.pack(side=tk.LEFT, fill=tk.Y, padx=3)

            # +12 标签
            ttk.Label(col, text="+12", foreground="gray", font=("", 7)).pack()
            # -12 标签 (占位，后面用值标签替换)
            ttk.Label(col, text="", font=("", 7)).pack()  # spacer top

            # 竖滑条
            var = tk.DoubleVar(value=0.0)
            self._eq_vars[freq] = var
            scale = ttk.Scale(col, from_=12, to=-12, variable=var,
                               orient=tk.VERTICAL, length=140,
                               command=lambda v, f=freq: self._on_eq_change(f, v))
            scale.pack()

            # 数值标签
            val_lbl = ttk.Label(col, text="0.0", foreground="gray", font=("", 7))
            val_lbl.pack()
            self._eq_labels_val[freq] = val_lbl

            # 频率标签
            label = f"{freq}" if freq < 1000 else f"{freq // 1000}kHz"
            ttk.Label(col, text=label, font=("", 8)).pack()

            # -12 标签
            ttk.Label(col, text="-12", foreground="gray", font=("", 7)).pack()

        # 重置按钮
        ttk.Button(tab, text="重置全部频段", command=self._reset_eq).pack(pady=(8, 0))

    def _on_eq_change(self, freq: int, val: str) -> None:
        try:
            v = float(val)
            self._eq_labels_val[freq].configure(text=f"{v:.1f}")
        except Exception:
            pass

    def _reset_eq(self) -> None:
        for freq in EQ_BANDS:
            self._eq_vars[freq].set(0.0)
            self._eq_labels_val[freq].configure(text="0.0")

    # ── Tab 3: 输出 & 预设 ──

    def _build_output_tab(self) -> None:
        tab = ttk.Frame(self._notebook, padding=8)
        self._notebook.add(tab, text="输出与预设")

        # 输出格式
        fmt_frame = ttk.Frame(tab)
        fmt_frame.pack(fill=tk.X, pady=4)
        ttk.Label(fmt_frame, text="输出格式:", width=10).pack(side=tk.LEFT)
        self._fmt_var = tk.StringVar(value="mp3")
        ttk.Combobox(fmt_frame, textvariable=self._fmt_var,
                      values=list(FORMAT_MAP.keys()), state="readonly",
                      width=10).pack(side=tk.LEFT)

        # 输出目录
        out_frame = ttk.Frame(tab)
        out_frame.pack(fill=tk.X, pady=4)
        ttk.Label(out_frame, text="输出目录:", width=10).pack(side=tk.LEFT)
        self._out_entry_var = tk.StringVar(value="(源文件夹下的 output 子文件夹)")
        self._out_entry = ttk.Entry(out_frame, textvariable=self._out_entry_var, width=42)
        self._out_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        ttk.Button(out_frame, text="浏览...", command=self._browse_out_dir).pack(side=tk.LEFT)
        ttk.Button(out_frame, text="重置为默认", command=self._reset_out_dir).pack(
            side=tk.LEFT, padx=(4, 0))

        # 预设
        sep = ttk.Separator(tab, orient=tk.HORIZONTAL)
        sep.pack(fill=tk.X, pady=12)

        preset_frame = ttk.Frame(tab)
        preset_frame.pack(fill=tk.X, pady=4)
        ttk.Label(preset_frame, text="预设管理:", width=10).pack(side=tk.LEFT)
        self._preset_var = tk.StringVar(value="")
        self._preset_cb = ttk.Combobox(preset_frame, textvariable=self._preset_var,
                                        values=[], state="readonly", width=18)
        self._preset_cb.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(preset_frame, text="加载", command=self._load_preset).pack(
            side=tk.LEFT, padx=2)
        ttk.Button(preset_frame, text="保存", command=self._save_preset).pack(
            side=tk.LEFT, padx=2)
        ttk.Button(preset_frame, text="删除", command=self._delete_preset).pack(
            side=tk.LEFT, padx=2)

        self._refresh_preset_list()

    def _browse_out_dir(self) -> None:
        path = filedialog.askdirectory(title="选择输出文件夹")
        if path:
            self._out_entry_var.set(path)
            self._out_dir = path

    def _reset_out_dir(self) -> None:
        self._out_entry_var.set("(源文件夹下的 output 子文件夹)")
        self._out_dir = ""

    def _refresh_preset_list(self) -> None:
        names = list(self._presets.keys())
        self._preset_cb.configure(values=names)

    # ── 时长控件 ──

    def _on_dur_scale(self, val: str) -> None:
        v = round(float(val))
        self._dur_var.set(v)

    def _on_dur_spin(self) -> None:
        try:
            self._dur_scale.set(self._dur_var.get())
        except Exception:
            pass

    def _update_dur_label(self, *args) -> None:
        try:
            v = self._dur_var.get()
        except Exception:
            return
        m, s = divmod(v, 60)
        self._dur_label.configure(text=f"秒 ({m}:{s:02d})")

    @property
    def _start_time_sec(self) -> int:
        return self._start_h_var.get() * 60 + self._start_s_var.get()

    @property
    def _end_time_sec(self) -> int:
        return self._end_h_var.get() * 60 + self._end_s_var.get()

    # ── 文件操作 ──

    def _add_files(self) -> None:
        exts = " ".join(f"*{e}" for e in AUDIO_EXTENSIONS)
        paths = filedialog.askopenfilenames(
            title="选择音频文件",
            filetypes=[("音频文件", exts), ("所有文件", "*.*")]
        )
        for p in paths:
            if p not in self._drag_files:
                self._drag_files.append(p)
                self._file_list.insert(tk.END, Path(p).name)

    def _add_folder(self) -> None:
        path = filedialog.askdirectory(title="选择包含音频的文件夹")
        if not path:
            return
        files = sorted(
            f for f in Path(path).iterdir()
            if f.suffix.lower() in AUDIO_EXTENSIONS
        )
        if not files:
            messagebox.showinfo("提示", "该文件夹中没有支持的音频文件。")
            return
        self._drag_files.clear()
        self._file_list.delete(0, tk.END)
        for f in files:
            self._drag_files.append(str(f))
            self._file_list.insert(tk.END, f.name)
        self._log(f"已加载 {len(files)} 个文件，来自 {path}")

    def _clear_files(self) -> None:
        self._drag_files.clear()
        self._file_list.delete(0, tk.END)

    def _on_drop(self, event) -> None:
        import re as _re
        raw = event.data
        files: list[str] = []
        for m in _re.finditer(r"\{([^}]*)\}", raw):
            files.append(m.group(1))
        if not files:
            files = raw.split()

        for p in files:
            path = Path(p)
            if path.is_dir():
                for f in sorted(path.iterdir()):
                    if (f.suffix.lower() in AUDIO_EXTENSIONS
                            and str(f) not in self._drag_files):
                        self._drag_files.append(str(f))
                        self._file_list.insert(tk.END, f.name)
            elif path.suffix.lower() in AUDIO_EXTENSIONS:
                if str(path) not in self._drag_files:
                    self._drag_files.append(str(path))
                    self._file_list.insert(tk.END, path.name)

    # ── 日志 ──

    def _log(self, msg: str) -> None:
        self._log_queue.put(msg)

    def _poll_log(self) -> None:
        while True:
            try:
                item = self._log_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, tuple) and item[0] == "progress":
                self._progress.configure(value=item[1])
            elif isinstance(item, tuple) and item[0] == "done":
                self._reset_ui()
            else:
                self._log_text.configure(state=tk.NORMAL)
                self._log_text.insert(tk.END, str(item) + "\n")
                self._log_text.see(tk.END)
                self._log_text.configure(state=tk.DISABLED)
        self.root.after(100, self._poll_log)

    # ── 预设操作 ──

    def _save_preset(self) -> None:
        name = simpledialog.askstring("保存预设", "请输入预设名称：", parent=self.root)
        if not name:
            return
        self._presets[name] = get_current_params(self)
        save_presets(self._presets)
        self._refresh_preset_list()
        self._preset_var.set(name)
        self._log(f"预设 '{name}' 已保存")

    def _load_preset(self) -> None:
        name = self._preset_var.get()
        if not name or name not in self._presets:
            messagebox.showwarning("提示", "请先选择一个预设。")
            return
        apply_params(self, self._presets[name])
        self._on_mode_change()
        self._log(f"已加载预设 '{name}'")

    def _delete_preset(self) -> None:
        name = self._preset_var.get()
        if not name or name not in self._presets:
            return
        if messagebox.askyesno("确认", f"确定要删除预设 '{name}' 吗？", parent=self.root):
            del self._presets[name]
            save_presets(self._presets)
            self._refresh_preset_list()
            self._preset_var.set("")
            self._log(f"预设 '{name}' 已删除")

    # ── 处理 ──

    def _gather_params(self) -> dict:
        eq_bands = {f: round(self._eq_vars[f].get(), 1) for f in EQ_BANDS}
        return {
            "trim_mode": self._trim_mode.get(),
            "duration": self._dur_var.get(),
            "start_time": self._start_time_sec,
            "end_time": self._end_time_sec,
            "fade_in": self._fade_in_var.get(),
            "fade_out": self._fade_out_var.get(),
            "fade_type": self._curve_var.get(),
            "output_format": self._fmt_var.get(),
            "eq_bands": eq_bands,
        }

    def _resolve_out_dir(self) -> str | None:
        """解析输出目录。返回 None 表示 auto（源文件夹/output）。"""
        if self._out_dir:
            return self._out_dir
        txt = self._out_entry_var.get().strip()
        if txt and txt != "(源文件夹下的 output 子文件夹)":
            if Path(txt).is_dir():
                return txt
        return None

    def _start(self) -> None:
        if not self._drag_files:
            messagebox.showwarning("提示", "请先添加音频文件或文件夹。")
            return

        self._cancel.clear()
        self._processing = True
        self._start_btn.configure(state=tk.DISABLED)
        self._stop_btn.configure(state=tk.NORMAL)
        self._preview_btn.configure(state=tk.DISABLED)
        self._progress.configure(value=0)
        self._log_text.configure(state=tk.NORMAL)
        self._log_text.delete("1.0", tk.END)
        self._log_text.configure(state=tk.DISABLED)

        params = self._gather_params()
        total = len(self._drag_files)
        self._log(f"开始处理 {total} 个文件...")
        self._log(f"模式={params['trim_mode']}, 格式={params['output_format']}, "
                  f"淡入={params['fade_in']}s 淡出={params['fade_out']}s")
        self._progress.configure(maximum=total)

        out_dir = self._resolve_out_dir()

        fmt_ext = FORMAT_MAP[params["output_format"]]["ext"]

        def worker() -> None:
            success = 0
            for i, fp in enumerate(self._drag_files, 1):
                if self._cancel.is_set():
                    break
                name = Path(fp).name
                if out_dir:
                    base = Path(out_dir)
                else:
                    base = Path(fp).parent / "output"
                out_path = str(base / f"{Path(fp).stem}_processed{fmt_ext}")
                Path(out_path).parent.mkdir(parents=True, exist_ok=True)

                self._log(f"[{i}/{total}] {name}")
                try:
                    process_file(fp, out_path, **params)
                    success += 1
                    self._log(f"  OK")
                except Exception as e:
                    self._log(f"  FAIL: {e}")
                self._log_queue.put(("progress", i))

            self._log(f"\n完成: {success}/{total} 成功")
            self._log_queue.put(("done", None))

        threading.Thread(target=worker, daemon=True).start()

    def _stop(self) -> None:
        self._cancel.set()
        self._log("[已停止]")
        self._reset_ui()

    def _reset_ui(self) -> None:
        self._processing = False
        self._start_btn.configure(state=tk.NORMAL)
        self._stop_btn.configure(state=tk.DISABLED)
        self._preview_btn.configure(state=tk.NORMAL)

    # ── 预览 ──

    def _preview(self) -> None:
        """用 ffplay 播放 15 秒预览。"""
        selected = self._file_list.curselection()
        if not selected:
            messagebox.showwarning("提示", "请在文件列表中选择一个文件进行预览。")
            return
        idx = selected[0]
        fp = self._drag_files[idx]
        self._log(f"正在生成预览: {Path(fp).name}")
        try:
            params = self._gather_params()
            preview_path = generate_preview(fp, params)
            self._log("正在播放预览... (关闭 ffplay 窗口以结束)")
            subprocess.Popen(
                ["ffplay", "-nodisp", "-autoexit", preview_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            self._log(f"预览失败: {e}")


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

def cli_main() -> None:
    parser = argparse.ArgumentParser(description="音频处理工具 — 裁剪/淡入淡出/EQ/格式转换")
    parser.add_argument("input", nargs="?", help="输入文件或文件夹路径")
    parser.add_argument("-o", "--output", help="输出文件/目录路径")
    parser.add_argument("-d", "--duration", type=float, default=180.0, help="截取时长，秒")
    parser.add_argument("--start", type=float, default=0.0, help="范围模式: 开始时间(秒)")
    parser.add_argument("--end", type=float, default=180.0, help="范围模式: 结束时间(秒)")
    parser.add_argument("--fade-in", type=float, default=3.0, help="淡入时长，秒")
    parser.add_argument("--fade-out", type=float, default=3.0, help="淡出时长，秒")
    parser.add_argument("-t", "--type", choices=["logarithmic", "linear"],
                        default="logarithmic", help="淡入淡出曲线")
    parser.add_argument("--format", choices=list(FORMAT_MAP.keys()), default="mp3",
                        help="输出格式")
    parser.add_argument("--eq", nargs="*", help="EQ 频段设置，格式: freq=gain (如 1000=3 250=-2)")
    parser.add_argument("--preset", help="从预设文件加载指定预设")
    parser.add_argument("--batch", action="store_true", help="批量处理文件夹")
    parser.add_argument("-v", "--version", action="store_true", help="显示版本")

    args = parser.parse_args()

    if args.version:
        print(f"mp3_tool v{VERSION}")
        return

    # 加载预设
    eq_bands = {}
    if args.preset:
        presets = load_presets()
        if args.preset in presets:
            p = presets[args.preset]
            args.duration = p.get("duration", args.duration)
            args.start = p.get("start_time", args.start)
            args.end = p.get("end_time", args.end)
            args.fade_in = p.get("fade_in", p.get("fade_seconds", args.fade_in))
            args.fade_out = p.get("fade_out", p.get("fade_seconds", args.fade_out))
            args.type = p.get("fade_type", args.type)
            args.format = p.get("output_format", args.format)
            eq_bands = {int(k): v for k, v in p.get("eq_bands", {}).items()}
            print(f"已加载预设: {args.preset}")

    # 解析 CLI EQ
    if args.eq:
        for item in args.eq:
            try:
                freq_str, gain_str = item.split("=")
                eq_bands[int(freq_str)] = float(gain_str)
            except ValueError:
                print(f"警告: 无效 EQ 格式 '{item}'，应为 freq=gain")

    trim_mode = "range" if args.start != 0.0 else "duration"
    params = {
        "trim_mode": trim_mode,
        "duration": args.duration,
        "start_time": args.start,
        "end_time": args.end,
        "fade_in": args.fade_in,
        "fade_out": args.fade_out,
        "fade_type": args.type,
        "output_format": args.format,
        "eq_bands": eq_bands,
    }

    if not args.input:
        parser.print_help()
        return

    input_path = Path(args.input)

    if args.batch or input_path.is_dir():
        success, total = batch_process(
            str(input_path), args.output,
            log_callback=lambda msg: print(msg),
            progress_callback=lambda i, t: None,
            **params,
        )
        print(f"\n完成: {success}/{total} 成功")
    else:
        fmt_ext = FORMAT_MAP[params["output_format"]]["ext"]
        out = args.output or f"{input_path.stem}_processed{fmt_ext}"
        print(f"处理: {input_path}")
        process_file(str(input_path), out, **params)
        print(f"输出: {out}")


# ═══════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════

def main() -> None:
    if len(sys.argv) <= 1:
        try:
            from tkinterdnd2 import TkinterDnD
            root = TkinterDnD.Tk()
        except ImportError:
            root = tk.Tk()
        AudioToolGUI(root)
        root.mainloop()
    else:
        cli_main()


if __name__ == "__main__":
    main()
