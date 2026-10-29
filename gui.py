"""Tkinter 图形界面：单文件分轨 + 批量拖放分轨。

批量拖放区（「批量处理」标签页）：
- 把文件夹或 .cue 文件拖入「批量拖放区」，系统自动识别完整路径，递归遍历其中的
  全部 .cue 文件；
- 解析后在下方的「解析结果列表」中清晰展示每个 CUE 的专辑、表演者、曲目数量，
  以及每首曲目的 序号 / 标题 / 表演者 / 时长；
- 对格式错误或无法解析的 .cue 也会列出并标注原因；
- 点击「开始批量分轨」后，依据 CUE 信息对相关音频进行无损转换处理，并输出汇总。

单文件标签页同样支持把文件 / 文件夹拖到输入框或「拖放区」（智能识别）。
拖放基于 Windows 原生 COM IDropTarget（零依赖）；非 Windows 下自动降级为浏览按钮。
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
from cue2flac.batch import process_cues, expand_to_cues

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
CUE_COLOR = "#1a5fb4"      # 解析结果列表中「CUE 行」


def _is_audio(path: str) -> bool:
    return os.path.isfile(path) and os.path.splitext(path)[1].lower() in AUDIO_EXTS


def _fmt(seconds) -> str:
    if seconds is None:
        return ""
    m, s = divmod(int(round(seconds)), 60)
    return f"{m:02d}:{s:02d}"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("cue2flac —— CUE 整轨分轨工具")
        self.geometry("840x720")
        self.resizable(True, True)

        # 单文件状态
        self.sheet: Optional[CueSheet] = None
        self.audio_path: Optional[str] = None
        self.out_dir: Optional[str] = None

        # 批量状态
        self.batch_cue_paths: List[str] = []  # 已成功解析、待处理的 CUE 路径

        # 记录每个控件的高亮初始状态，便于复位
        self._hl_state: dict[tk.Widget, tuple] = {}

        self._build_widgets()

    # ------------------------------------------------------------------ 界面
    def _build_widgets(self) -> None:
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))

        tab1 = ttk.Frame(nb)
        nb.add(tab1, text="单文件分轨")
        self._build_single_tab(tab1)

        tab2 = ttk.Frame(nb)
        nb.add(tab2, text="批量处理（拖放）")
        self._build_batch_tab(tab2)

        self.text_log = tk.Text(self, height=9, state="normal")
        self.text_log.pack(fill="both", expand=True, padx=10, pady=(6, 10))

    # ============================================================ 单文件标签页
    def _build_single_tab(self, parent) -> None:
        # 外层「拖放区」容器：本身也是智能路由的拖放目标
        zone = ttk.LabelFrame(
            parent,
            text="拖放区 · 可直接把 CUE 文件 / 音频文件 / 文件夹拖到下方输入框（或拖到本区域内自动识别）",
            padding=(10, 6),
        )
        zone.pack(fill="x", padx=8, pady=(8, 4))

        frm = ttk.Frame(zone, padding=4)
        frm.pack(fill="x")

        ttk.Label(frm, text="CUE 文件:").grid(row=0, column=0, sticky="e")
        self.entry_cue = ttk.Entry(frm, width=64)
        self.entry_cue.grid(row=0, column=1, padx=4)
        ttk.Button(frm, text="浏览…", command=self.browse_cue).grid(row=0, column=2)

        ttk.Label(frm, text="音频文件:").grid(row=1, column=0, sticky="e")
        self.entry_audio = ttk.Entry(frm, width=64)
        self.entry_audio.grid(row=1, column=1, padx=4)
        ttk.Button(frm, text="浏览…", command=self.browse_audio).grid(row=1, column=2)

        ttk.Label(frm, text="输出目录:").grid(row=2, column=0, sticky="e")
        self.entry_out = ttk.Entry(frm, width=64)
        self.entry_out.grid(row=2, column=1, padx=4)
        ttk.Button(frm, text="浏览…", command=self.browse_out).grid(row=2, column=2)

        cols = ("#", "标题", "表演者", "起点", "终点")
        self.tree = ttk.Treeview(parent, columns=cols, show="headings", height=12)
        for c in cols:
            self.tree.heading(c, text=c)
            self.tree.column(c, width=140 if c in ("标题", "表演者") else 80)
        self.tree.pack(fill="both", expand=True, padx=10, pady=6)

        self.btn_start = ttk.Button(
            parent, text="开始分轨", command=self.start, state="disabled"
        )
        self.btn_start.pack(pady=4)

        self._register_drops(zone)

    def _register_drops(self, zone) -> None:
        if not enable_drop:
            return
        enable_drop(
            zone,
            on_drop=self._on_smart_drop,
            on_drag_enter=lambda: self._highlight(zone),
            on_drag_leave=lambda: self._unhighlight(zone),
        )
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

    # ----------------------------------------------------------- 浏览按钮
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
            self.after(0, lambda: messagebox.showinfo(
                "完成", f"已生成 {len(results)} 个文件：\n{out}"))
        except Exception as e:  # noqa: BLE001
            self.log(f"错误：{e}\n")
            self.after(0, lambda: messagebox.showerror("分轨失败", str(e)))
        finally:
            self.btn_start.config(state="normal")

    # ============================================================ 批量标签页
    def _build_batch_tab(self, parent) -> None:
        # 拖放区标签 + 选择按钮
        zone = ttk.LabelFrame(
            parent,
            text="批量拖放区 · 把「文件夹」或「.cue 文件」拖到这里，自动扫描其中全部 .cue",
            padding=(10, 8),
        )
        zone.pack(fill="x", padx=8, pady=(8, 4))

        self._batch_orig_bg = None
        self.batch_drop_label = tk.Label(
            zone,
            text="⬇  将文件夹或多个 .cue 文件拖入此处  ⬇\n（自动递归遍历子目录，发现所有 .cue）",
            relief="groove",
            borderwidth=2,
            padx=12,
            pady=14,
            font=("Microsoft YaHei", 11),
            justify="center",
        )
        self.batch_drop_label.pack(fill="x", padx=4, pady=4)
        self._batch_orig_bg = self.batch_drop_label.cget("bg")

        row = ttk.Frame(zone)
        row.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Button(row, text="选择文件夹…", command=self._on_batch_pick).pack(side="left")
        self.lbl_batch_hint = ttk.Label(
            row, text="拖入或选择后，下方列表会显示每个 CUE 的解析结果；点击「开始批量分轨」执行转换。"
        )
        self.lbl_batch_hint.pack(side="left", padx=8)

        # 解析结果列表（树形：父 = CUE，子 = 曲目）
        cols = ("来源", "#", "标题", "表演者", "时长")
        self.batch_tree = ttk.Treeview(parent, columns=cols, show="headings", height=14)
        self.batch_tree.heading("来源", text="来源 CUE")
        self.batch_tree.column("来源", width=200)
        self.batch_tree.heading("#", text="#")
        self.batch_tree.column("#", width=40, anchor="center")
        self.batch_tree.heading("标题", text="标题")
        self.batch_tree.column("标题", width=220)
        self.batch_tree.heading("表演者", text="表演者")
        self.batch_tree.column("表演者", width=160)
        self.batch_tree.heading("时长", text="时长")
        self.batch_tree.column("时长", width=80, anchor="center")

        vsb = ttk.Scrollbar(parent, orient="vertical", command=self.batch_tree.yview)
        self.batch_tree.configure(yscrollcommand=vsb.set)
        self.batch_tree.pack(fill="both", expand=True, padx=10, pady=6, side="left")
        vsb.pack(fill="y", pady=6, side="right")

        self.btn_batch_start = ttk.Button(
            parent, text="开始批量分轨", command=self._on_batch_start, state="disabled"
        )
        self.btn_batch_start.pack(pady=(0, 4))

        # 注册批量拖放目标
        if enable_drop:
            enable_drop(
                self.batch_drop_label,
                on_drop=self._on_batch_drop,
                on_drag_enter=lambda: self._batch_highlight(True),
                on_drag_over=lambda: self._batch_highlight(True),
                on_drag_leave=lambda: self._batch_highlight(False),
            )

    def _batch_highlight(self, on: bool) -> None:
        try:
            if on:
                self.batch_drop_label.config(bg=DRAG_COLOR, fg="white", relief="solid")
            else:
                self.batch_drop_label.config(
                    bg=self._batch_orig_bg, fg="black", relief="groove"
                )
        except Exception:
            pass

    # ----------------------------------------------------- 批量：拖放 / 选择
    def _on_batch_pick(self) -> None:
        root = filedialog.askdirectory(title="选择包含 .cue 的文件夹（含子目录）")
        if root:
            self._on_batch_drop([root])

    def _on_batch_drop(self, paths: List[str]) -> None:
        """拖入文件 / 文件夹后：解析路径 → 递归收集全部 .cue → 列出解析结果。"""
        self._batch_highlight(False)
        cues = self._collect_cues(paths)
        if not cues:
            self.batch_drop_label.config(
                bg=INVALID_COLOR, fg="white", relief="solid")
            self.after(900, lambda: self._batch_highlight(False))
            self.log("拖入的内容中未找到任何 .cue 文件（请拖入文件夹或 .cue 文件）。\n")
            return

        self.batch_cue_paths = []
        self._populate_batch_tree(cues)
        self.btn_batch_start.config(state="normal")
        self.batch_drop_label.config(bg=OK_COLOR, fg="white", relief="solid")
        self.after(700, lambda: self._batch_highlight(False))
        self.log(
            f"\n已发现并解析 {len(cues)} 个 CUE 文件，解析结果见上方列表。"
            f"点击「开始批量分轨」依据 CUE 信息转换音频。\n"
        )

    def _collect_cues(self, paths: List[str]) -> List[str]:
        """从拖入的项中收集全部 .cue（文件夹递归扫描，文件直接判定），去重并排序。"""
        return expand_to_cues(paths, recursive=True)

    def _populate_batch_tree(self, cues: List[str]) -> None:
        """解析每个 CUE 并在结果列表中展示全部内容（含解析失败项）。"""
        for row in self.batch_tree.get_children():
            self.batch_tree.delete(row)

        for cue in cues:
            try:
                sheet = parse_cue(cue)
            except Exception as e:  # noqa: BLE001
                # 解析失败也列出，清晰标注，便于排查
                self.batch_tree.insert(
                    "",
                    "end",
                    values=(os.path.basename(cue), "", "解析失败", str(e)[:60], ""),
                    tags=("fail",),
                )
                self.log(f"[解析失败] {os.path.basename(cue)} —— {e}\n")
                continue

            parent = self.batch_tree.insert(
                "",
                "end",
                values=(
                    os.path.basename(cue),
                    "",
                    f"《{sheet.album_title or '未命名'}》",
                    sheet.album_performer or "",
                    f"{sheet.total_tracks} 首",
                ),
                tags=("cue",),
                open=True,
            )
            for tr in sheet.tracks:
                dur = (_fmt(tr.end - tr.start)
                       if tr.end is not None else "到结尾")
                self.batch_tree.insert(
                    parent,
                    "end",
                    values=(
                        "",
                        f"{tr.number:02d}",
                        tr.title or "",
                        tr.performer or "",
                        dur,
                    ),
                )
            # 只有成功解析的才进入待处理列表
            self.batch_cue_paths.append(os.path.abspath(cue))

        self.batch_tree.tag_configure("fail", foreground=INVALID_COLOR)
        self.batch_tree.tag_configure("cue", foreground=CUE_COLOR)

    # ----------------------------------------------------- 批量：执行转换
    def _on_batch_start(self) -> None:
        if not self.batch_cue_paths:
            return
        self.btn_batch_start.config(state="disabled")
        self.log("\n开始批量转换处理（依据上方 CUE 信息拆分音频）…\n")
        cue_paths = list(self.batch_cue_paths)
        threading.Thread(
            target=self._batch_runner, args=(cue_paths,), daemon=True
        ).start()

    def _batch_runner(self, cue_paths: List[str]) -> None:
        try:
            result = process_cues(
                cue_paths,
                out=None,  # 每个 CUE 在其所在目录下的 <专辑名> 子文件夹中输出
                copy_cover=True,
                verify=False,
                on_progress=self.log,
            )
            if result.success_count == 0:
                self.after(0, lambda: messagebox.showwarning(
                    "批量处理",
                    f"未成功处理任何 CUE（跳过 {result.skip_count} 个），详见日志。",
                ))
            else:
                self.after(0, lambda: messagebox.showinfo(
                    "批量处理完成",
                    f"成功 {result.success_count} 个，跳过 {result.skip_count} 个。\n详见日志汇总。",
                ))
        except Exception as e:  # noqa: BLE001
            self.log(f"批量处理出错：{e}\n")
            self.after(0, lambda: messagebox.showerror("批量处理出错", str(e)))
        finally:
            if self.batch_cue_paths:
                self.btn_batch_start.config(state="normal")

    # --------------------------------------------------------------- 日志
    def log(self, msg: str) -> None:
        self.text_log.insert(tk.END, msg)
        self.text_log.see(tk.END)


def run_gui() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    run_gui()
