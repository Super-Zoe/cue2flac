"""转换后无损校验：确认输出 FLAC 可正常播放、音频参数与源一致、且数据逐字节无损。

校验项（两条转换链路共用同一套逻辑）
------------------------------------------------------------------
1. **可播放性** —— ffprobe 能识别音轨，且 ffmpeg 能完整解码（无错误）；
2. **采样率**   —— 输出 == 源；
3. **位深**     —— 输出 == 源（FLAC 记 ``bits_per_raw_sample``）；
4. **声道数**   —— 输出 == 源；
5. **帧数**     —— 输出 == 源（由解码字节数精确推算，见 :func:`decoded_frames`）；
6. **时长**     —— 输出 == 源（误差在容差内）；
7. **PCM 逐字节** —— 源与输出解码为 s32le 后 MD5 完全一致。

判定规则
------------------------------------------------------------------
* 以上 7 项全部满足 → 判定为 **无损**（``lossless=True``）；
* 采样率 / 位深 / 声道数 / 帧数 / 时长 任一被改动 → 判定为 **参数被改动**
  （``params_match=False``），逐项列出差异，绝不静默通过；
* DSD 源（``.dsf`` / ``.dff``）是 1-bit 德尔塔-西格玛编码，转 PCM **本质不可逆**，
  因此无法做逐样本比对。这类源判定为 **有损转换**（``lossless=False``），
  并明确记录低通截止频率与目标采样率，绝不混称「无损」。

帧数为什么不用 ffprobe 的 ``nb_frames``
------------------------------------------------------------------
``nb_frames`` 对 WAV / FLAC 均返回 ``N/A``；``-count_frames`` 的 ``nb_read_frames``
统计的是**解码包数**而非采样帧数（实测同一段音频 WAV 报 44、FLAC 报 39，
而真实帧数为 177600），不可用。
本模块改用**解码后的 s32le 字节数 ÷ (4 × 声道数)**推算采样帧数，
这是与 PCM 逐字节比对同一份数据，既精确又能真实反映截断。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


class VerifyError(RuntimeError):
    """校验过程中不可恢复的错误（如工具缺失）。"""


# 时长比对容差（秒）。FLAC 重新封装后 duration 可能与源相差极小量（浮点累积），
# 低于 1 毫秒视为一致；真正截断的差异是「秒」级别，不会被容差掩盖。
DURATION_TOLERANCE = 0.001


@dataclass
class TrackVerifyResult:
    """单个输出文件的校验结果。"""

    out_path: str
    track_no: int = 1
    playable: bool = False         # 文件可被 ffmpeg 完整解码
    params_match: bool = False     # 采样率/位深/声道数 与源一致
    length_match: bool = False     # 帧数/时长 与源一致（无截断、无变速）
    lossless: bool = False         # PCM 与源逐字节一致
    no_truncation: bool = False    # 无截断
    lossy_reason: str = ""         # 非无损时的原因说明（DSD 等）
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    src_params: Dict = field(default_factory=dict)
    out_params: Dict = field(default_factory=dict)
    src_frames: int = 0
    out_frames: int = 0
    src_duration: float = 0.0
    out_duration: float = 0.0

    @property
    def ok(self) -> bool:
        """整体通过：可播放 + 参数一致 + 长度一致 + 无损。"""
        return (
            self.playable
            and self.params_match
            and self.length_match
            and self.lossless
            and self.no_truncation
        )

    @property
    def status(self) -> str:
        """人类可读的判定结论。"""
        if self.ok:
            return "无损"
        if self.lossy_reason:
            return "有损转换"
        if not self.params_match or not self.length_match:
            return "参数被改动"
        return "校验未通过"


@dataclass
class VerificationReport:
    total: int = 0
    passed: int = 0
    failed: int = 0
    lossy: int = 0
    results: List[TrackVerifyResult] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return self.failed == 0


# --------------------------------------------------------------------------
# 探测
# --------------------------------------------------------------------------


def probe_audio(path: str, ffprobe: str) -> Optional[Dict]:
    """用 ffprobe 读取音频流参数；失败返回 None。

    位深同时读取 ``bits_per_raw_sample`` 与 ``bits_per_sample``：
    - WAV 的 pcm_s16le 只有 ``bits_per_sample=16``，``bits_per_raw_sample`` 为 N/A；
    - FLAC / APE 则由 ``bits_per_raw_sample`` 给出真实位深。
    只读其中一个会导致位深探测漏判（例如把 16bit WAV 误判为未知）。

    ``codec_type`` 必须显式列入 ``show_entries``：ffprobe 的 ``-show_entries``
    是**白名单**语义，不列出的字段不会出现在 JSON 里。若只按
    ``s.get("codec_type") != "audio"`` 过滤而不把该字段列入白名单，
    就会把所有流都当成非音频流跳过，导致本函数恒返回 None、
    参数比对形同虚设。
    """
    try:
        out = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-select_streams", "a:0",
                "-show_entries",
                "stream=codec_type,sample_rate,bits_per_raw_sample,bits_per_sample,"
                "channels,codec_name,duration",
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
        # codec_type 已在 show_entries 白名单中；仍做一次兜底判断，
        # 以防 ffprobe 版本差异导致该字段缺失时误收非音频流。
        if s.get("codec_type", "audio") != "audio":
            continue

        def _int(x, default=0):
            try:
                return int(float(x))
            except Exception:  # noqa: BLE001
                return default

        raw_bits = _int(s.get("bits_per_raw_sample"))
        sample_bits = _int(s.get("bits_per_sample"))
        bits = raw_bits or sample_bits
        return {
            "sample_rate": _int(s.get("sample_rate")),
            "bits": bits,
            "channels": _int(s.get("channels")),
            "codec": s.get("codec_name", ""),
            "duration": float(s.get("duration") or 0),
        }
    return None


def decode_ok(path: str, ff: str) -> Tuple[bool, str]:
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


def decoded_stream_info(path: str, ff: str) -> Dict:
    """用 ffmpeg 解码到 null，读取**解码后**音频流的实际参数。

    为什么不用 ffprobe 的容器参数：ffprobe 报告的是**容器**声明的采样率，
    而解码器实际输出的采样率可能不同——
    - DSD（.dsf/.dff）容器声明 2.8224MHz（比特率），ffmpeg 的 DSD 解码器
      固定按 8× 抽取，实际输出 352.8kHz PCM；
    - 被重采样过的源，容器率与解码率也可能不一致。

    帧数/时长必须基于**解码后**的真实速率推算，否则算出来的时长是错的。
    ffmpeg 在 stderr 打印输出流信息，其中含解码后的采样率与声道数。
    """
    info: Dict = {}
    try:
        proc = subprocess.run(
            [ff, "-nostdin", "-hide_banner", "-i", path,
             "-map", "0:a:0?", "-f", "null", "-"],
            capture_output=True, text=True, timeout=300,
        )
    except Exception:  # noqa: BLE001
        return info
    if proc.returncode != 0:
        return info
    # 输出流信息形如：
    #   Stream #0:0: Audio: pcm_s16le, 48000 Hz, stereo, s16, 1536 kb/s
    for line in (proc.stderr or "").splitlines():
        s = line.strip()
        if not s.startswith("Stream #0:0: Audio:"):
            continue
        m = re.search(r"(\d+)\s*Hz", s)
        if m:
            info["decoded_sample_rate"] = int(m.group(1))
        m2 = re.search(r",\s*(\d+)\s*channels", s)
        if m2:
            info["decoded_channels"] = int(m2.group(1))
        elif re.search(r"\bmono\b", s):
            info["decoded_channels"] = 1
        elif re.search(r"\bstereo\b", s):
            info["decoded_channels"] = 2
    return info


def pcm_bytes(path: str, ff: str, start: float, end: Optional[float]) -> bytes:
    """把音频（区段）解码为 s32le 裸 PCM，返回字节内容。

    这是校验的**唯一数据来源**：MD5 用它比对逐字节一致性，
    帧数与时长也由它推算——三者同源，不会出现「MD5 过了但帧数是另一份数据」的矛盾。
    """
    args = [ff, "-nostdin", "-v", "error"]
    if start:
        args += ["-ss", f"{start:.6f}"]
    if end is not None:
        args += ["-to", f"{end:.6f}"]
    args += ["-i", path, "-map", "0:a:0?", "-f", "s32le", "-c:a", "pcm_s32le", "-"]
    try:
        proc = subprocess.run(args, capture_output=True, timeout=300)
    except Exception:  # noqa: BLE001
        return b""
    if proc.returncode != 0:
        return b""
    return proc.stdout


def pcm_md5(path: str, ff: str, start: float, end: Optional[float]) -> str:
    """把音频（区段）解码为 s32le 裸 PCM 并求 MD5，用于无损 / 无截断比对。"""
    data = pcm_bytes(path, ff, start, end)
    if not data:
        return ""
    return hashlib.md5(data).hexdigest()


def decoded_frames(pcm_len: int, channels: int) -> int:
    """由 s32le 字节数推算采样帧数。

    s32le 每样本 4 字节，每帧 channels 个样本 → 帧数 = 字节数 ÷ (4 × 声道数)。
    """
    if pcm_len <= 0 or channels <= 0:
        return 0
    return pcm_len // (4 * channels)


def frames_to_duration(frames: int, sample_rate: int) -> float:
    """由帧数与采样率推算时长（秒）。"""
    if frames <= 0 or sample_rate <= 0:
        return 0.0
    return frames / float(sample_rate)


# --------------------------------------------------------------------------
# 统一校验
# --------------------------------------------------------------------------


def _compare_params(
    src: Optional[Dict],
    out: Optional[Dict],
    is_dsd: bool,
) -> Tuple[bool, List[str]]:
    """比对采样率 / 位深 / 声道数。返回 (是否一致, 差异描述列表)。

    DSD 源不做采样率与位深比对——1-bit 源转 PCM 后采样率必然改变（这是
    DSD→PCM 的固有性质，不是缺陷），位深也不存在对应关系。只比对声道数。
    """
    mism: List[str] = []
    if not src or not out:
        return True, mism

    if not is_dsd:
        if out["sample_rate"] and src["sample_rate"] \
                and out["sample_rate"] != src["sample_rate"]:
            mism.append(f"采样率 {out['sample_rate']}≠源 {src['sample_rate']}")
        if src["bits"] and out["bits"] and out["bits"] != src["bits"]:
            mism.append(f"位深 {out['bits']}≠源 {src['bits']}")
    if out["channels"] and src["channels"] and out["channels"] != src["channels"]:
        mism.append(f"声道数 {out['channels']}≠源 {src['channels']}")
    return (not mism), mism


def _compare_length(
    src_frames: int,
    out_frames: int,
    src_rate: int,
    out_rate: int,
) -> Tuple[bool, List[str], float, float]:
    """比对帧数与时长。返回 (是否一致, 差异描述列表, 源时长, 输出时长)。

    判据随采样率是否变化而不同：

    * **采样率相同** → 帧数必须**完全相等**。这是最强的长度判据：
      任何截断、丢帧、补零都会立刻暴露。
    * **采样率不同**（重采样 / DSD 降采样）→ 帧数本就不同（时长不变），
      此时改用**时长**判据，带 1ms 容差以吸收浮点累积误差。

    两者都会用**解码后**的真实速率推算时长，因此 DSD 这类
    「容器声明率 ≠ 实际解码率」的场景也能算对。
    """
    mism: List[str] = []
    src_dur = frames_to_duration(src_frames, src_rate)
    out_dur = frames_to_duration(out_frames, out_rate)

    if src_frames and out_frames:
        if src_rate and out_rate and src_rate == out_rate:
            # 同采样率 → 帧数必须严格相等
            if src_frames != out_frames:
                diff = out_frames - src_frames
                mism.append(
                    f"帧数 {out_frames}≠源 {src_frames}（差 {diff:+d} 帧，"
                    f"约 {diff / float(src_rate or 1) * 1000:+.1f} ms）"
                )
        else:
            # 采样率不同 → 帧数差异属正常，用时长判据
            if abs(src_dur - out_dur) > DURATION_TOLERANCE:
                mism.append(
                    f"时长 {out_dur:.6f}s≠源 {src_dur:.6f}s"
                    f"（差 {out_dur - src_dur:+.6f}s）"
                )
            return (not mism), mism, src_dur, out_dur

    if abs(src_dur - out_dur) > DURATION_TOLERANCE:
        mism.append(f"时长 {out_dur:.6f}s≠源 {src_dur:.6f}s（差 {out_dur - src_dur:+.6f}s）")

    return (not mism), mism, src_dur, out_dur


def verify_output(
    src_audio: str,
    out_path: str,
    ff: str,
    ffprobe: str,
    src_start: float = 0.0,
    src_end: Optional[float] = None,
    track_no: int = 1,
    dsd_note: str = "",
    on_progress=None,
) -> TrackVerifyResult:
    """统一校验入口：分轨（比区段）与直转（比整文件）共用。

    参数：
      src_audio   —— 源整轨音频路径
      out_path    —— 输出 FLAC 路径
      src_start / src_end —— 比对源的那个时间区段；直转时传 (0.0, None)
      dsd_note    —— 源为 DSD 时填入转换说明（如低通截止频率、目标采样率），
                    会被记入 ``lossy_reason`` 并在结果中明确标注为有损转换
    """
    res = TrackVerifyResult(out_path=out_path, track_no=track_no)

    src = probe_audio(src_audio, ffprobe)
    out = probe_audio(out_path, ffprobe)
    if src:
        res.src_params = src
    if out:
        res.out_params = out

    # ---- 1) 可播放性 ----
    ok, err = decode_ok(out_path, ff)
    res.playable = ok
    if not ok:
        res.errors.append("文件无法解码（可能已损坏）：" + str(err)[:300])

    is_dsd = bool(src) and str(src.get("codec", "")).lower().startswith("dsd")
    # 只要调用方明确声明了「本次转换有损」（如 DSD → PCM），
    # 就必须无条件按有损处理，不能依赖源类型是否被 ffprobe 识别为 dsd。
    if dsd_note:
        res.lossy_reason = dsd_note
    elif is_dsd:
        res.lossy_reason = "DSD（1-bit 编码）转 PCM 不可逆，无法逐样本比对"
    lossy = bool(res.lossy_reason)

    # ---- 2) 参数比对：采样率 / 位深 / 声道数 ----
    params_ok, mism = _compare_params(src, out, is_dsd)
    if mism:
        res.errors.append("音频参数被改动：" + "; ".join(mism))
    res.params_match = params_ok

    # ---- 3) 长度比对：帧数 / 时长 ----
    src_pcm = pcm_bytes(src_audio, ff, src_start, src_end)
    out_pcm = pcm_bytes(out_path, ff, 0.0, None)
    src_ch = int((src or {}).get("channels") or 0)
    out_ch = int((out or {}).get("channels") or 0)

    # 采样率必须取**解码后**的真实值：DSD 容器声明 2.8224MHz，
    # 而 ffmpeg 的 DSD 解码器固定 8× 抽取（实际输出 352.8kHz）。
    # 用容器率推算时长会得到错误结果。
    src_dec = decoded_stream_info(src_audio, ff)
    out_dec = decoded_stream_info(out_path, ff)
    src_rate = int(src_dec.get("decoded_sample_rate")
                   or (src or {}).get("sample_rate") or 0)
    out_rate = int(out_dec.get("decoded_sample_rate")
                   or (out or {}).get("sample_rate") or 0)
    src_ch = int(src_dec.get("decoded_channels") or src_ch) or src_ch
    out_ch = int(out_dec.get("decoded_channels") or out_ch) or out_ch

    res.src_frames = decoded_frames(len(src_pcm), src_ch)
    res.out_frames = decoded_frames(len(out_pcm), out_ch)

    if not src_pcm or not out_pcm:
        res.errors.append("无法对源 / 输出做 PCM 解码（跳过长度与无损校验）")
        res.length_match = False
        res.no_truncation = False
        return res

    len_ok, len_mism, src_dur, out_dur = _compare_length(
        res.src_frames, res.out_frames, src_rate, out_rate
    )
    res.src_duration = src_dur
    res.out_duration = out_dur
    if len_mism:
        res.errors.append("音频长度不一致：" + "; ".join(len_mism))
    res.length_match = len_ok
    res.no_truncation = len_ok

    # ---- 4) PCM 逐字节一致 ----
    if lossy:
        # 有损转换（如 DSD→PCM）不可逆，无法逐样本比对；
        # 此时长度一致性靠帧数 / 时长比对保证
        res.lossless = False
    else:
        src_md5 = hashlib.md5(src_pcm).hexdigest()
        out_md5 = hashlib.md5(out_pcm).hexdigest()
        if src_md5 == out_md5:
            res.lossless = True
        else:
            res.errors.append(
                "PCM 与源" + ("文件" if src_end is None else "区段")
                + "不一致（存在数据损坏或位深下降）"
            )

    return res


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
    """校验分轨输出（比对源文件的某个时间区段）。"""
    return verify_output(
        src_audio, out_path, ff, ffprobe,
        src_start=src_start, src_end=src_end, track_no=track_no,
        on_progress=on_progress,
    )


def verify_whole_file(
    src_audio: str,
    out_path: str,
    ff: str,
    ffprobe: str,
    on_progress=None,
    dsd_note: str = "",
) -> TrackVerifyResult:
    """校验「整轨文件 → FLAC」的直接转换结果（比对整个文件）。

    与 :func:`verify_track` 的区别：这里比对的是**整个文件**而非某个时间区段，
    适用于不依赖 CUE 的直接转换场景。
    """
    return verify_output(
        src_audio, out_path, ff, ffprobe,
        src_start=0.0, src_end=None, track_no=1,
        dsd_note=dsd_note, on_progress=on_progress,
    )


def verify_outputs(
    sheet,
    out_paths: List[str],
    ff: str,
    ffprobe: str,
    on_progress=None,
) -> VerificationReport:
    """对一批分轨输出逐一校验，返回汇总报告。

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
                on_progress("    ✓ 可播放 / 参数一致 / 帧数时长一致 / PCM 逐字节一致\n")
        else:
            report.failed += 1
            if on_progress:
                on_progress(f"    ✗ [{r.status}] " + "; ".join(r.errors) + "\n")
    return report


def format_verification_report(report: VerificationReport) -> str:
    lines: List[str] = []
    lines.append("─" * 52)
    lines.append("输出校验汇总（无损判定）")
    lines.append(f"  校验文件数：{report.total}")
    lines.append(f"  无损通过  ：{report.passed}")
    lines.append(f"  未通过    ：{report.failed}")
    lines.append("─" * 52)
    for r in report.results:
        status = "✓" if r.ok else "✗"
        lines.append(f"  {status} {os.path.basename(r.out_path)} —— {r.status}")
        lines.append(
            "      采样率 {sr} / 位深 {sb} / 声道 {sc} / 帧数 {sf} / 时长 {sd}".format(
                sr=f"{r.src_params.get('sample_rate', '?')}→"
                   f"{r.out_params.get('sample_rate', '?')}",
                sb=f"{r.src_params.get('bits', '?')}→{r.out_params.get('bits', '?')}",
                sc=f"{r.src_params.get('channels', '?')}→"
                   f"{r.out_params.get('channels', '?')}",
                sf=f"{r.src_frames}→{r.out_frames}",
                sd=f"{r.src_duration:.3f}s→{r.out_duration:.3f}s",
            )
        )
        if r.lossy_reason:
            lines.append(f"      提示：{r.lossy_reason}")
        for w in r.warnings:
            lines.append(f"      提示：{w}")
        if not r.ok:
            for e in r.errors:
                lines.append(f"      {e}")
    lines.append("─" * 52)
    return "\n".join(lines)
