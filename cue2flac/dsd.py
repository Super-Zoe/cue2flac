"""DSD（1-bit 德尔塔-西格玛）转换策略的公共实现。

DSD 源（``.dsf`` / ``.dff``）不能逐样本无损地映射为 PCM/FLAC：
DSD 是 1-bit 编码 + 噪声整形，把量化噪声推到超声频段，
所以业界标准做法是 **低通滤波 → 抽取降采样 → 32-bit FLAC**，
并明确标注为「高质量转换」而非「无损」。

本模块把该策略集中实现，供分轨（``splitter``）与直转（``convert``）
两条链路共用，避免两边各写一套而出现行为不一致
（历史上分轨链路漏掉了降采样，导致输出 2822400 Hz 被 FLAC 编码器拒绝）。

采样率的事实依据（实测 ffmpeg ``dsd_lsbf_planar`` / ``dsd_msbf`` 解码器）：

======================  ==================  ====================
DSD 档位                容器声明采样率        ffmpeg 解码输出
======================  ==================  ====================
DSD64                    2 822 400 Hz        352 800 Hz
DSD128                   5 644 800 Hz        705 600 Hz
DSD256                  11 289 600 Hz      1 411 200 Hz
======================  ==================  ====================

即 ffmpeg 的 DSD 解码器按 **44.1kHz 音频基准**输出（每声道 8 bit/样本），
容器里的 2.8224MHz 是 **1-bit 流的比特率**，两者相差 8 倍。
``ffprobe`` 报告的 ``sample_rate`` 是**容器声明值除以 8** 后的结果
（见 ffmpeg 源码 ``libavformat/dsfdec.c`` 的 ``avio_rl32(pb) / 8``），
因此 **不能**用它与下表的键直接比较。
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

from .ffmpeg_provider import resolve_tool
from .verify import probe_audio
from ._util import DSD_EXTS  # 单一真源，保持 dsd.DSD_EXTS 的导出名不变

#: ffmpeg 的 DSD 解码器名。
DSD_CODECS = (
    "dsd", "dsd_lsbf", "dsd_msbf", "dsd_lsbf_planar", "dsd_msbf_planar",
)

#: 按 **ffmpeg 解码输出采样率** 索引的目标采样率与低通截止频率。
#:
#: 键 = 解码输出采样率（Hz），值 = (目标采样率, 低通截止 Hz)。
#:
#: 目标采样率取音频基准的 2 倍（DSD64 → 88.2kHz），既能容纳 22.05kHz
#: 的音频基带，又远低于超声区；低通截止取基带之上一点，滤掉 DSD 的噪声整形
#: 产生的超声嘶嘶声。
DSD_TARGETS: Dict[int, Tuple[int, int]] = {
    352800: (88200, 24000),      # DSD64
    705600: (176400, 40000),     # DSD128
    1411200: (352800, 80000),    # DSD256
}

#: 探测不到档位时的兜底目标（按 DSD64 处理）。
DEFAULT_TARGET = (88200, 24000)


def is_dsd(src: str, ffprobe: Optional[str] = None,
           params: Optional[Dict] = None) -> bool:
    """判断源是否为 DSD（按容器扩展名或 ffprobe 解码器名）。

    ``params`` 可由调用方传入已有的 :func:`~cue2flac.verify.probe_audio` 结果，
    避免同一文件被重复探测；不传时行为与原来完全一致。
    """
    if os.path.splitext(src)[1].lower() in DSD_EXTS:
        return True
    if params is None:
        if ffprobe is None:
            ffprobe = resolve_tool("ffprobe")
        params = probe_audio(src, ffprobe)
    codec = (params or {}).get("codec", "").lower()
    return codec.startswith("dsd")


def dsd_rate(src: str, ffprobe: Optional[str] = None,
             params: Optional[Dict] = None) -> int:
    """探测 DSD 源的**解码输出采样率**（Hz）。

    注意这是 ffprobe 报告的值，即容器声明值 ÷ 8（DSD64 → 352800）。
    它恰好就是 :data:`DSD_TARGETS` 的键，可直接查表。
    ``params`` 传入时直接复用，不再启动 ffprobe。
    """
    if params is None:
        if ffprobe is None:
            ffprobe = resolve_tool("ffprobe")
        params = probe_audio(src, ffprobe)
    return int((params or {}).get("sample_rate") or 0)


def dsd_target(rate: int) -> Tuple[int, int]:
    """按解码输出采样率查表，返回 (目标采样率, 低通截止 Hz)。"""
    if rate in DSD_TARGETS:
        return DSD_TARGETS[rate]
    # 未识别的档位：向上取已知档位中最接近的一档的下限，
    # 保证目标采样率不高于源（避免升采样引入虚假高频）。
    for known in sorted(DSD_TARGETS):
        if rate < known:
            return DSD_TARGETS[known]
    return DSD_TARGETS[max(DSD_TARGETS)]


def dsd_filter(target_sr: int, cutoff: int) -> str:
    """生成 DSD 转换用的 ffmpeg 滤镜链。

    低通用 soxr 高精度重采样器一并完成降采样，避免二次重采样叠加失真。
    """
    return f"lowpass=f={cutoff},aresample=resampler=soxr:osr={target_sr}"


def dsd_note(target_sr: int, cutoff: int) -> str:
    """生成面向用户的性质说明（明确标注「非逐样本无损」）。"""
    return (
        f"DSD 源（1-bit 编码）：已按标准做法低通滤波（截止 {cutoff} Hz）"
        f"并抽取降采样至 {target_sr} Hz / 32-bit FLAC。"
        f"此过程属于高质量转换，并非逐样本无损。"
    )


def dsd_plan(src: str, ffprobe: Optional[str] = None,
             params: Optional[Dict] = None) -> Tuple[int, int, str, str]:
    """一次性算出 DSD 转换所需的全部参数。

    返回 ``(目标采样率, 低通截止 Hz, 滤镜链, 性质说明)``。
    分轨与直转都调用它，保证两条链路的 DSD 行为完全一致。
    ``params`` 传入时复用已有探测结果，不再启动 ffprobe。
    """
    rate = dsd_rate(src, ffprobe, params)
    target_sr, cutoff = dsd_target(rate)
    return target_sr, cutoff, dsd_filter(target_sr, cutoff), dsd_note(target_sr, cutoff)


def dsd_args_from_plan(target_sr: int, filt: str) -> List[str]:
    """由 :func:`dsd_plan` 的已有结果生成 ffmpeg 参数片段。

    分轨与直转都已在转换前调用过 ``dsd_plan``，这里只做纯拼装，
    **不会**再探测一次源文件（否则每首曲目都会多起一个 ffprobe）。
    """
    return [
        "-af", filt,
        "-ar", str(target_sr),
        "-c:a", "flac",
        "-sample_fmt", "s32",
    ]


def dsd_ffmpeg_args(src: str, ffprobe: Optional[str] = None) -> List[str]:
    """生成 DSD → FLAC 所需的 ffmpeg 参数片段。

    调用方把它追加到自己的 ``ffmpeg`` 命令行里即可；内部复用
    :func:`dsd_args_from_plan`，保证与分轨 / 直转链路逐字符一致。
    """
    target_sr, _cutoff, filt, _note = dsd_plan(src, ffprobe)
    return dsd_args_from_plan(target_sr, filt)
