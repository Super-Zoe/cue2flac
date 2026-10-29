"""简单的 Tkinter 图形界面：选 CUE → 预览曲目 → 一键分轨。"""

from __future__ import annotations

import os
import sys
import threading
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from cue2flac.cue_parser import CueSheet, parse_cue
from cue2flac.splitter import split


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("cue2flac —— CUE 整轨分轨工具")
        self.geometry("760x520")
        self.resizable(True, True)

        self.sheet: Optional[CueSheet] = None
        self.audio_path: Optional[str] = None
        self.out_dir: Optional[str] = None

        self._build_widgets()

    def _build_widgets(self) -> None:
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="x")

        ttk.Label(frm, text="CUE 文件:").grid(row=0, column=0, sticky="e")
        self.entry_cue = ttk.Entry(frm, width=60)
        self.entry_cue.grid(row=0, column=1, padx=4)
        ttk.Button(frm, text="浏览…", command=self.browse_cue).grid(row=0, column=2)

        ttk.Label(frm, text="音频文件:").grid(row=1, column=0, sticky="e")
        self.entry_audio = ttk.Entry(frm, width=60)
        self.entry_audio.grid(row=1, column=1, padx=4)
        ttk.Button(frm, text="浏览…", command=self.browse_audio).grid(row=1, column=2)

        ttk.Label(frm, text="输出目录:").grid(row=2, column=0, sticky="e")
        self.entry_out = ttk.Entry(frm, width=60)
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

        self.text_log = tk.Text(self, height=8, state="normal")
        self.text_log.pack(fill="both", expand=True, padx=10, pady=(0, 10))

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
            self.entry_audio.delete(0, tk.END)
            self.entry_audio.insert(0, path)

    def browse_out(self) -> None:
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.out_dir = path
            self.entry_out.delete(0, tk.END)
            self.entry_out.insert(0, path)

    def load_cue(self, path: str) -> None:
        self.entry_cue.delete(0, tk.END)
        self.entry_cue.insert(0, path)
        try:
            self.sheet = parse_cue(path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("解析失败", str(e))
            return

        if self.sheet.tracks and self.sheet.tracks[0].file:
            self.audio_path = self.sheet.tracks[0].file
            self.entry_audio.delete(0, tk.END)
            self.entry_audio.insert(0, self.audio_path)

        default = os.path.join(
            os.path.dirname(os.path.abspath(path)),
            self.sheet.album_title or "output",
        )
        self.out_dir = default
        self.entry_out.delete(0, tk.END)
        self.entry_out.insert(0, default)

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
            messagebox.showinfo(
                "完成", f"已生成 {len(results)} 个文件：\n{out}"
            )
        except Exception as e:  # noqa: BLE001
            self.log(f"错误：{e}\n")
            messagebox.showerror("分轨失败", str(e))
        finally:
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
