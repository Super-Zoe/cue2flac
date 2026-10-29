"""批量处理：扫描目录（可递归）下所有 CUE 文件，逐个解析并分轨。

功能要点：
- 自动发现指定目录（含子目录）下的全部 .cue 文件；
- 逐个解析音轨元数据并尝试分轨；
- 对格式错误 / 无法解析 / 音频缺失的文件跳过并记录原因；
- 返回所有成功处理文件的结果汇总（BatchResult）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .cue_parser import parse_cue
from .splitter import split


@dataclass
class ProcessedItem:
    """一个被成功处理的 CUE。"""

    cue: str            # CUE 文件绝对路径
    out_dir: str        # 输出目录
    tracks: int         # 生成的音轨数
    files: List[str] = field(default_factory=list)  # 生成的 FLAC 路径


@dataclass
class SkippedItem:
    """一个被跳过的 CUE（或扫描异常）。"""

    cue: Optional[str]  # 路径（扫描层异常时可能为 None）
    reason: str         # 跳过原因


@dataclass
class BatchResult:
    """一次批量处理的汇总结果。"""

    scanned: int = 0
    processed: List[ProcessedItem] = field(default_factory=list)
    skipped: List[SkippedItem] = field(default_factory=list)

    @property
    def success_count(self) -> int:
        return len(self.processed)

    @property
    def skip_count(self) -> int:
        return len(self.skipped)


def discover_cues(
    root: str,
    recursive: bool = True,
    on_progress: Optional[Callable[[str], None]] = None,
) -> List[str]:
    """扫描 root 下所有 .cue 文件，返回按路径排序的绝对路径列表。

    recursive=True 时遍历子目录。
    """
    if not os.path.isdir(root):
        raise FileNotFoundError(f"目录不存在或不是目录：{root}")

    found: List[str] = []
    seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.lower().endswith(".cue"):
                full = os.path.abspath(os.path.join(dirpath, fn))
                if full not in seen:
                    seen.add(full)
                    found.append(full)
        if not recursive:
            # 仅在顶层处理一次：清空子目录，停止下钻
            dirnames[:] = []

    found.sort(key=lambda p: p.lower())
    if on_progress:
        on_progress(f"扫描完成：在 {root} 下发现 {len(found)} 个 CUE 文件。\n")
    return found


def expand_to_cues(paths: List[str], recursive: bool = True) -> List[str]:
    """把一组拖入的路径（文件 / 文件夹，可混合）展开为去重、排序后的 .cue 路径列表。

    - 路径是 .cue 文件 → 直接纳入；
    - 路径是文件夹   → 递归（recursive=True）扫描其中的全部 .cue；
    - 其它文件       → 忽略。
    返回的均为绝对路径，按小写路径排序。空输入返回空列表。
    """
    cues: List[str] = []
    seen: set[str] = set()
    for p in paths:
        if os.path.isfile(p) and p.lower().endswith(".cue"):
            ap = os.path.abspath(p)
            if ap not in seen:
                seen.add(ap)
                cues.append(ap)
        elif os.path.isdir(p):
            try:
                found = discover_cues(p, recursive=recursive)
            except Exception:  # noqa: BLE001
                found = []
            for f in found:
                if f not in seen:
                    seen.add(f)
                    cues.append(f)
    cues.sort(key=lambda x: x.lower())
    return cues


def _sanitize(name: str) -> str:
    """把专辑名 / 文件名整理为安全的目录名。"""
    name = (name or "").strip()
    for ch in '\\/:*?"<>|\r\n\t':
        name = name.replace(ch, "_")
    name = name.strip(" ._")
    if not name:
        name = "output"
    return name[:120]


def _unique_out_dir(base: str, used: dict) -> str:
    """在同一批处理内避免输出目录重名（同名专辑分别落子文件夹）。"""
    if base not in used:
        used[base] = 0
        return base
    used[base] += 1
    return f"{base}_{used[base]}"


def process_cues(
    cue_paths: List[str],
    out: Optional[str] = None,
    audio_hint: Optional[str] = None,
    copy_cover: bool = True,
    verify: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
) -> BatchResult:
    """处理一个明确的 CUE 路径列表（已由调用方发现 / 收集）。

    与 process_directory 核心逻辑一致：逐个解析 → 分轨，对解析失败、音频缺失等
    跳过并记录原因，最终返回 BatchResult 汇总。

    参数：
      cue_paths  —— 要处理的 .cue 绝对路径列表
      out        —— 统一输出根目录；为 None 时每个 CUE 在其所在目录下的
                    <专辑名> 子文件夹中输出
      audio_hint—— 统一指定的整轨音频（可选，覆盖 CUE 中 FILE）
      copy_cover —— 是否复制封面
      verify     —— 是否逐样本校验无损
      on_progress—— 进度回调（接受一段文本）
    """
    result = BatchResult()
    result.scanned = len(cue_paths)
    if not cue_paths:
        if on_progress:
            on_progress("未发现任何 .cue 文件，无需处理。\n")
        return result

    used_out: dict[str, int] = {}
    for idx, cue in enumerate(cue_paths, 1):
        if on_progress:
            on_progress(f"\n=== [{idx}/{len(cue_paths)}] {os.path.basename(cue)} ===\n")

        # 1) 解析（格式错误 / 编码异常在此捕获并跳过）
        try:
            sheet = parse_cue(cue, audio_hint=audio_hint)
        except Exception as e:  # noqa: BLE001
            result.skipped.append(SkippedItem(cue, f"解析失败：{e}"))
            if on_progress:
                on_progress(f"[跳过] {os.path.basename(cue)}\n  原因：{e}\n")
            continue

        # 2) 确定输出目录
        cue_dir = os.path.dirname(os.path.abspath(cue))
        album = sheet.album_title or os.path.splitext(os.path.basename(cue))[0]
        if out:
            out_dir = _unique_out_dir(os.path.join(out, _sanitize(album)), used_out)
        else:
            out_dir = os.path.join(cue_dir, _sanitize(album))

        # 3) 分轨（音频缺失等可预期错误在此捕获并跳过）
        try:
            files = split(
                sheet,
                out_dir,
                audio_hint=audio_hint,
                copy_cover=copy_cover,
                verify=verify,
                on_progress=on_progress,
            )
            result.processed.append(
                ProcessedItem(cue=cue, out_dir=out_dir, tracks=len(files), files=files)
            )
            if on_progress:
                on_progress(
                    f"[完成] {os.path.basename(cue)} → {len(files)} 首 @ {out_dir}\n"
                )
        except Exception as e:  # noqa: BLE001
            result.skipped.append(SkippedItem(cue, f"分轨失败：{e}"))
            if on_progress:
                on_progress(f"[跳过] {os.path.basename(cue)}\n  原因：{e}\n")
            continue

    if on_progress:
        on_progress("\n" + format_summary(result))
    return result


def process_directory(
    root: str,
    recursive: bool = True,
    out: Optional[str] = None,
    audio_hint: Optional[str] = None,
    copy_cover: bool = True,
    verify: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
) -> BatchResult:
    """扫描 root 下的全部 CUE 并批量分轨。

    参数：
      root        —— 要扫描的根目录
      recursive   —— 是否递归遍历子目录
      out         —— 统一输出根目录；为 None 时每个 CUE 在其所在目录下的
                     <专辑名> 子文件夹中输出
      audio_hint —— 统一指定的整轨音频（可选，覆盖 CUE 中 FILE）
      copy_cover —— 是否复制封面
      verify     —— 是否逐样本校验无损
      on_progress—— 进度回调（接受一段文本）

    返回 BatchResult：含扫描总数、成功列表、跳过列表。
    """
    cues = discover_cues(root, recursive=recursive, on_progress=on_progress)
    # discover_cues 在 root 非法时抛 FileNotFoundError，这里原样向上传播。
    return process_cues(
        cues,
        out=out,
        audio_hint=audio_hint,
        copy_cover=copy_cover,
        verify=verify,
        on_progress=on_progress,
    )


def format_summary(result: BatchResult) -> str:
    """把 BatchResult 渲染为可读的汇总文本。"""
    lines: List[str] = []
    lines.append("─" * 48)
    lines.append("批量处理汇总")
    lines.append(f"  扫描到 CUE  ：{result.scanned} 个")
    lines.append(f"  成功处理    ：{result.success_count} 个")
    lines.append(f"  跳过        ：{result.skip_count} 个")
    lines.append("─" * 48)
    if result.processed:
        lines.append("成功：")
        for it in result.processed:
            lines.append(
                f"  ✓ {os.path.basename(it.cue)} → {it.tracks} 首 @ {it.out_dir}"
            )
    if result.skipped:
        lines.append("跳过（已记录原因）：")
        for it in result.skipped:
            name = os.path.basename(it.cue) if it.cue else "(扫描异常)"
            lines.append(f"  ✗ {name}")
            lines.append(f"      {it.reason}")
    lines.append("─" * 48)
    return "\n".join(lines)
