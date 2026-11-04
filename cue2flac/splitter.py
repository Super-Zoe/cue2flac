"""分轨核心：调用 ffmpeg 把整轨音频按 CUE 切分为独立 FLAC。

无损说明：FLAC 是无损压缩，对 PCM 样本做有损重编码是不可能的；
因此「解码整轨 → 用 ffmpeg 的 FLAC 编码器重新封装」得到的是逐样本一致的 PCM，
与直接从源文件取对应区段完全一致（可由 --verify 校验）。
"""

from __future__ import annotations

import os
import subprocess
from typing import List, Optional

from .cue_parser import CueSheet, Track
from .dsd import DSD_EXTS as _DSD_EXTS, DSD_CODECS as _DSD_CODECS
from .dsd import dsd_plan, dsd_args_from_plan
from .ffmpeg_provider import resolve_tool
from .verify import probe_audio, verify_outputs, format_verification_report
from ._util import (
    AUDIO_HINT_EXTS as _AUDIO_EXTS,
    run_ffmpeg as _run_ffmpeg_impl,
    sample_fmt_for_bits,
)


class SplitterError(RuntimeError):
    """分轨过程中的可预期错误。"""


def _is_dsd(audio_path: str, ffprobe: str) -> bool:
    """判断音源是否为 DSD（.dsf/.dff 容器，或 ffprobe 解码器为 dsd*）。

    DSD 为 1-bit 编码，无法逐样本无损映射为 PCM/FLAC；此函数仅用于
    在转换时给出明确提示，并避免把 DSD 的“1 bit”误当成 PCM 位深。
    """
    low = (audio_path or "").lower()
    if low.endswith(_DSD_EXTS):
        return True
    try:
        out = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=nw=1:nk=1",
                audio_path,
            ],
            capture_output=True, text=True, timeout=30,
        ).stdout
        for line in out.splitlines():
            if line.strip().lower() in _DSD_CODECS:
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _missing_audio_hint(audio_path: str) -> str:
    """音频缺失时，列出同目录下的候选音频文件，便于用户快速定位。"""
    d = os.path.dirname(os.path.abspath(audio_path)) if audio_path else ""
    if not d or not os.path.isdir(d):
        return ""
    try:
        entries = sorted(
            e for e in os.listdir(d)
            if os.path.splitext(e)[1].lower() in _AUDIO_EXTS
            and os.path.isfile(os.path.join(d, e))
        )
    except OSError:
        return ""
    if not entries:
        return ""
    shown = entries[:8]
    extra = f" 等 {len(entries)} 个文件" if len(entries) > len(shown) else ""
    return "同目录下的音频文件：" + "、".join(shown) + extra


def _safe_name(tr: Track) -> str:
    """生成安全的输出文件名：NN. 标题（必要时追加表演者去重）。"""
    title = (tr.title or f"Track {tr.number:02d}").strip()
    for ch in '\\/:*?"<>|\r\n\t':
        title = title.replace(ch, "_")
    title = title.strip(" ._")
    if not title:
        title = f"Track{tr.number:02d}"
    name = f"{tr.number:02d}. {title}"
    # 若标题过短或重复风险，附加表演者
    if tr.performer:
        base = f"{tr.number:02d}. {tr.performer} - {title}"
        base = "".join(c if c not in '\\/:*?"<>|\r\n\t' else "_" for c in base).strip(" ._")
        if len(base) <= 120:
            name = base
    return name[:120]


def _run_ffmpeg(cmd, on_progress=None):
    """兼容别名：公共实现见 :func:`cue2flac._util.run_ffmpeg`。

    保留分轨链路原有的 ``stderr.strip() or stdout.strip()`` 错误提取口径
    与 ``"  $ <cmd>"`` 进度回显。
    """
    return _run_ffmpeg_impl(
        cmd, SplitterError,
        on_progress=on_progress,
        err_picker=lambda p: (p.stderr or "").strip() or (p.stdout or "").strip(),
    )


def split(
    sheet: CueSheet,
    out_dir: str,
    audio_hint: Optional[str] = None,
    copy_cover: bool = True,
    verify: bool = False,
    on_progress=None,
) -> List[str]:
    """按 CUE 把整轨切分为独立 FLAC，返回输出文件路径列表。"""
    ff = resolve_tool("ffmpeg")
    ffprobe = resolve_tool("ffprobe")
    os.makedirs(out_dir, exist_ok=True)
    sample_fmt = ""
    probed = False
    total = sheet.total_tracks
    results: List[str] = []

    # DSD 源码检测：用于提示 + 走「低通 + 降采样」转换路径
    is_dsd = bool(sheet.tracks) and _is_dsd(sheet.tracks[0].file, ffprobe)
    dsd_args: List[str] = []
    dsd_note_text = ""
    if is_dsd:
        # DSD 为 1-bit 编码，其“位深”不能当作 PCM 位深。
        #
        # 关键：DSD 源的解码采样率高达 352.8kHz（DSD64），**FLAC 编码器
        # 不接受该采样率**（实测报 "2822400 Hz not supported"）。
        # 必须像直转链路一样做「低通 + 抽取降采样」，两条链路行为保持一致。
        sample_fmt = ""
        target_sr, cutoff, filt, dsd_note_text = dsd_plan(
            sheet.tracks[0].file, ffprobe
        )
        dsd_args = dsd_args_from_plan(target_sr, filt)
        if on_progress:
            on_progress(
                "注意：检测到 DSD 源码（.dsf/.dff）。DSD 是 1-bit 编码，转 FLAC（PCM）"
                f"属于高质量转换，并非逐样本无损；已低通滤波（截止 {cutoff} Hz）"
                f"并降采样至 {target_sr} Hz / 32-bit FLAC。\n"
            )

    if on_progress:
        on_progress(f"开始分轨：共 {total} 首，输出到 {out_dir}\n")

    for idx, tr in enumerate(sheet.tracks, 1):
        if not tr.file or not os.path.isfile(tr.file):
            hint = _missing_audio_hint(tr.file or "")
            raise FileNotFoundError(
                f"音轨 {tr.number} 的音频文件不存在或未找到：{tr.file}\n"
                + (f"{hint}\n" if hint else "")
                + f"请用 --audio 指定正确的整轨音频文件。"
            )
        if not is_dsd and not probed:
            # 位深只探测一次；映射规则与直转链路共用 _util.sample_fmt_for_bits。
            # strict=True 保留分轨原有语义：未知位深时返回空串 = 不传 -sample_fmt。
            probed = True
            params = probe_audio(tr.file, ffprobe) or {}
            sample_fmt = sample_fmt_for_bits(
                int(params.get("bits") or 0), strict=True
            )

        out_name = _safe_name(tr) + ".flac"
        out_path = os.path.join(out_dir, out_name)

        cmd = [
            ff,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{tr.start:.6f}",
        ]
        if tr.end is not None:
            cmd += ["-to", f"{tr.end:.6f}"]
        cmd += ["-i", tr.file]
        cmd += ["-map", "0:a:0?"]
        if copy_cover:
            cmd += ["-map", "0:v:0?"]
            cmd += ["-c:v", "copy"]
        if is_dsd:
            # DSD：低通 + 抽取降采样（与直转链路完全一致）
            cmd += dsd_args
        else:
            cmd += ["-c:a", "flac"]
            if sample_fmt:
                cmd += ["-sample_fmt", sample_fmt]

        if tr.title:
            cmd += ["-metadata", f"title={tr.title}"]
        artist = tr.performer or sheet.album_performer
        if artist:
            cmd += ["-metadata", f"artist={artist}"]
        if sheet.album_title:
            cmd += ["-metadata", f"album={sheet.album_title}"]
        if sheet.genre:
            cmd += ["-metadata", f"genre={sheet.genre}"]
        if sheet.date:
            cmd += ["-metadata", f"date={sheet.date}"]
        cmd += ["-metadata", f"track={tr.number}/{total}"]
        cmd += [out_path]

        if on_progress:
            on_progress(f"[{idx}/{total}] {out_name}\n")
        _run_ffmpeg(cmd, on_progress)
        results.append(out_path)

    if verify:
        if on_progress:
            on_progress(
                "开始输出验证：检查可播放性、音频参数匹配与无损一致性…\n"
            )
        report = verify_outputs(
            sheet, results, ff, ffprobe, on_progress, dsd_note=dsd_note_text
        )
        if on_progress:
            on_progress(format_verification_report(report) + "\n")
        if not report.all_ok:
            failed = [os.path.basename(r.out_path) for r in report.results if not r.ok]
            raise SplitterError(
                "输出验证未通过（文件可能无法播放、参数不匹配或音质非无损）：\n"
                + "\n".join(failed)
            )

    if on_progress:
        on_progress(f"完成：已生成 {len(results)} 个文件。\n")
    return results
