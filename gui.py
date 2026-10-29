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
from cue2flac.config import get_out_dir, set_out_dir
from cue2flac.layout import plan_rows

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
        # 结果树中「CUE 顶层行」的 iid → 对应 CUE 绝对路径（用于逐项删减）
        self._cue_map: dict[str, str] = {}
        # 用户指定的统一输出目录（None = 使用内置默认：各 CUE 所在目录下的专辑名子文件夹）
        self.out_dir: Optional[str] = get_out_dir()

        self._orig_bg: Optional[str] = None
        # 需要「宽度不足时自动折行」的按钮行：(容器, [(控件, padx, 是否弹性拉伸)])
        self._flows: list = []

        self._build_widgets()

        # 窗口尺寸变化时重新排布按钮行、并让长文字自动换行，防止缩小后文字被裁切
        self.bind("<Configure>", self._on_resize)
        self.after_idle(self._relayout)

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
        self.btn_files = ttk.Button(btn_row, text="选择文件…", command=self._pick_files)
        self.btn_folder = ttk.Button(
            btn_row, text="选择文件夹…", command=self._pick_folder
        )
        self.status_label = ttk.Label(btn_row, text="等待拖入或选择…")
        self._flows.append(
            (
                btn_row,
                [
                    (self.btn_files, 0, False),
                    (self.btn_folder, 6, False),
                    (self.status_label, 12, True),
                ],
            )
        )

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

        # ===== 输出目录 =====
        out_frame = ttk.LabelFrame(self, text="输出目录", padding=(8, 6))
        out_frame.pack(fill="x", padx=12, pady=(0, 6))

        row = ttk.Frame(out_frame)
        row.pack(fill="x", padx=4, pady=2)
        self.lbl_save = ttk.Label(row, text="保存到：")
        self.out_dir_var = tk.StringVar(value=self.out_dir or "")
        self.out_entry = ttk.Entry(row, textvariable=self.out_dir_var)
        self.btn_pick_out = ttk.Button(row, text="选择…", command=self._pick_out_dir)
        self.btn_reset_out = ttk.Button(row, text="恢复默认", command=self._reset_out_dir)
        self._flows.append(
            (
                row,
                [
                    (self.lbl_save, 0, False),
                    (self.out_entry, 6, True),
                    (self.btn_pick_out, 4, False),
                    (self.btn_reset_out, 0, False),
                ],
            )
        )
        self.out_desc = ttk.Label(
            out_frame,
            text=(
                "留空 = 默认：各 CUE 所在目录下的『专辑名』子文件夹；"
                "也可指定统一输出根目录（配置会被记住，下次自动复用）。"
            ),
        )
        self.out_desc.pack(anchor="w", fill="x", padx=4, pady=(2, 0))

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

        # 垂直 + 水平滚动条：内容过多或窗口过窄时，两个方向都能滚动浏览
        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(list_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=4, pady=(4, 0))
        vsb.grid(row=0, column=1, sticky="ns", pady=(4, 0))
        hsb.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 4))
        list_frame.grid_rowconfigure(0, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)

        # 逐项删减：右键菜单、Delete 键、选中态联动「删除选中」按钮
        self.tree.bind("<Button-3>", self._on_tree_right_click)
        self.tree.bind("<Delete>", self._delete_selected)
        self.tree.bind(
            "<<TreeviewSelect>>",
            lambda e: self.btn_delete.config(
                state="normal" if self.tree.selection() else "disabled"
            ),
        )

        # ===== 转换操作 =====
        act = ttk.Frame(self)
        act.pack(fill="x", padx=12, pady=(0, 6))
        self.btn_convert = ttk.Button(
            act, text="开始转换", command=self._on_convert, state="disabled"
        )
        self.btn_delete = ttk.Button(
            act, text="删除选中", command=self._delete_selected, state="disabled"
        )
        self.btn_clear = ttk.Button(act, text="清空", command=self._clear)
        self.lbl_hint = ttk.Label(
            act, text="（右键条目可移除；内容过长时拖动右侧滚动条浏览）"
        )
        self._flows.append(
            (
                act,
                [
                    (self.btn_convert, 0, False),
                    (self.btn_delete, 6, False),
                    (self.btn_clear, 6, False),
                    (self.lbl_hint, 12, True),
                ],
            )
        )

        # ===== 日志 =====
        self.text_log = tk.Text(self, height=9, state="normal")
        self.text_log.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        # 记录需要随窗口缩小自动换行的长文字标签：(控件, 额外边距)
        # 当方框横向缩小、文字超出可用宽度时，这些标签会动态换行，避免被裁切。
        self._wrappables = [
            (self.drop_label, 34),   # padx=16*2
            (self.status_label, 24),  # padx=12*2
            (self.out_desc, 10),     # out_frame padding(8) + 自身 padx(4)
            (self.lbl_hint, 24),     # padx=12*2
        ]

    # ----------------------------------------------------- 自适应布局（防止缩小时文字被裁切）
    def _on_resize(self, event) -> None:
        # 仅响应根窗口本身的尺寸变化（子控件的事件会冒泡上来，需过滤）
        if event.widget is self:
            self._relayout()

    def _relayout(self) -> None:
        self._reflow()
        self._update_wraps()

    def _reflow(self) -> None:
        """按钮行在宽度不足时自动折行，避免按钮 / 文字被裁切看不见。

        Tkinter 的 pack 不会换行，窄窗口下横向排列的按钮会溢出被裁切；
        这里改用 grid 动态重排：按可用宽度贪心分行，弹性控件拉伸填充剩余空间。
        """
        for parent, items in getattr(self, "_flows", []):
            avail = parent.winfo_width() - 8
            if avail < 20:
                # 尚未完成布局（宽度为 0/1）：先按一行排，之后尺寸就绪会再重排
                avail = 10 ** 6

            # 弹性控件（状态栏 / 提示 / 输入框）可被拉伸，按最小占用宽度计算
            sizes = [
                60 if expand else w.winfo_reqwidth() + 2 * padx
                for w, padx, expand in items
            ]
            rows = [[items[i] for i in row] for row in plan_rows(sizes, avail)]

            ncols = max(len(r) for r in rows)
            for c in range(ncols):
                parent.grid_columnconfigure(c, weight=0)
            for r, row_items in enumerate(rows):
                for c, (w, padx, expand) in enumerate(row_items):
                    w.grid_configure(
                        row=r,
                        column=c,
                        sticky="ew" if expand else "w",
                        padx=padx,
                        pady=2,
                    )
                    if expand:
                        parent.grid_columnconfigure(c, weight=1)

    def _update_wraps(self) -> None:
        for label, pad in getattr(self, "_wrappables", []):
            w = label.winfo_width()
            if w > pad + 10:
                label.config(wraplength=max(20, w - pad))

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

    # ----------------------------------------------------- 输出目录选择
    def _pick_out_dir(self) -> None:
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.out_dir_var.set(d)
            self.out_dir = d
            set_out_dir(d)
            self.log(f"输出目录已设为：{d}\n")

    def _reset_out_dir(self) -> None:
        self.out_dir_var.set("")
        self.out_dir = None
        set_out_dir(None)
        self.log("已恢复默认输出目录（各 CUE 所在目录下的专辑名子文件夹）。\n")

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
        self._cue_map.clear()

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
            self._cue_map[parent] = os.path.abspath(cue)

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
                out=self.out_dir,  # None = 每个 CUE 在其所在目录下的 <专辑名> 子文件夹中输出
                copy_cover=True,
                verify=True,  # 转换完成后逐文件验证：可播放 / 参数匹配 / 无损无截断
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
                    f"成功 {success} 个，跳过 {skip} 个。\n"
                    f"已对输出 FLAC 做播放 / 参数 / 无损验证（详见日志）。",
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
        self._cue_map.clear()
        self.btn_convert.config(state="disabled")
        self.btn_delete.config(state="disabled")
        self.status_label.config(text="等待拖入或选择…")
        self.text_log.delete("1.0", tk.END)

    # ----------------------------------------------------- 逐项删减
    def _resolve_cue_iid(self, iid: str) -> Optional[str]:
        """把任意选中的行解析为其所属的 CUE 顶层行 iid（曲目子行归并到父 CUE）。"""
        if not iid or not self.tree.exists(iid):
            return None
        parent = self.tree.parent(iid)
        return parent if parent else iid

    def _remove_cue(self, cue_iid: str) -> None:
        """从结果列表与待处理列表中移除一个 CUE 条目（含其全部曲目子行）。"""
        if not cue_iid or not self.tree.exists(cue_iid):
            return
        path = self._cue_map.pop(cue_iid, None)
        for child in self.tree.get_children(cue_iid):
            self.tree.delete(child)
        self.tree.delete(cue_iid)
        if path and path in self.batch_cue_paths:
            self.batch_cue_paths.remove(path)
        if not self.batch_cue_paths:
            self.btn_convert.config(state="disabled")
        self.btn_delete.config(
            state="normal" if self.tree.selection() else "disabled"
        )
        self.status_label.config(
            text=f"已移除 1 个条目，剩余 {len(self.batch_cue_paths)} 个待处理"
        )
        self.log(f"已移除条目：{os.path.basename(path) if path else cue_iid}\n")

    def _delete_selected(self, event=None) -> None:
        """删除当前选中的条目（可多选；曲目子行会自动归并到所属 CUE）。"""
        sel = self.tree.selection()
        if not sel:
            return
        cue_ids: List[str] = []
        for s in sel:
            cid = self._resolve_cue_iid(s)
            if cid and cid not in cue_ids:
                cue_ids.append(cid)
        for cid in cue_ids:
            self._remove_cue(cid)

    def _on_tree_right_click(self, event) -> None:
        """右键弹出菜单：移除该 CUE 条目。"""
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        cid = self._resolve_cue_iid(iid)
        if not cid:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(
            label="移除该 CUE 条目",
            command=lambda: self._remove_cue(cid),
        )
        menu.post(event.x_root, event.y_root)

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
