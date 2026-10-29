"""简单的 Tkinter 图形界面：选 CUE → 预览曲目 → 一键分轨。

支持把文件 / 文件夹直接拖到输入框或「拖放区」：
- 拖 .cue 文件 → 加载 CUE
- 拖音频文件   → 设为整轨音频
- 拖文件夹     → 设为输出目录
支持单个、多个文件及整个文件夹的拖入，并带有拖入高亮反馈。
"""

from __future__ import annotations

import os
import sys
import threading
from typing import List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from cue2flac.cue_parser import CueSheet, parse_cue
from cue2flac.splitter import split
from cue2flac.batch import process_directory, format_summary

# 拖放为 Windows 原生实现（零依赖）；非 Windows 下 enable_drop 自动空操作。
try:
    from dnd import enable_drop
except Exception:  # noqa: BLE001
    enable_drop = None  # type: ignore[assignment]


# 音频扩展名（用于拖放智能识别）
AUDIO_EXTS = {
    ".flac", ".wav", ".w64", ".ape", ".wv", ".tta", ".tak",
    ".m4a", ".aac", ".mp3", ".ogg", ".oga", ".opus", ".mpc",
    ".mp4", ".mka", ".spx", ".caf", ".aif", ".aiff",
}

# 高亮配色
DRAG_COLOR = "#f5a623"     # 拖入过程中（橙）
OK_COLOR = "#2e9e5b"       # 放下成功（绿）
INVALID_COLOR = "#d9534f"  # 放下被拒绝（红）


def _is_audio(path: str) -> bool:
    return os.path.isfile(path) and os.path.splitext(path)[1].lower() in AUDIO_EXTS


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("cue2flac —— CUE 整轨分轨工具")
        self.geometry("780x540")
        self.resizable(True, True)

        self.sheet: Optional[CueSheet] = None
        self.audio_path: Optional[str] = None
        self.out_dir: Optional[str] = None

        # 记录每个控件的高亮初始状态，便于复位
        self._hl_state: dict[tk.Widget, tuple] = {}

        self._build_widgets()

    # ------------------------------------------------------------------ 界面
    def _build_widgets(self) -> None:
        # 外层「拖放区」容器：本身也是智能路由的拖放目标
        zone = ttk.LabelFrame(
            self,
            text="拖放区 · 可直接把 CUE 文件 / 音频文件 / 文件夹拖到下方输入框（或拖到本区域内自动识别）",
            padding=(10, 6),
        )
        zone.pack(fill="x", padx=10, pady=(10, 4))

        frm = ttk.Frame(zone, padding=4)
        frm.pack(fill="x")

        ttk.Label(frm, text="CUE 文件:").grid(row=0, column=0, sticky="e")
        self.entry_cue = ttk.Entry(frm, width=62)
        self.entry_cue.grid(row=0, column=1, padx=4)
        ttk.Button(frm, text="浏览…", command=self.browse_cue).grid(row=0, column=2)

        ttk.Label(frm, text="音频文件:").grid(row=1, column=0, sticky="e")
        self.entry_audio = ttk.Entry(frm, width=62)
        self.entry_audio.grid(row=1, column=1, padx=4)
        ttk.Button(frm, text="浏览…", command=self.browse_audio).grid(row=1, column=2)

        ttk.Label(frm, text="输出目录:").grid(row=2, column=0, sticky="e")
        self.entry_out = ttk.Entry(frm, width=62)
        self.entry_out.grid(row=2, column=1, padx=4)
        ttk.Button(frm, text="浏览…", command=self.browse_out).grid(row=2, column=2)

        cols = ("#", "标题", "表演者", "起点", "终点")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=12)
        for c in cols:
            self.tree.heading(c, text=c)
            self.tree.column(c, width=120 if c in ("标题", "表演者") else 80)
        self.tree.pack(fill="both", expand=True, padx=10, pady=6)

        self.btn_start = ttk.Button(
            self, text="开始分轨", command=self.start, state="disabled"
        )
        self.btn_start.pack(pady=4)

        self.btn_batch = ttk.Button(
            self, text="批量处理文件夹…", command=self.batch_dir
        )
        self.btn_batch.pack(pady=(0, 4))

        self.text_log = tk.Text(self, height=8, state="normal")
        self.text_log.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # 注册拖放目标
        self._register_drops(zone)

    def _register_drops(self, zone) -> None:
        if not enable_drop:
            return
        # 智能路由：拖到「拖放区」整体，按内容自动分到正确字段
        enable_drop(
            zone,
            on_drop=self._on_smart_drop,
            on_drag_enter=lambda: self._highlight(zone),
            on_drag_leave=lambda: self._unhighlight(zone),
        )
        # 每个输入框独立作为精确目标
        enable_drop(
            self.entry_cue,
            on_drop=self._on_cue_drop,
            on_drag_enter=lambda: self._highlight(self.entry_cue),
            on_drag_leave=lambda: self._unhighlight(self.entry_cue),
        )
        enable_drop(
            self.entry_audio,
            on_drop=self._on_audio_drop,
            on_drag_enter=lambda: self._highlight(self.entry_audio),
            on_drag_leave=lambda: self._unhighlight(self.entry_audio),
        )
        enable_drop(
            self.entry_out,
            on_drop=self._on_out_drop,
            on_drag_enter=lambda: self._highlight(self.entry_out),
            on_drag_leave=lambda: self._unhighlight(self.entry_out),
        )

    # ------------------------------------------------------------- 浏览按钮
    def browse_cue(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 CUE 文件", filetypes=[("CUE 文件", "*.cue"), ("所有文件", "*.*")]
        )
        if path:
            self.load_cue(path)

    def browse_audio(self) -> None:
        path = filedialog.askopenfilename(
            title="选择整轨音频文件",
            filetypes=[
                ("无损音频", "*.flac *.wav *.ape *.wv *.tta *.tak *.m4a"),
                ("所有文件", "*.*"),
            ],
        )
        if path:
            self.audio_path = path
            self._set_entry(self.entry_audio, path)
            self.log(f"已设置音频文件：{path}\n")

    def browse_out(self) -> None:
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.out_dir = path
            self._set_entry(self.entry_out, path)
            self.log(f"已设置输出目录：{path}\n")

    # ----------------------------------------------------------- 拖放处理
    def _on_cue_drop(self, paths: List[str]) -> None:
        cues = [p for p in paths if os.path.isfile(p) and p.lower().endswith(".cue")]
        if not cues:
            self._flash_invalid(self.entry_cue)
            self.log("拖入内容不是有效的 .cue 文件（仅支持 CUE 文件）。\n")
            return
        self._flash_ok(self.entry_cue)
        self.load_cue(cues[0])
        if len(paths) > 1:
            self.log(f"已使用 {os.path.basename(cues[0])}，忽略其余 {len(paths) - 1} 个拖入项。\n")

    def _on_audio_drop(self, paths: List[str]) -> None:
        files = [p for p in paths if os.path.isfile(p)]
        if not files:
            self._flash_invalid(self.entry_audio)
            self.log("拖入的不是文件（音频需要单个文件，文件夹无效）。\n")
            return
        chosen = files[0]
        self.audio_path = chosen
        self._set_entry(self.entry_audio, chosen)
        self._flash_ok(self.entry_audio)
        self.log(f"已设置音频文件：{chosen}\n")
        if len(paths) > 1:
            self.log(f"忽略其余 {len(paths) - 1} 个拖入项。\n")

    def _on_out_drop(self, paths: List[str]) -> None:
        dirs = [p for p in paths if os.path.isdir(p)]
        if dirs:
            chosen = dirs[0]
        elif paths:
            # 拖入的是文件：取其所在目录作为输出目录
            chosen = os.path.dirname(paths[0])
        else:
            self._flash_invalid(self.entry_out)
            return
        if not chosen:
            self._flash_invalid(self.entry_out)
            return
        self.out_dir = chosen
        self._set_entry(self.entry_out, chosen)
        self._flash_ok(self.entry_out)
        self.log(f"已设置输出目录：{chosen}\n")
        if len(paths) > 1:
            self.log(f"忽略其余 {len(paths) - 1} 个拖入项，仅取首个有效目录。\n")

    def _on_smart_drop(self, paths: List[str]) -> None:
        """拖到「拖放区」整体时，按内容智能路由到正确字段。"""
        if not paths:
            self.log("未识别到可拖入的文件或文件夹。\n")
            return
        cues = [p for p in paths if os.path.isfile(p) and p.lower().endswith(".cue")]
        audios = [p for p in paths if _is_audio(p)]
        dirs = [p for p in paths if os.path.isdir(p)]
        if len(paths) == 1:
            p = paths[0]
            if os.path.isfile(p) and p.lower().endswith(".cue"):
                self._on_cue_drop([p])
            elif _is_audio(p):
                self._on_audio_drop([p])
            elif os.path.isdir(p):
                self._on_out_drop([p])
            else:
                self.log(f"无法识别拖入项：{p}\n")
            return
        # 多项：优先取最有意义的一项
        if cues:
            self._on_cue_drop(cues)
        elif audios:
            self._on_audio_drop(audios)
        elif dirs:
            self._on_out_drop(dirs)
        else:
            self.log("拖入内容无法识别（需要 .cue 文件 / 音频文件 / 文件夹）。\n")

    # --------------------------------------------------------- 高亮反馈
    def _set_entry(self, entry: ttk.Entry, value: str) -> None:
        entry.delete(0, tk.END)
        entry.insert(0, value)

    def _highlight(self, widget) -> None:
        try:
            if widget not in self._hl_state:
                self._hl_state[widget] = (
                    widget.cget("highlightthickness"),
                    widget.cget("highlightcolor"),
                    widget.cget("highlightbackground"),
                )
            widget.config(
                highlightthickness=2,
                highlightcolor=DRAG_COLOR,
                highlightbackground=DRAG_COLOR,
            )
        except Exception:
            pass

    def _unhighlight(self, widget) -> None:
        try:
            if widget in self._hl_state:
                thick, color, bg = self._hl_state[widget]
                widget.config(highlightthickness=thick, highlightcolor=color, highlightbackground=bg)
            else:
                widget.config(highlightthickness=0)
        except Exception:
            pass

    def _flash_ok(self, widget) -> None:
        try:
            widget.config(highlightthickness=2, highlightcolor=OK_COLOR, highlightbackground=OK_COLOR)
        except Exception:
            pass
        self.after(700, lambda: self._unhighlight(widget))

    def _flash_invalid(self, widget) -> None:
        try:
            widget.config(
                highlightthickness=2, highlightcolor=INVALID_COLOR, highlightbackground=INVALID_COLOR
            )
        except Exception:
            pass
        self.after(900, lambda: self._unhighlight(widget))

    # ------------------------------------------------------------- CUE 加载
    def load_cue(self, path: str) -> None:
        self._set_entry(self.entry_cue, path)
        try:
            self.sheet = parse_cue(path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("解析失败", str(e))
            return

        if self.sheet.tracks and self.sheet.tracks[0].file:
            self.audio_path = self.sheet.tracks[0].file
            self._set_entry(self.entry_audio, self.audio_path)

        default = os.path.join(
            os.path.dirname(os.path.abspath(path)),
            self.sheet.album_title or "output",
        )
        self.out_dir = default
        self._set_entry(self.entry_out, default)

        self.populate_tree()
        self.btn_start.config(state="normal")
        self.log(
            f"已加载《{self.sheet.album_title or '未命名'}》，共 {self.sheet.total_tracks} 首。\n"
        )

    def populate_tree(self) -> None:
        for row in self.tree.get_children():
            self.tree.delete(row)
        for tr in self.sheet.tracks:
            self.tree.insert(
                "",
                "end",
                values=(
                    f"{tr.number:02d}",
                    tr.title or "",
                    tr.performer or "",
                    _fmt(tr.start),
                    _fmt(tr.end) if tr.end is not None else "EOF",
                ),
            )

    # --------------------------------------------------------------- 分轨
    def start(self) -> None:
        if not self.sheet:
            return
        out = self.out_dir or os.path.join(
            os.path.dirname(self.entry_cue.get()), "output"
        )
        self.btn_start.config(state="disabled")
        self.log("开始分轨…\n")
        threading.Thread(target=self._run, args=(out,), daemon=True).start()

    def _run(self, out: str) -> None:
        try:
            results = split(
                self.sheet,
                out,
                audio_hint=self.audio_path,
                on_progress=self.log,
            )
            self.log("完成！\n")
            messagebox.showinfo("完成", f"已生成 {len(results)} 个文件：\n{out}")
        except Exception as e:  # noqa: BLE001
            self.log(f"错误：{e}\n")
            messagebox.showerror("分轨失败", str(e))
        finally:
            self.btn_start.config(state="normal")

    # ----------------------------------------------------- 批量处理文件夹
    def batch_dir(self) -> None:
        root = filedialog.askdirectory(title="选择要批量扫描的文件夹（含子目录中的 .cue）")
        if not root:
            return
        self.btn_batch.config(state="disabled")
        self.btn_start.config(state="disabled")
        self.log(f"\n开始批量处理：{root}\n")
        threading.Thread(target=self._run_batch, args=(root,), daemon=True).start()

    def _run_batch(self, root: str) -> None:
        try:
            result = process_directory(
                root,
                recursive=True,
                out=None,  # 每个专辑输出到其 CUE 所在目录下的 <专辑名> 子文件夹
                on_progress=self.log,
            )
            # 顶部再打印一次汇总，确保可见
            self.log("\n" + format_summary(result) + "\n")
            if result.success_count == 0:
                messagebox.showwarning(
                    "批量处理",
                    f"未成功处理任何 CUE（共扫描 {result.scanned} 个，跳过 {result.skip_count} 个）。\n"
                    "请查看日志中的跳过原因。",
                )
            else:
                messagebox.showinfo(
                    "批量处理完成",
                    f"成功 {result.success_count} 个，跳过 {result.skip_count} 个。\n"
                    "详见日志汇总。",
                )
        except Exception as e:  # noqa: BLE001
            self.log(f"批量处理出错：{e}\n")
            messagebox.showerror("批量处理出错", str(e))
        finally:
            self.btn_batch.config(state="normal")
            if self.sheet:
                self.btn_start.config(state="normal")

    def log(self, msg: str) -> None:
        self.text_log.insert(tk.END, msg)
        self.text_log.see(tk.END)


def _fmt(seconds) -> str:
    if seconds is None:
        return ""
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def run_gui() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    run_gui()
