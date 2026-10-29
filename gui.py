"""Tkinter 图形界面：单一拖放区域批量分轨。

唯一的输入是一个文件拖放区域，支持把单个 / 多个文件、文件夹拖入：
- 系统自动识别并解析其完整路径，递归遍历以查找其中全部 .cue 文件；
- 读取每个 CUE 并提取完整信息（音轨标题、表演者、专辑、时长、音轨序号等），
  支持批量识别多个 CUE；
- 以清晰列表展示解析结果（含文件名、所在路径与解析出的元数据明细）；
- 点击「开始转换」后，依据各 CUE 文件中的信息对相关音频执行转换处理。

拖放基于 Windows 原生 COM IDropTarget（零依赖）；非 Windows 下自动降级为浏览按钮。
"""

from __future__ import annotations

import os
import threading
from typing import List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from cue2flac.cue_parser import parse_cue
from cue2flac.batch import process_cues, expand_to_cues

# 拖放为 Windows 原生实现（零依赖）；非 Windows 下 enable_drop 自动空操作。
try:
    from dnd import enable_drop
except Exception:  # noqa: BLE001
    enable_drop = None  # type: ignore[assignment]


# 高亮配色
DRAG_COLOR = "#f5a623"     # 拖入过程中（橙）
OK_COLOR = "#2e9e5b"       # 放下成功（绿）
INVALID_COLOR = "#d9534f"  # 未找到 CUE（红）
CUE_COLOR = "#1a5fb4"      # 解析结果列表中「CUE 行」
FAIL_COLOR = "#d9534f"     # 解析失败行


def _fmt(seconds) -> str:
    if seconds is None:
        return ""
    m, s = divmod(int(round(seconds)), 60)
    return f"{m:02d}:{s:02d}"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("cue2flac —— 拖放批量分轨")
        self.geometry("940x720")
        self.resizable(True, True)

        # 已成功解析、待处理的 CUE 路径
        self.batch_cue_paths: List[str] = []

        self._orig_bg: Optional[str] = None

        self._build_widgets()

    # ------------------------------------------------------------------ 界面
    def _build_widgets(self) -> None:
        # ===== 唯一的输入：拖放区域 =====
        zone = ttk.LabelFrame(self, text="拖放区域", padding=(12, 10))
        zone.pack(fill="x", padx=12, pady=(12, 6))

        self.drop_label = tk.Label(
            zone,
            text=(
                "⬇    将「文件」或「文件夹」拖入此处    ⬇\n"
                "支持单个 / 多个文件、整个文件夹（自动递归遍历其中的全部 .cue 文件）"
            ),
            relief="groove",
            borderwidth=2,
            padx=16,
            pady=22,
            font=("Microsoft YaHei", 12),
            justify="center",
        )
        self.drop_label.pack(fill="x", padx=6, pady=6)
        self._orig_bg = self.drop_label.cget("bg")

        btn_row = ttk.Frame(zone)
        btn_row.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(btn_row, text="选择文件…", command=self._pick_files).pack(side="left")
        ttk.Button(btn_row, text="选择文件夹…", command=self._pick_folder).pack(side="left", padx=6)
        self.status_label = ttk.Label(btn_row, text="等待拖入或选择…")
        self.status_label.pack(side="left", padx=12)

        # 注册拖放目标（Windows 原生；非 Windows 时为 no-op，仍可走上方浏览按钮）
        if enable_drop:
            enable_drop(
                self.drop_label,
                on_drop=self._on_drop,
                on_drag_enter=lambda: self._highlight(True),
                on_drag_over=lambda: self._highlight(True),
                on_drag_leave=lambda: self._highlight(False),
            )
            # 整个 LabelFrame 也作为拖放目标，扩大命中区域
            enable_drop(
                zone,
                on_drop=self._on_drop,
                on_drag_enter=lambda: self._highlight(True),
                on_drag_leave=lambda: self._highlight(False),
            )

        # ===== 解析结果列表 =====
        list_frame = ttk.LabelFrame(
            self, text="解析结果（文件名 · 路径 · 专辑 / 表演者 · 曲目明细）", padding=(8, 6)
        )
        list_frame.pack(fill="both", expand=True, padx=12, pady=(4, 6))

        cols = ("路径", "专辑", "表演者", "时长", "曲目数")
        self.tree = ttk.Treeview(
            list_frame, columns=cols, show="tree headings", height=16
        )
        self.tree.heading("#0", text="文件名")
        self.tree.column("#0", width=280, anchor="w")
        self.tree.heading("路径", text="路径")
        self.tree.column("路径", width=270)
        self.tree.heading("专辑", text="专辑")
        self.tree.column("专辑", width=160)
        self.tree.heading("表演者", text="表演者")
        self.tree.column("表演者", width=150)
        self.tree.heading("时长", text="时长")
        self.tree.column("时长", width=90, anchor="center")
        self.tree.heading("曲目数", text="曲目数")
        self.tree.column("曲目数", width=70, anchor="center")

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(fill="both", expand=True, padx=4, pady=4, side="left")
        vsb.pack(fill="y", pady=4, side="right")

        # ===== 转换操作 =====
        act = ttk.Frame(self)
        act.pack(fill="x", padx=12, pady=(0, 6))
        self.btn_convert = ttk.Button(
            act, text="开始转换", command=self._on_convert, state="disabled"
        )
        self.btn_convert.pack(side="left")
        ttk.Button(act, text="清空", command=self._clear).pack(side="left", padx=6)

        # ===== 日志 =====
        self.text_log = tk.Text(self, height=9, state="normal")
        self.text_log.pack(fill="both", expand=True, padx=12, pady=(0, 10))

    # ----------------------------------------------------- 选择（浏览回退）
    def _pick_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择 .cue 文件（可多选）",
            filetypes=[("CUE 文件", "*.cue"), ("所有文件", "*.*")],
        )
        if paths:
            self._on_drop(list(paths))

    def _pick_folder(self) -> None:
        d = filedialog.askdirectory(title="选择包含 .cue 的文件夹（含子目录）")
        if d:
            self._on_drop([d])

    # ----------------------------------------------------- 拖放 / 选择处理
    def _on_drop(self, paths: List[str]) -> None:
        """拖入文件 / 文件夹后：解析路径 → 递归收集全部 .cue → 列出解析结果。"""
        self._highlight(False)
        cues = expand_to_cues(paths, recursive=True)
        if not cues:
            self._flash(INVALID_COLOR)
            self.status_label.config(text="未找到任何 .cue 文件")
            self.log("拖入的内容中未找到任何 .cue 文件（请拖入文件夹或 .cue 文件）。\n")
            return

        self.batch_cue_paths = []
        self._populate_tree(cues)
        self.btn_convert.config(state="normal")
        self._flash(OK_COLOR)
        self.status_label.config(
            text=f"已识别 {len(cues)} 个 CUE，点击「开始转换」开始处理"
        )
        self.log(
            f"\n已发现并解析 {len(cues)} 个 CUE 文件，解析结果见上方列表。"
            f"点击「开始转换」依据 CUE 信息转换音频。\n"
        )

    def _populate_tree(self, cues: List[str]) -> None:
        """解析每个 CUE 并在结果列表中展示全部内容（含解析失败项）。"""
        for row in self.tree.get_children():
            self.tree.delete(row)

        for cue in cues:
            try:
                sheet = parse_cue(cue)
            except Exception as e:  # noqa: BLE001
                # 解析失败也列出，清晰标注，便于排查
                self.tree.insert(
                    "",
                    "end",
                    text=os.path.basename(cue),
                    values=(os.path.dirname(cue), "解析失败", str(e)[:80], "", ""),
                    tags=("fail",),
                )
                self.log(f"[解析失败] {os.path.basename(cue)} —— {e}\n")
                continue

            # 计算整张专辑总时长（若每首都有终点）
            total = None
            for tr in sheet.tracks:
                if tr.end is not None and tr.start is not None:
                    total = (total or 0) + (tr.end - tr.start)

            parent = self.tree.insert(
                "",
                "end",
                text=os.path.basename(cue),
                values=(
                    os.path.dirname(cue),
                    sheet.album_title or "未命名",
                    sheet.album_performer or "",
                    _fmt(total) if total is not None else "",
                    f"{sheet.total_tracks} 首",
                ),
                tags=("cue",),
                open=True,
            )
            for tr in sheet.tracks:
                dur = _fmt(tr.end - tr.start) if tr.end is not None else "到结尾"
                self.tree.insert(
                    parent,
                    "end",
                    text=f"  {tr.number:02d}  {tr.title or '(未命名)'}",
                    values=("", "", tr.performer or "", dur, ""),
                    tags=("track",),
                )
            # 只有成功解析的才进入待处理列表
            self.batch_cue_paths.append(os.path.abspath(cue))

        self.tree.tag_configure("cue", foreground=CUE_COLOR)
        self.tree.tag_configure("fail", foreground=FAIL_COLOR)

    # ----------------------------------------------------- 转换执行
    def _on_convert(self) -> None:
        if not self.batch_cue_paths:
            return
        self.btn_convert.config(state="disabled")
        self.status_label.config(text="正在转换…")
        self.log("\n开始依据 CUE 信息转换音频…\n")
        cue_paths = list(self.batch_cue_paths)
        threading.Thread(
            target=self._runner, args=(cue_paths,), daemon=True
        ).start()

    def _runner(self, cue_paths: List[str]) -> None:
        try:
            result = process_cues(
                cue_paths,
                out=None,  # 每个 CUE 在其所在目录下的 <专辑名> 子文件夹中输出
                copy_cover=True,
                verify=False,
                on_progress=self.log,
            )
            success = result.success_count
            skip = result.skip_count
            if success == 0:
                self.after(0, lambda: messagebox.showwarning(
                    "转换结果",
                    f"未成功处理任何 CUE（跳过 {skip} 个），详见日志。",
                ))
            else:
                self.after(0, lambda: messagebox.showinfo(
                    "转换完成",
                    f"成功 {success} 个，跳过 {skip} 个。\n详见日志汇总。",
                ))
            self.after(0, lambda: self.status_label.config(
                text=f"处理完成：成功 {success} 个，跳过 {skip} 个"
            ))
        except Exception as e:  # noqa: BLE001
            self.log(f"转换出错：{e}\n")
            self.after(0, lambda: messagebox.showerror("转换出错", str(e)))
            self.after(0, lambda: self.status_label.config(text="转换出错，详见日志"))
        finally:
            if self.batch_cue_paths:
                self.after(0, lambda: self.btn_convert.config(state="normal"))

    # ----------------------------------------------------- 清空
    def _clear(self) -> None:
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.batch_cue_paths = []
        self.btn_convert.config(state="disabled")
        self.status_label.config(text="等待拖入或选择…")
        self.text_log.delete("1.0", tk.END)

    # ----------------------------------------------------- 高亮反馈
    def _highlight(self, on: bool) -> None:
        try:
            if on:
                self.drop_label.config(bg=DRAG_COLOR, fg="white", relief="solid")
            else:
                self.drop_label.config(
                    bg=self._orig_bg, fg="black", relief="groove"
                )
        except Exception:
            pass

    def _flash(self, color: str) -> None:
        try:
            self.drop_label.config(bg=color, fg="white", relief="solid")
        except Exception:
            pass
        self.after(800, lambda: self._highlight(False))

    # ----------------------------------------------------- 日志
    def log(self, msg: str) -> None:
        self.text_log.insert(tk.END, msg)
        self.text_log.see(tk.END)


def run_gui() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    run_gui()
