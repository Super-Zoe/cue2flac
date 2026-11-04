"""批量处理：扫描目录（可递归）下所有 CUE 文件，逐个解析并分轨。

功能要点：
- 自动发现指定目录（含子目录）下的全部 .cue 文件；
- 逐个解析音轨元数据并尝试分轨；
- 对格式错误 / 无法解析 / 音频缺失的文件跳过并记录原因；
- 返回所有成功处理文件的结果汇总（BatchResult）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from .cue_parser import parse_cue
from .splitter import split
from .convert import (
    SUPPORTED_EXTS,
    convert_files,
    format_convert_summary,
)
from ._util import sanitize_name, unique_name

#: 分轨输出文件名特征：``NN. 标题``（见 splitter._safe_name）。
#: 用于识别「程序自己生成的分轨产物」，避免重复扫描时把输出再当母带转换。
#:
#: 注意 ``\\s+?`` 用非贪婪匹配——否则它会把标题的首个字符也吃掉
#: （``01. 第一首`` 会变成 ``一首``，导致与 CUE 标题比不上）。
_TRACK_OUT_NAME_RE = re.compile(r"^(\d{1,3})\.\s+?(\S.*)$")


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


def _sheet_stamp(path: str) -> Tuple[int, int]:
    """CUE 文件的 (size, mtime_ns)，用于判定解析缓存是否过期。"""
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return (-1, -1)


def get_cached_sheet(cue: str, sheets: Optional[dict]):
    """从解析缓存里取出仍有效的 ``CueSheet``；无有效条目时返回 None。

    缓存值约定为 ``(stamp, sheet)``；若调用方直接放入 ``CueSheet``，
    也按「不再校验时间戳」的宽松方式兼容。这样「拖入 → 展示 → 开始转换」
    三个阶段可以复用同一次 ``parse_cue``，而用户中途改动 CUE 时
    （size / mtime 变化）会自动失效并重解析，不会用到陈旧快照。
    """
    if not sheets:
        return None
    entry = sheets.get(os.path.abspath(cue))
    if entry is None:
        return None
    if isinstance(entry, tuple) and len(entry) == 2:
        stamp, sheet = entry
        if stamp != _sheet_stamp(cue):
            return None
        return sheet
    return entry


def _walk_files(
    root: str,
    recursive: bool,
    predicate: Callable[[str], bool],
) -> List[str]:
    """扫描 root 下的文件，返回去重、按小写路径排序的绝对路径。

    ``predicate`` 接收文件名（不含目录），决定是否纳入结果。
    root 非法时抛 :class:`FileNotFoundError` —— ``process_directory``
    依赖这个异常原样向上传播，不能在这里吞掉。
    """
    if not os.path.isdir(root):
        raise FileNotFoundError(f"目录不存在或不是目录：{root}")

    found: List[str] = []
    seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        for fn in filenames:
            if predicate(fn):
                full = os.path.abspath(os.path.join(dirpath, fn))
                if full not in seen:
                    seen.add(full)
                    found.append(full)
        if not recursive:
            # 仅在顶层处理一次：清空子目录，停止下钻
            dirnames[:] = []
    found.sort(key=lambda p: p.lower())
    return found


def _unique_sorted(
    paths: List[str],
    exclude: Optional[Callable[[str], bool]] = None,
) -> List[str]:
    """把一组路径转成绝对路径、去重、按小写路径排序。

    ``exclude(path)`` 返回 True 的路径不纳入结果。展开拖入路径
    （文件 / 目录混合）时用它统一处理「绝对路径 + seen + 排序」模板。
    """
    found: List[str] = []
    seen: set[str] = set()
    for p in paths:
        ap = os.path.abspath(p)
        if ap in seen:
            continue
        seen.add(ap)
        if exclude is not None and exclude(ap):
            continue
        found.append(ap)
    found.sort(key=lambda x: x.lower())
    return found


def discover_cues(
    root: str,
    recursive: bool = True,
    on_progress: Optional[Callable[[str], None]] = None,
) -> List[str]:
    """扫描 root 下所有 .cue 文件，返回按路径排序的绝对路径列表。

    recursive=True 时遍历子目录。
    """
    found = _walk_files(root, recursive, lambda fn: fn.lower().endswith(".cue"))
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
    candidates: List[str] = []
    for p in paths:
        if os.path.isfile(p) and p.lower().endswith(".cue"):
            candidates.append(p)
        elif os.path.isdir(p):
            try:
                candidates.extend(discover_cues(p, recursive=recursive))
            except Exception:  # noqa: BLE001
                continue
    return _unique_sorted(candidates)


def discover_audio_files(root: str, recursive: bool = True) -> List[str]:
    """扫描 root 下所有受支持的音频文件（不含 .cue），返回排序后的绝对路径。"""
    return _walk_files(
        root,
        recursive,
        lambda fn: os.path.splitext(fn)[1].lower() in SUPPORTED_EXTS,
    )


def expand_to_inputs(
    paths: List[str],
    recursive: bool = True,
    audio_hint: Optional[str] = None,
    sheet_cache: Optional[dict] = None,
) -> Tuple[List[str], List[str]]:
    """把一组拖入的路径展开为 (cue 列表, 待直转音频列表)。

    直转列表只包含**没有被任何 CUE 引用**的音频文件——否则同一张专辑的整轨母带
    会既被 CUE 分轨、又被单独转一次，造成重复输出。

    ``audio_hint``（对应 CLI 的 ``--audio``）必须原样传给 :func:`parse_cue`：
    它会覆盖 CUE 里 ``FILE`` 行的指向，即使用户指定的母带与 CUE 写的文件名
    不一致（例如 CUE 写 ``Album.ape``、磁盘上是 ``Album-master.ape``），
    解析结果才是真实被消费的那一个。不传时会把该母带误判为「未被引用」，
    导致既分轨又直转、输出重复。

    ``sheet_cache`` 可选：把本次已解析成功的 CUE 结果按绝对路径写入，
    供 ``process_cues`` / GUI 展示层复用，避免同一 CUE 被解析 2~3 次。
    不传时行为与原来完全一致，**返回值结构始终是 ``(cues, audios)``**。
    """
    cues = expand_to_cues(paths, recursive=recursive)

    # 收集被 CUE 引用的音源（按解析后的绝对路径）
    referenced: set[str] = set()
    # 本批 CUE 的曲目标题集合——分轨产物命名为「NN. <曲目标题>.flac」，
    # 用它来识别「程序自己生成的分轨产物」。
    track_titles: set[str] = set()
    for cue in cues:
        try:
            sheet = parse_cue(cue, audio_hint=audio_hint)
        except Exception:  # noqa: BLE001
            continue
        if sheet_cache is not None:
            sheet_cache[os.path.abspath(cue)] = (_sheet_stamp(cue), sheet)
        for tr in sheet.tracks:
            if tr.file:
                referenced.add(os.path.abspath(tr.file))
            if tr.title:
                track_titles.add(tr.title.strip().lower())

    def _is_generated_output(path: str) -> bool:
        """判断路径是否是**本批 CUE 生成的分轨产物**。

        分轨输出命名为 ``NN. <曲目标题>.flac``（见 splitter._safe_name），
        而文件名里的序号 + 标题必须与某个 CUE 的曲目对得上，才认定为产物。
        仅凭「NN. 开头」判断会误伤真实母带——按曲目编号命名的整轨专辑
        （如 ``01. 某专辑.ape``）也很常见。
        """
        stem = os.path.splitext(os.path.basename(path))[0]
        m = _TRACK_OUT_NAME_RE.match(stem)
        if not m:
            return False
        title = m.group(2).strip().lower()
        if not title:
            return False
        return any(title == t or title.startswith(t) for t in track_titles)

    candidates: List[str] = []
    for p in paths:
        if os.path.isfile(p):
            if os.path.splitext(p)[1].lower() in SUPPORTED_EXTS:
                candidates.append(p)
        elif os.path.isdir(p):
            try:
                candidates.extend(discover_audio_files(p, recursive=recursive))
            except Exception:  # noqa: BLE001
                continue

    def _excluded(ap: str) -> bool:
        return ap in referenced or _is_generated_output(ap)

    audios = _unique_sorted(candidates, exclude=_excluded)
    return cues, audios


def _sanitize(name: str) -> str:
    """兼容别名：实现见 :func:`cue2flac._util.sanitize_name`。"""
    return sanitize_name(name)


def _unique_out_dir(base: str, used: dict) -> str:
    """兼容别名：同名专辑分别落 ``album_1`` / ``album_2`` 子文件夹。"""
    return unique_name(base, used)


def process_cues(
    cue_paths: List[str],
    out: Optional[str] = None,
    audio_hint: Optional[str] = None,
    copy_cover: bool = True,
    verify: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
    sheets: Optional[dict] = None,
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
      sheets     —— 可选的已解析 CUE 缓存（见 :func:`get_cached_sheet`）。
                    **不传时行为与原来完全一致**：逐个 ``parse_cue``。
                    解析失败或缓存过期的 CUE 会重新解析并照常记为 skipped。
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
        #    优先复用前面 expand_to_inputs 已解析且仍未过期的结果。
        try:
            sheet = get_cached_sheet(cue, sheets)
            if sheet is None:
                sheet = parse_cue(cue, audio_hint=audio_hint)
        except Exception as e:  # noqa: BLE001
            result.skipped.append(SkippedItem(cue, f"解析失败：{e}"))
            if on_progress:
                on_progress(f"[跳过] {os.path.basename(cue)}\n  原因：{e}\n")
            continue

        # 1.5) 输出解析提示（如音源文件名不一致被自动匹配）
        for note in getattr(sheet, "notes", []) or []:
            if on_progress:
                on_progress(f"[提示] {note}\n")

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


def process_all(
    paths: List[str],
    recursive: bool = True,
    out: Optional[str] = None,
    audio_hint: Optional[str] = None,
    copy_cover: bool = True,
    verify: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
    sheet_cache: Optional[dict] = None,
) -> Tuple[BatchResult, List]:
    """混合处理：一次调用里既做 CUE 分轨，也做不依赖 CUE 的音频直转。

    拖入的路径会被展开为两类任务：
    - ``.cue`` 文件      → 按 CUE 分轨成多首 FLAC；
    - 其它音频文件      → 整轨直接转成一个 FLAC。

    返回 (分轨结果, 直转结果列表)。

    ``sheet_cache`` 不传时内部自建一份，使「展开输入 → 分轨」两个阶段
    复用同一次 CUE 解析；调用方也可传入自己的 dict 与 GUI 展示层共享。
    """
    sheets = sheet_cache if sheet_cache is not None else {}
    cues, audios = expand_to_inputs(
        paths, recursive=recursive, audio_hint=audio_hint, sheet_cache=sheets
    )

    # 只有确实存在 CUE 时才进入分轨流程——否则 process_cues 会打印
    # 「未发现任何 .cue 文件，无需处理。」这种噪音提示，
    # 在「纯直转」场景下会让人误以为出错了。
    if cues:
        result = process_cues(
            cues,
            out=out,
            audio_hint=audio_hint,
            copy_cover=copy_cover,
            verify=verify,
            on_progress=on_progress,
            sheets=sheets,
        )
    else:
        result = BatchResult()

    convert_results = []
    if audios:
        if on_progress:
            on_progress(
                f"\n=== 不依赖 CUE 的直接转换：{len(audios)} 个音频文件 ===\n"
            )
        convert_results = convert_files(
            audios,
            out_dir=out,
            copy_cover=copy_cover,
            verify=verify,
            on_progress=on_progress,
        )
        if on_progress:
            on_progress("\n" + format_convert_summary(convert_results) + "\n")

    return result, convert_results


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
