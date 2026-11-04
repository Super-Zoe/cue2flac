"""跨模块共用的纯工具函数与格式常量（单一真源）。

本模块只依赖标准库，不导入 ``cue2flac`` 的其它子模块，因此任何模块都可以
安全引用而不会出现循环导入。

集中在这里的内容分两类：

* **格式清单**：``SUPPORTED_EXTS``（可转换）/ ``DSD_EXTS``（1-bit 容器）
  是唯一真源，其余清单（模糊匹配候选、扩展名回退顺序、GUI 文件过滤器）
  全部由它们派生，避免「加了格式但某条链路不认」。
* **工具函数**：文件名清洗、重名后缀、ffmpeg 执行、位深映射，原先在
  ``convert`` / ``batch`` / ``splitter`` 里各写了一遍。

注意这些清单**语义并不相同**，不能简单替换：候选/提示清单比可转换清单
多出 ``.mp3/.ogg/.opus``（仅用于「同目录里有哪些音频」的提示与模糊匹配），
但少掉裸 PCM 容器；GUI 展示顺序则保持历史文案不变。
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# 格式常量（唯一真源）
# ---------------------------------------------------------------------------

#: DSD（1-bit）容器扩展名。
DSD_EXTS = (".dsf", ".dff")

#: 支持直接转换的输入扩展名（小写，含点）。顺序对外可见（CLI 帮助文本按
#: ``[:6]`` 与 ``[-2:]`` 取片段），不得随意调整。
SUPPORTED_EXTS = (
    # PCM 系无损
    ".flac", ".wav", ".ape", ".wv", ".tta", ".tak",
    ".alac", ".m4a", ".aiff", ".aif", ".pcm", ".sf",
    # DSD 系
    ".dsf", ".dff",
)

#: 仅用于「缺失音频提示 / CUE 音源模糊匹配候选」的扩展名：这些格式可以
#: 提示给用户或参与名称比对，但不在可转换清单内。
HINT_ONLY_EXTS = (".mp3", ".ogg", ".opus")

#: 不参与候选筛选的可转换扩展名（裸 PCM / IRCAM，通常没有可比对的文件名）。
_NON_CANDIDATE_EXTS = (".pcm", ".sf")

#: 候选 / 提示清单：可转换清单 ⊋ 关系中的「提示专用」部分由
#: :data:`HINT_ONLY_EXTS` 补上；结果与历史上两份逐字相同的
#: ``_AUDIO_EXTS`` 常量集合一致（顺序不影响 ``in`` 判断）。
AUDIO_HINT_EXTS = tuple(
    dict.fromkeys(
        tuple(e for e in SUPPORTED_EXTS if e not in _NON_CANDIDATE_EXTS)
        + HINT_ONLY_EXTS
    )
)

#: CUE 的 ``FILE`` 行省略扩展名时的尝试优先级；末尾 ``""`` 表示按原名尝试。
#: 只保留仍在 :data:`SUPPORTED_EXTS` 中的项，避免提示一个不支持的格式。
_FALLBACK_PRIORITY = (
    ".flac", ".wav", ".wv", ".ape", ".tta", ".tak", ".m4a", ".dsf", ".dff",
)
EXT_FALLBACK = tuple(e for e in _FALLBACK_PRIORITY if e in SUPPORTED_EXTS) + ("",)

#: GUI 文件选择框的展示顺序（保持历史显示文案逐字不变）。
_GUI_EXTS = (
    ".flac", ".wav", ".ape", ".wv", ".tta", ".tak", ".alac", ".aiff",
    ".dsf", ".dff",
)


def audio_file_patterns() -> str:
    """生成 GUI / 文件对话框用的 ``*.flac *.wav …`` 通配符串。"""
    return " ".join("*" + e for e in _GUI_EXTS if e in SUPPORTED_EXTS)


# ---------------------------------------------------------------------------
# 纯工具函数
# ---------------------------------------------------------------------------


def sanitize_name(name: str) -> str:
    """把文件名 / 专辑名整理为安全的输出名（不含扩展名）。"""
    name = (name or "").strip()
    for ch in '\\/:*?"<>|\r\n\t':
        name = name.replace(ch, "_")
    name = name.strip(" ._")
    if not name:
        name = "output"
    return name[:120]


def unique_name(key: str, used: Dict[str, int], in_stem: bool = False) -> str:
    """同一批处理内避免输出重名。

    ``in_stem=True`` 时后缀加在扩展名前（``a.flac → a_1.flac``），
    ``False`` 时直接拼在末尾（``album → album_1``）。
    """
    if key not in used:
        used[key] = 0
        return key
    used[key] += 1
    n = used[key]
    if in_stem:
        base, ext = os.path.splitext(key)
        return f"{base}_{n}{ext}"
    return f"{key}_{n}"


def run_ffmpeg(
    cmd: List[str],
    error_cls,
    on_progress: Optional[Callable[[str], None]] = None,
    err_picker: Optional[Callable[[subprocess.CompletedProcess], str]] = None,
):
    """执行 ffmpeg：进度回显 + 退出码检查 + 错误输出裁剪 2000 字符。

    ``err_picker`` 允许调用方保留各自的历史错误提取口径
    （``convert`` 用 ``(stderr or stdout).strip()``，``splitter`` 用
    ``stderr.strip() or stdout.strip()``），避免统一时产生细微行为差异。
    """
    if on_progress:
        on_progress("  $ " + " ".join(cmd) + "\n")
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        # 显式 UTF-8：subprocess 在 text 模式下由后台线程解码，若跟随
        # 系统区域（中文 Windows 为 cp936）解码含中文路径/标签的
        # ffmpeg 输出会抛 UnicodeDecodeError 且主线程 try 抓不到，
        # 最终 stdout 变 None 导致后续 .strip() 崩溃。ffmpeg 输出本就 UTF-8。
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        if err_picker is not None:
            err = err_picker(proc)
        else:
            err = (proc.stderr or proc.stdout or "").strip()
        raise error_cls(
            f"ffmpeg 执行失败（退出码 {proc.returncode}）：\n{err[:2000]}"
        )
    return proc


def sample_fmt_for_bits(bits: int, strict: bool = False) -> str:
    """位深 → FLAC 编码器支持的 ``-sample_fmt`` 取值。

    FLAC 编码器只接受 ``s16`` / ``s32``；24bit 必须用 ``s32`` 承载
    （FLAC 会把 ``bits_per_raw_sample`` 记为 24，PCM 仍逐样本无损），
    误传 ``s24`` 会直接报 ``Invalid sample format 's24'``。

    ``strict=True`` 时只接受已知位深 (8/16/24/32)，未知返回空串
    （调用方据此不传 ``-sample_fmt``）；``False`` 时 ``<=16 → s16``、
    其余 → ``s32``，由调用方保证 ``bits`` 非 0。
    """
    if strict and bits not in (8, 16, 24, 32):
        return ""
    return "s16" if bits <= 16 else "s32"
