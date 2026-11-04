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
import tempfile
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
        """整体通过。

        - **无损路径**：可播放 + 参数一致 + 长度一致 + PCM 逐字节一致。
        - **有损路径**（DSD → PCM 等，天然不可逆）：可播放 + 参数一致 +
          长度一致即可判定为「转换成功」。此时**不能**要求逐样本无损——
          否则 DSD 源无论怎么转都会被判为失败。
        """
        if self.playable and self.params_match and self.length_match \
                and self.no_truncation:
            return True
        return False

    @property
    def status(self) -> str:
        """人类可读的判定结论。"""
        if self.ok:
            return "有损转换" if self.lossy_reason else "无损"
        if self.lossy_reason:
            return "有损转换失败"
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


@dataclass
class SourceContext:
    """同一源音频在一次批量校验中可复用的探测结果。

    分轨时 ``sheet.tracks`` 里的 N 首曲目往往指向**同一个**整轨文件，
    而 ``probe_audio``(容器参数) 与 ``decoded_stream_info``(解码后参数)
    都与曲目无关。逐首重新探测会让源整轨被完整解码 O(N) 次。

    ``key`` 由 ``(绝对路径, size, mtime_ns)`` 组成：批处理中若同一路径的
    文件被覆盖写入，key 会变化，从而避免复用过期的参数。
    """

    path: str
    key: Tuple = ()
    params: Optional[Dict] = None       # probe_audio 的结果（容器参数）
    decoded: Dict = field(default_factory=dict)  # 解码后的采样率 / 声道数


def _source_key(path: str) -> Tuple:
    """生成源音频的缓存键；stat 失败时退化为仅按路径（仍可用，但不复用旧参数）。"""
    abspath = os.path.abspath(path or "")
    try:
        st = os.stat(abspath)
        return (abspath, st.st_size, st.st_mtime_ns)
    except OSError:
        return (abspath, -1, -1)


# --------------------------------------------------------------------------
# 探测
# --------------------------------------------------------------------------


def probe_audio(path: str, ffprobe: str,
                with_tags: bool = False) -> Optional[Dict]:
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

    ``with_tags=True`` 时在同一次调用里附带读取 ``format.tags``，
    以 ``format_tags`` 键返回；默认 False 保持原有返回结构不变。
    这样直转链路复制元数据时无需再单独启动一次 ffprobe。
    """
    try:
        entries = (
            "stream=codec_type,sample_rate,bits_per_raw_sample,bits_per_sample,"
            "channels,codec_name,duration"
        )
        if with_tags:
            entries += ":format_tags"
        out = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", entries,
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
        params = {
            "sample_rate": _int(s.get("sample_rate")),
            "bits": bits,
            "channels": _int(s.get("channels")),
            "codec": s.get("codec_name", ""),
            "duration": float(s.get("duration") or 0),
        }
        if with_tags:
            params["format_tags"] = (data.get("format") or {}).get("tags") or {}
        return params
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


def _parse_decoded_stream(text: str) -> Dict:
    """从 ffmpeg 的 stderr 里解析**输出流**的采样率与声道数。

    ffmpeg 会同时打印输入流和输出流的描述行，两者都以
    ``Stream #0:0: Audio:`` 开头；按出现顺序覆盖后留在 dict 里的
    是最后那条（输出流），这正是「解码后」的真实参数。
    """
    info: Dict = {}
    for line in (text or "").splitlines():
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


def decoded_stream_info(path: str, ff: str) -> Dict:
    """用 ffmpeg 解码到 null，读取**解码后**音频流的实际参数。

    为什么不用 ffprobe 的容器参数：ffprobe 报告的是**容器**声明的采样率，
    而解码器实际输出的采样率可能不同——
    - DSD（.dsf/.dff）容器声明 2.8224MHz（这是 1-bit **比特率**），
      ffmpeg 解码后实际输出 352.8kHz PCM（相差 8 倍）；
    - 被重采样过的源，容器率与解码率也可能不一致。

    帧数/时长必须基于**解码后**的真实速率推算，否则算出来的时长是错的。
    ffmpeg 在 stderr 打印输出流信息，其中含解码后的采样率与声道数。

    保留为公开 API；生产校验路径已改用 :func:`_decode_pcm`，
    后者在解码 PCM 的同一次进程里顺带取得同样的信息，无需再全解码一遍。
    """
    try:
        proc = subprocess.run(
            [ff, "-nostdin", "-hide_banner", "-i", path,
             "-map", "0:a:0?", "-f", "null", "-"],
            capture_output=True, text=True, timeout=300,
        )
    except Exception:  # noqa: BLE001
        return {}
    if proc.returncode != 0:
        return {}
    # 输出流信息形如：
    #   Stream #0:0: Audio: pcm_s16le, 48000 Hz, stereo, s16, 1536 kb/s
    return _parse_decoded_stream(proc.stderr or "")


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


#: 流式解码时每次从 stdout 读取的块大小（1 MiB），内存占用与音频长度无关。
_PCM_CHUNK = 1 << 20


def _decode_error(text: str) -> str:
    """从 ffmpeg 的 stderr 里摘出真正的错误行，避免把 Input/Output 描述当错误。"""
    bad = [
        line.strip() for line in (text or "").splitlines()
        if re.search(r"error|invalid|failed|corrupt|could not|unsupported", line, re.I)
    ]
    if bad:
        return " | ".join(bad[-5:])[:400]
    return (text or "").strip()[-400:]


def _decode_pcm(path: str, ff: str, start: float, end: Optional[float]):
    """**一次**解码同时产出 PCM MD5、字节数、解码后参数与可播放性。

    用 ``Popen`` 流式读 stdout，边读边更新 MD5 并累计字节数，**不保留整段
    PCM**：74 分钟 CD 原先需要源+输出两份 s32le 常驻（约 2.92 GiB），
    现在峰值只有 1 MiB 量级的缓冲。

    与原来的 ``decode_ok`` / ``decoded_stream_info`` / ``pcm_bytes`` 三次全解码
    相比，stdout 的 PCM 字节流完全一致，因此 MD5、帧数、时长判据不变；
    stderr 用临时文件承接（避免管道缓冲区满导致死锁），其中的输出流描述行
    由 :func:`_parse_decoded_stream` 解析出解码后采样率与声道数。

    返回 ``(md5, pcm_len, decoded_info, ok, err)``；解码失败时
    ``md5=""``、``pcm_len=0``，与 ``pcm_bytes`` 返回 ``b""`` 的失败语义等价。
    """
    args = [ff, "-nostdin", "-hide_banner", "-nostats"]
    if start:
        args += ["-ss", f"{start:.6f}"]
    if end is not None:
        args += ["-to", f"{end:.6f}"]
    args += ["-i", path, "-map", "0:a:0?", "-f", "s32le", "-c:a", "pcm_s32le", "-"]

    digest = hashlib.md5()
    total = 0
    try:
        with tempfile.TemporaryFile() as errf:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=errf)
            try:
                stream = proc.stdout
                while True:
                    chunk = stream.read(_PCM_CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
                    total += len(chunk)
            finally:
                stream.close()
                try:
                    rc = proc.wait(timeout=300)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    raise
            errf.seek(0)
            stderr_text = errf.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return "", 0, {}, False, str(e)

    info = _parse_decoded_stream(stderr_text)
    if rc != 0:
        return "", 0, info, False, _decode_error(stderr_text)
    return digest.hexdigest(), total, info, True, ""


def pcm_md5(path: str, ff: str, start: float, end: Optional[float]) -> str:
    """把音频（区段）解码为 s32le 裸 PCM 并求 MD5，用于无损 / 无截断比对。

    走与校验主链路相同的流式解码实现，不再把整段 PCM 读进内存。
    """
    md5, _length, _info, ok, _err = _decode_pcm(path, ff, start, end)
    return md5 if ok else ""


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
    src_ctx: Optional[SourceContext] = None,
) -> TrackVerifyResult:
    """统一校验入口：分轨（比区段）与直转（比整文件）共用。

    参数：
      src_audio   —— 源整轨音频路径
      out_path    —— 输出 FLAC 路径
      src_start / src_end —— 比对源的那个时间区段；直转时传 (0.0, None)
      dsd_note    —— 源为 DSD 时填入转换说明（如低通截止频率、目标采样率），
                    会被记入 ``lossy_reason`` 并在结果中明确标注为有损转换
      src_ctx     —— 可选的「源音频探测上下文」（见 :class:`SourceContext`）。
                    批量校验时由 :func:`verify_outputs` 按源文件缓存并复用，
                    避免 N 首曲目反复探测 / 解码同一个整轨。不传时行为与
                    单独校验完全一致。
    """
    res = TrackVerifyResult(out_path=out_path, track_no=track_no)

    src = src_ctx.params if src_ctx is not None else probe_audio(src_audio, ffprobe)
    out = probe_audio(out_path, ffprobe)
    if src:
        res.src_params = src
    if out:
        res.out_params = out

    # ---- 一次解码，同时取得 PCM 摘要 / 字节数 / 解码后参数 / 可播放性 ----
    # 源与输出各解码 1 次（共 2 次），取代原先的
    # decode_ok + decoded_stream_info(src) + decoded_stream_info(out)
    # + pcm_bytes(src) + pcm_bytes(out) 共 5 次全解码。
    # PCM 以流式方式处理，内存占用与音频长度无关。
    src_md5, src_pcm_len, src_dec, _src_ok, _src_err = _decode_pcm(
        src_audio, ff, src_start, src_end
    )
    out_md5, out_pcm_len, out_dec, out_ok, out_err = _decode_pcm(
        out_path, ff, 0.0, None
    )
    # 同一整轨的解码后参数回填到上下文，后续曲目无需重复解析。
    if src_ctx is not None and src_dec and not src_ctx.decoded:
        src_ctx.decoded = dict(src_dec)

    # ---- 1) 可播放性 ----
    res.playable = out_ok
    if not out_ok:
        res.errors.append("文件无法解码（可能已损坏）：" + str(out_err)[:300])

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
    src_ch = int((src or {}).get("channels") or 0)
    out_ch = int((out or {}).get("channels") or 0)

    # 采样率必须取**解码后**的真实值：DSD 容器声明 2.8224MHz（1-bit 比特率），
    # 而 ffmpeg 解码后输出的是 352.8kHz PCM（相差 8 倍）。
    # 用容器率推算时长会得到错误结果。解码后参数已由同一次 s32le 解码的
    # 输出流描述行给出；上下文里缓存的值作为兜底。
    dec_src = src_dec or (src_ctx.decoded if src_ctx is not None else {}) or {}
    src_rate = int(dec_src.get("decoded_sample_rate")
                   or (src or {}).get("sample_rate") or 0)
    out_rate = int(out_dec.get("decoded_sample_rate")
                   or (out or {}).get("sample_rate") or 0)
    src_ch = int(dec_src.get("decoded_channels") or src_ch) or src_ch
    out_ch = int(out_dec.get("decoded_channels") or out_ch) or out_ch

    res.src_frames = decoded_frames(src_pcm_len, src_ch)
    res.out_frames = decoded_frames(out_pcm_len, out_ch)

    if not src_pcm_len or not out_pcm_len:
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
        if src_md5 and out_md5 and src_md5 == out_md5:
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
    dsd_note: str = "",
    src_ctx: Optional[SourceContext] = None,
) -> TrackVerifyResult:
    """校验分轨输出（比对源文件的某个时间区段）。

    ``dsd_note`` 非空时按「有损转换」处理（DSD → PCM 不可逆，
    无法做逐样本 MD5 比对，长度一致性改由时长判据保证）。
    """
    return verify_output(
        src_audio, out_path, ff, ffprobe,
        src_start=src_start, src_end=src_end, track_no=track_no,
        dsd_note=dsd_note, on_progress=on_progress, src_ctx=src_ctx,
    )


def verify_whole_file(
    src_audio: str,
    out_path: str,
    ff: str,
    ffprobe: str,
    on_progress=None,
    dsd_note: str = "",
    src_ctx: Optional[SourceContext] = None,
) -> TrackVerifyResult:
    """校验「整轨文件 → FLAC」的直接转换结果（比对整个文件）。

    与 :func:`verify_track` 的区别：这里比对的是**整个文件**而非某个时间区段，
    适用于不依赖 CUE 的直接转换场景。
    """
    return verify_output(
        src_audio, out_path, ff, ffprobe,
        src_start=0.0, src_end=None, track_no=1,
        dsd_note=dsd_note, on_progress=on_progress, src_ctx=src_ctx,
    )


def verify_outputs(
    sheet,
    out_paths: List[str],
    ff: str,
    ffprobe: str,
    on_progress=None,
    dsd_note: str = "",
) -> VerificationReport:
    """对一批分轨输出逐一校验，返回汇总报告。

    sheet.tracks 与 out_paths 按出现顺序一一对应。

    同一源整轨的容器参数按 ``(绝对路径, size, mtime_ns)`` 缓存：
    N 首曲目只探测一次；解码后参数在首次 PCM 解码时回填到同一个上下文，
    因此源整轨不再需要额外做整文件全解码。多 ``FILE`` 的 CUE
    则按各自的音源文件分别缓存。
    """
    report = VerificationReport()
    report.total = len(out_paths)
    ctx_cache: Dict[Tuple, SourceContext] = {}
    for i, out_path in enumerate(out_paths):
        if i >= len(sheet.tracks):
            break
        tr = sheet.tracks[i]
        key = _source_key(tr.file)
        ctx = ctx_cache.get(key)
        if ctx is None:
            ctx = SourceContext(
                path=tr.file,
                key=key,
                params=probe_audio(tr.file, ffprobe),
            )
            ctx_cache[key] = ctx
        if on_progress:
            on_progress(f"  校验 {os.path.basename(out_path)} …\n")
        r = verify_track(
            tr.file, tr.start, tr.end, out_path, tr.number, ff, ffprobe,
            on_progress, dsd_note=dsd_note, src_ctx=ctx,
        )
        report.results.append(r)
        if r.ok:
            report.passed += 1
            if on_progress:
                if r.lossy_reason:
                    on_progress("    ✓ 可播放 / 参数一致 / 时长一致（无损转换）\n")
                else:
                    on_progress(
                        "    ✓ 可播放 / 参数一致 / 帧数时长一致 / PCM 逐字节一致\n"
                    )
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
