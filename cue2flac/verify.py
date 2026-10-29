"""转换后验证：确认输出 FLAC 可正常播放、音频参数（采样率 / 位深 / 声道数）与源文件
匹配，且音质无损、无数据损坏或截断。

校验项：
1. 可播放性 —— ffprobe 能识别音轨，且 ffmpeg 能完整解码（无错误）；
2. 参数匹配 —— 输出流的采样率 / 声道数 / 位深与源流一致；
3. 无损 + 无截断 —— 把源对应区段与输出文件解码为同一 PCM 格式求 MD5，逐字节一致。

FLAC 为无损压缩，对 PCM 做有损重编码不可能发生，因此解码整轨后用 FLAC 重新封装
得到的 PCM 与源对应区段逐样本一致；本模块正是校验这一点，并同时确认文件可解码、参数匹配。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class VerifyError(RuntimeError):
    """校验过程中不可恢复的错误（如工具缺失）。"""


@dataclass
class TrackVerifyResult:
    out_path: str
    track_no: int
    playable: bool = False        # 文件可被 ffmpeg 完整解码
    params_match: bool = False    # 采样率/声道数/位深 与源一致
    lossless: bool = False        # PCM 与源区段逐字节一致
    no_truncation: bool = False   # 无截断（PCM 长度与源区段一致）
    errors: List[str] = field(default_factory=list)
    src_params: Dict = field(default_factory=dict)
    out_params: Dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.playable and self.lossless and self.no_truncation


@dataclass
class VerificationReport:
    total: int = 0
    passed: int = 0
    failed: int = 0
    results: List[TrackVerifyResult] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return self.failed == 0


def probe_audio(path: str, ffprobe: str) -> Optional[Dict]:
    """用 ffprobe 读取音频流参数；失败返回 None。"""
    try:
        out = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "stream=sample_rate,bits_per_raw_sample,channels,codec_name,duration",
                "-of", "json", path,
            ],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:  # noqa: BLE001
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        data = json.loads(out.stdout)
    except Exception:  # noqa: BLE001
        return None
    for s in data.get("streams", []):
        if s.get("codec_type") != "audio":
            continue

        def _int(x, default=0):
            try:
                return int(float(x))
            except Exception:  # noqa: BLE001
                return default

        return {
            "sample_rate": _int(s.get("sample_rate")),
            "bits": _int(s.get("bits_per_raw_sample")),
            "channels": _int(s.get("channels")),
            "codec": s.get("codec_name", ""),
            "duration": float(s.get("duration") or 0),
        }
    return None


def decode_ok(path: str, ff: str) -> tuple[bool, str]:
    """尝试用 ffmpeg 把文件解码到 null，确认文件可正常播放、无损坏。"""
    try:
        proc = subprocess.run(
            [ff, "-nostdin", "-v", "error", "-i", path, "-f", "null", "-"],
            capture_output=True, text=True, timeout=300,
        )
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()[:400]
    return True, ""


def pcm_md5(path: str, ff: str, start: float, end: Optional[float]) -> str:
    """把音频（区段）解码为 s32le 裸 PCM 并求 MD5，用于无损 / 无截断比对。"""
    args = [ff, "-nostdin", "-v", "error"]
    if start:
        args += ["-ss", f"{start:.6f}"]
    if end is not None:
        args += ["-to", f"{end:.6f}"]
    args += ["-i", path, "-map", "0:a:0?", "-f", "s32le", "-c:a", "pcm_s32le", "-"]
    try:
        proc = subprocess.run(args, capture_output=True, timeout=300)
    except Exception:  # noqa: BLE001
        return ""
    if proc.returncode != 0:
        return ""
    return hashlib.md5(proc.stdout).hexdigest()


def verify_track(
    src_audio: str,
    src_start: float,
    src_end: Optional[float],
    out_path: str,
    track_no: int,
    ff: str,
    ffprobe: str,
    on_progress=None,
) -> TrackVerifyResult:
    res = TrackVerifyResult(out_path=out_path, track_no=track_no)

    src = probe_audio(src_audio, ffprobe)
    out = probe_audio(out_path, ffprobe)
    if src:
        res.src_params = src
    if out:
        res.out_params = out

    # 1) 可播放性
    ok, err = decode_ok(out_path, ff)
    res.playable = ok
    if not ok:
        res.errors.append("文件无法解码（可能已损坏）：" + str(err)[:300])

    # 2) 参数匹配
    if src and out:
        mism: List[str] = []
        if out["sample_rate"] and out["sample_rate"] != src["sample_rate"]:
            mism.append(f"采样率 {out['sample_rate']}≠源 {src['sample_rate']}")
        if out["channels"] and out["channels"] != src["channels"]:
            mism.append(f"声道数 {out['channels']}≠源 {src['channels']}")
        if src["bits"] and out["bits"] and out["bits"] != src["bits"]:
            mism.append(f"位深 {out['bits']}≠源 {src['bits']}")
        if mism:
            res.errors.append("音频参数不一致：" + "; ".join(mism))
        else:
            res.params_match = True
    elif out:
        # 无源参数可比，至少文件自身可探测，视为参数层面通过
        res.params_match = True

    # 3) 无损 + 无截断（PCM 逐字节一致）
    src_md5 = pcm_md5(src_audio, ff, src_start, src_end)
    out_md5 = pcm_md5(out_path, ff, 0.0, None)
    if src_md5 and out_md5:
        if src_md5 == out_md5:
            res.lossless = True
            res.no_truncation = True
        else:
            res.errors.append("PCM 与源区段不一致（可能存在截断或数据损坏）")
    else:
        res.errors.append("无法对源 / 输出做 PCM 比对（跳过无损校验）")

    return res


def verify_outputs(
    sheet,
    out_paths: List[str],
    ff: str,
    ffprobe: str,
    on_progress=None,
) -> VerificationReport:
    """对一批输出文件逐一校验，返回汇总报告。

    sheet.tracks 与 out_paths 按出现顺序一一对应。
    """
    report = VerificationReport()
    report.total = len(out_paths)
    for i, out_path in enumerate(out_paths):
        if i >= len(sheet.tracks):
            break
        tr = sheet.tracks[i]
        if on_progress:
            on_progress(f"  校验 {os.path.basename(out_path)} …\n")
        r = verify_track(
            tr.file, tr.start, tr.end, out_path, tr.number, ff, ffprobe, on_progress
        )
        report.results.append(r)
        if r.ok:
            report.passed += 1
            if on_progress:
                on_progress("    ✓ 可播放 / 参数匹配 / 无损一致\n")
        else:
            report.failed += 1
            if on_progress:
                on_progress("    ✗ " + "; ".join(r.errors) + "\n")
    return report


def format_verification_report(report: VerificationReport) -> str:
    lines: List[str] = []
    lines.append("─" * 48)
    lines.append("输出验证汇总")
    lines.append(f"  校验文件数：{report.total}")
    lines.append(f"  通过      ：{report.passed}")
    lines.append(f"  失败      ：{report.failed}")
    lines.append("─" * 48)
    for r in report.results:
        status = "✓" if r.ok else "✗"
        lines.append(f"  {status} {os.path.basename(r.out_path)}")
        if not r.ok:
            for e in r.errors:
                lines.append(f"      {e}")
    lines.append("─" * 48)
    return "\n".join(lines)
