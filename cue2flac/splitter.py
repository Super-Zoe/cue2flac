"""分轨核心：调用 ffmpeg 把整轨音频按 CUE 切分为独立 FLAC。

无损说明：FLAC 是无损压缩，对 PCM 样本做有损重编码是不可能的；
因此「解码整轨 → 用 ffmpeg 的 FLAC 编码器重新封装」得到的是逐样本一致的 PCM，
与直接从源文件取对应区段完全一致（可由 --verify 校验）。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from typing import List, Optional

from .cue_parser import CueSheet, Track
from .ffmpeg_provider import resolve_tool


class SplitterError(RuntimeError):
    """分轨过程中的可预期错误。"""


def _detect_sample_fmt(audio_path: str, ffprobe: str) -> str:
    """探测源文件位深，返回 ffmpeg 的 -sample_fmt 取值（未知则返回空串）。"""
    try:
        out = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=bits_per_raw_sample",
                "-of",
                "default=nw=1:nk=1",
                audio_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                bits = int(line)
                if bits in (8, 16, 24, 32):
                    return f"s{bits}"
    except Exception:
        pass
    return ""


def _safe_name(tr: Track, total: int) -> str:
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
    if on_progress:
        on_progress("  $ " + " ".join(cmd) + "\n")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        raise SplitterError(f"ffmpeg 执行失败（退出码 {proc.returncode}）：\n{err[:2000]}")
    return proc


def _md5_pcm(path: str, ff: str, start: float, end: Optional[float]) -> str:
    """把音频区段解码为 s24le 裸 PCM 并求 MD5（用于无损校验）。"""
    args = [ff, "-nostdin", "-v", "error"]
    if start:
        args += ["-ss", f"{start:.6f}"]
    if end is not None:
        args += ["-to", f"{end:.6f}"]
    args += ["-i", path, "-map", "0:a:0?", "-f", "s24le", "-c:a", "pcm_s24le", "-"]
    proc = subprocess.run(args, capture_output=True)
    if proc.returncode != 0:
        return ""
    return hashlib.md5(proc.stdout).hexdigest()


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
    total = sheet.total_tracks
    results: List[str] = []

    if on_progress:
        on_progress(f"开始分轨：共 {total} 首，输出到 {out_dir}\n")

    for idx, tr in enumerate(sheet.tracks, 1):
        if not tr.file or not os.path.isfile(tr.file):
            raise FileNotFoundError(
                f"音轨 {tr.number} 的音频文件不存在或未找到：{tr.file}\n"
                f"请用 --audio 指定正确的整轨音频文件。"
            )
        if not sample_fmt:
            sample_fmt = _detect_sample_fmt(tr.file, ffprobe)

        out_name = _safe_name(tr, total) + ".flac"
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
            src_md5 = _md5_pcm(tr.file, ff, tr.start, tr.end)
            out_md5 = _md5_pcm(out_path, ff, 0.0, None)
            if src_md5 and out_md5 and src_md5 != out_md5:
                raise SplitterError(
                    f"校验失败：音轨 {tr.number} 的 PCM 与源不一致（可能源已损坏或非无损）。"
                )
            if on_progress and src_md5:
                on_progress(f"     PCM 校验 OK (md5={src_md5[:12]}…)\n")

    if on_progress:
        on_progress(f"完成：已生成 {len(results)} 个文件。\n")
    return results
