"""不依赖 CUE 的直接转换：把整轨无损音频文件转封装 / 转码为 FLAC。

与 ``splitter`` 的区别：
- ``splitter`` 依据 CUE 的 INDEX 时间码把**一个**整轨文件切成多首；
- 本模块处理**不切分**的场景：单个（或多个）整轨音频文件 → 一个 FLAC 文件。

支持的输入格式（由 ffmpeg 的解码能力决定）：
- PCM 系无损：``.wav`` ``.flac`` ``.ape`` ``.wv`` ``.tta`` ``.tak`` ``.alac`` ``.aiff`` …
- DSD 系：``.dsf`` ``.dff``（DSDIFF）

无损性说明（重要，勿夸大）：
- **PCM 系输入**：解码后的 PCM 与源文件逐样本一致 → 转为 FLAC 是**真正无损**。
  本模块用「源文件与输出文件各自解码为 s32le 后求 MD5」来实证这一点。
- **DSD 输入**：DSD 是 1-bit 德尔塔-西格玛编码，转成 PCM（FLAC）本质是
  **高质量转换，而非逐样本无损**——DSD 比特流无法无损映射为 PCM 样本。
  本模块对 DSD 显式施加低通滤波 + 抽取降采样（业界标准做法），
  并在输出与日志中明确标注其性质，不把它混称为「无损」。
- **FLAC 输入**：已经是 FLAC 编码，走**流拷贝**（不重新编码），
  结果与源文件字节级等价。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .ffmpeg_provider import resolve_tool
from .verify import probe_audio, pcm_md5, decode_ok, verify_output

# ---------------------------------------------------------------------------
# 格式常量
# ---------------------------------------------------------------------------

#: 支持直接转换的输入扩展名（小写，含点）。
SUPPORTED_EXTS = (
    # PCM 系无损
    ".flac", ".wav", ".ape", ".wv", ".tta", ".tak",
    ".alac", ".m4a", ".aiff", ".aif", ".pcm", ".sf",
    # DSD 系
    ".dsf", ".dff",
)

#: DSD（1-bit）容器扩展名。
DSD_EXTS = (".dsf", ".dff")

#: DSD 转换时的目标采样率与低通截止频率（按 DSD 档位区分）。
#:
#: DSD64 = 2.8224MHz，ffmpeg 的 DSD 解码器固定按 8× 抽取 → 352.8kHz PCM，
#: 再由 aresample 抽到目标采样率。DSD 噪声整形会把量化噪声推到超声频段，
#: 因此必须先用 lowpass 截止掉，否则输出里全是嘶嘶声。
_DSD_TARGETS = {
    # dsd_rate -> (目标采样率, 低通截止 Hz)
    2822400: (88200, 24000),    # DSD64
    5644800: (176400, 40000),   # DSD128
    11289600: (352800, 80000),  # DSD256
}


class ConvertError(RuntimeError):
    """转换过程中的可预期错误。"""


@dataclass
class ConvertResult:
    """单个文件的转换结果。"""

    src: str
    out_path: Optional[str] = None
    ok: bool = False
    #: 是否经实证为逐样本无损（PCM MD5 一致）；DSD 恒为 False。
    lossless: bool = False
    #: 供用户阅读的性质说明（如 DSD 的「高质量转换」提示）。
    note: str = ""
    errors: List[str] = field(default_factory=list)
    #: 校验警告（如「参数被改动」但不影响播放）。
    warnings: List[str] = field(default_factory=list)
    src_params: dict = field(default_factory=dict)
    out_params: dict = field(default_factory=dict)
    #: 统一的校验结论（verify.TrackVerifyResult）。
    verify: Optional[object] = None

    @property
    def name(self) -> str:
        return os.path.basename(self.src)

    @property
    def status(self) -> str:
        """人类可读的判定结论：无损 / 有损转换 / 参数被改动 / 校验未通过。

        未做校验（``--no-verify``）时**不宣称「无损」**——没有比对就没有
        结论依据，此时只报「已转换（未校验）」。
        """
        if self.verify is not None:
            return getattr(self.verify, "status", "校验未通过")
        if not self.ok:
            return "失败"
        return "已转换（未校验）"

def is_supported_audio(path: str) -> bool:
    """判断路径是否是本模块支持的音频文件（按扩展名）。"""
    return os.path.splitext(path)[1].lower() in SUPPORTED_EXTS


def _sanitize(name: str) -> str:
    """把文件名整理为安全的输出名（不含扩展名）。"""
    name = (name or "").strip()
    for ch in '\\/:*?"<>|\r\n\t':
        name = name.replace(ch, "_")
    name = name.strip(" ._")
    if not name:
        name = "output"
    return name[:120]


def _unique_path(path: str, used: dict) -> str:
    """同一批处理内避免输出文件重名。"""
    if path not in used:
        used[path] = 0
        return path
    used[path] += 1
    base, ext = os.path.splitext(path)
    return f"{base}_{used[path]}{ext}"


def _detect_bits(src: str, ffprobe: str) -> int:
    """探测源文件的有效位深（取不到时返回 0）。"""
    params = probe_audio(src, ffprobe)
    if not params:
        return 0
    return int(params.get("bits") or 0)


def _is_dsd(src: str, ffprobe: str) -> bool:
    """判断源是否为 DSD（按容器扩展名或 ffprobe 解码器名）。"""
    if os.path.splitext(src)[1].lower() in DSD_EXTS:
        return True
    params = probe_audio(src, ffprobe)
    codec = (params or {}).get("codec", "").lower()
    return codec.startswith("dsd")


def _dsd_rate(src: str, ffprobe: str) -> int:
    """探测 DSD 源码流的比特率（DSD64 = 2822400 等）。"""
    params = probe_audio(src, ffprobe)
    return int((params or {}).get("sample_rate") or 0)


def _copy_tags(src: str, dst: str, ff: str) -> None:
    """把源文件的元数据（标题/表演者/专辑等）复制到输出 FLAC。

    用 ffmetadata 中转：ffprobe 导出 → ffmpeg 导入。
    失败不影响转换本身（标签只是附加信息）。
    """
    import subprocess
    import tempfile

    try:
        meta = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format_tags",
             "-of", "json", src],
            capture_output=True, text=True, timeout=60,
        ) if (ffprobe := resolve_tool("ffprobe")) else None
        if meta is None or meta.returncode != 0:
            return
        import json

        tags = (json.loads(meta.stdout).get("format", {}) or {}).get("tags", {})
        if not tags:
            return
        with tempfile.NamedTemporaryFile(
            "w", suffix=".ffmeta", encoding="utf-8", delete=False
        ) as f:
            for k, v in tags.items():
                if v:
                    f.write(f";FFMETADATA1\n{k}={v}\n")
            meta_path = f.name
        try:
            subprocess.run(
                [ff, "-v", "error", "-y", "-i", dst, "-i", meta_path,
                 "-map_metadata", "1", "-c:a", "copy", "-f", "flac",
                 dst + ".tmp.flac"],
                capture_output=True, text=True, timeout=600,
            )
            if os.path.isfile(dst + ".tmp.flac"):
                os.replace(dst + ".tmp.flac", dst)
        finally:
            if os.path.isfile(meta_path):
                os.remove(meta_path)
            tmp = dst + ".tmp.flac"
            if os.path.isfile(tmp):
                os.remove(tmp)
    except Exception:
        pass


def _run(cmd: List[str]) -> None:
    """执行 ffmpeg，失败时抛出带错误输出的异常。"""
    import subprocess

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise ConvertError(f"ffmpeg 执行失败（退出码 {proc.returncode}）：\n{err[:2000]}")


def convert_file(
    src: str,
    out_dir: Optional[str] = None,
    copy_cover: bool = True,
    verify: bool = True,
    used: Optional[dict] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> ConvertResult:
    """把单个整轨音频文件转换为 FLAC。

    参数：
      src        —— 源音频文件路径
      out_dir    —— 输出目录；None 时输出到源文件所在目录
      copy_cover —— 是否复制封面
      verify     —— 是否做无损实证（PCM MD5 比对）
      used       —— 批处理内用于避免重名的字典
      on_progress—— 进度回调（接受一段文本）

    返回 ConvertResult；失败时 ``ok=False`` 且 ``errors`` 非空。
    """
    res = ConvertResult(src=src)
    ff = resolve_tool("ffmpeg")
    ffprobe = resolve_tool("ffprobe")

    if not os.path.isfile(src):
        res.errors.append(f"文件不存在：{src}")
        return res
    if not is_supported_audio(src):
        res.errors.append(
            f"不支持的音频格式：{os.path.splitext(src)[1] or '(无扩展名)'}；"
            f"支持：{'、'.join(SUPPORTED_EXTS)}"
        )
        return res

    src_dir = os.path.dirname(os.path.abspath(src))
    target_dir = out_dir or src_dir
    try:
        os.makedirs(target_dir, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        res.errors.append(f"无法创建输出目录 {target_dir}：{e}")
        return res

    out_path = os.path.join(
        target_dir, _sanitize(os.path.basename(os.path.splitext(src)[0])) + ".flac"
    )
    if used is not None:
        out_path = _unique_path(out_path, used)

    if os.path.abspath(out_path) == os.path.abspath(src):
        # 源本身就是 FLAC 且输出位置相同：直接判定成功（内容本就一致）
        res.out_path = out_path
        res.ok = True
        res.lossless = True
        res.note = "源文件已是 FLAC 且输出位置与源相同，未做改动。"
        return res

    dsd = _is_dsd(src, ffprobe)
    ext = os.path.splitext(src)[1].lower()

    cmd = [
        ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", src,
        "-map", "0:a:0",
    ]

    if dsd:
        # ---- DSD 路径：低通 + 抽取降采样（高质量转换，非逐样本无损）----
        rate = _dsd_rate(src, ffprobe)
        target_sr, cutoff = _DSD_TARGETS.get(
            rate, (88200 if rate >= 2822400 else 44100, 24000)
        )
        cmd += [
            "-af", f"lowpass=f={cutoff},aresample=resampler=soxr:osr={target_sr}",
            "-ar", str(target_sr),
            "-c:a", "flac",
            "-sample_fmt", "s32",
        ]
        res.note = (
            f"DSD 源（1-bit 编码）：已按标准做法低通滤波（截止 {cutoff} Hz）"
            f"并抽取降采样至 {target_sr} Hz / 32-bit FLAC。"
            f"此过程属于高质量转换，并非逐样本无损。"
        )
        if on_progress:
            on_progress(f"  {res.name}：DSD 转换 → {target_sr} Hz FLAC\n")
    else:
        # ---- PCM 系路径：逐样本无损 ----
        bits = _detect_bits(src, ffprobe)
        cmd += ["-c:a", "flac"]
        if ext == ".flac":
            # 已是 FLAC：流拷贝即可，完全不重新编码。
            # 流拷贝时 ffmpeg 不会自动填充输出流参数，必须显式给出采样率，
            # 否则写头时报 "sample rate not set / Could not write header"。
            src_rate = int((probe_audio(src, ffprobe) or {}).get("sample_rate") or 0)
            cmd = [
                ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-i", src, "-map", "0:a:0", "-c:a", "copy",
            ]
            if src_rate:
                cmd += ["-ar", str(src_rate)]
            res.note = "源文件已是 FLAC，采用流拷贝（未重新编码）。"
        elif bits:
            # FLAC 编码器只接受 s16 / s32：24bit 用 s32 承载
            cmd += ["-sample_fmt", "s16" if bits <= 16 else "s32"]

    if copy_cover:
        cmd += ["-map", "0:v:0?", "-c:v", "copy"]
    cmd += [out_path]

    try:
        if on_progress:
            on_progress(f"  {res.name} → {os.path.basename(out_path)}\n")
        _run(cmd)
    except ConvertError as e:
        res.errors.append(str(e))
        return res
    except Exception as e:  # noqa: BLE001
        res.errors.append(f"转换失败：{e}")
        return res

    if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        res.errors.append("未生成有效的输出文件")
        return res

    # 复制元数据（失败不影响主流程）
    _copy_tags(src, out_path, ff)

    res.out_path = out_path

    if verify:
        # 复用与分轨链路完全相同的校验逻辑（verify.verify_output）：
        # 可播放性 + 采样率 / 位深 / 声道数 + 帧数 / 时长 + PCM 逐字节 MD5。
        # 两条链路用同一套判据，避免「分轨会查、直转不查」的不一致。
        vres = verify_output(
            src, out_path, ff, ffprobe,
            src_start=0.0, src_end=None, track_no=1,
            dsd_note=res.note if dsd else "",
        )
        res.verify = vres
        res.src_params = vres.src_params
        res.out_params = vres.out_params
        res.lossless = vres.lossless
        res.errors.extend(vres.errors)
        res.warnings.extend(vres.warnings)
    else:
        res.out_params = probe_audio(out_path, ffprobe) or {}
        res.src_params = probe_audio(src, ffprobe) or {}
        res.lossless = not dsd

    res.ok = not res.errors
    return res


def convert_files(
    paths: List[str],
    out_dir: Optional[str] = None,
    copy_cover: bool = True,
    verify: bool = True,
    on_progress: Optional[Callable[[str], None]] = None,
) -> List[ConvertResult]:
    """批量转换多个音频文件（不依赖 CUE）。"""
    results: List[ConvertResult] = []
    used: dict = {}
    total = len(paths)
    for i, p in enumerate(paths, 1):
        if on_progress:
            on_progress(f"\n=== [{i}/{total}] {os.path.basename(p)} ===\n")
        r = convert_file(
            p, out_dir=out_dir, copy_cover=copy_cover,
            verify=verify, used=used, on_progress=on_progress,
        )
        results.append(r)
        if on_progress:
            if r.ok:
                on_progress(f"[完成] {r.name} → {r.out_path}（{r.status}）\n")
                _print_verify_detail(r, on_progress)
            else:
                on_progress(f"[失败] {r.name}：{'; '.join(r.errors)}\n")
                _print_verify_detail(r, on_progress)
    return results


def _print_verify_detail(r: "ConvertResult", on_progress) -> None:
    """输出单个文件的逐项校验结果（参数对照 + 判定结论）。"""
    if on_progress is None:
        return
    v = r.verify
    if v is None:
        return
    on_progress(
        "        校验：采样率 {sr} / 位深 {sb} / 声道 {sc} / 帧数 {sf} / 时长 {sd}\n".format(
            sr=f"{v.src_params.get('sample_rate', '?')}→"
               f"{v.out_params.get('sample_rate', '?')}",
            sb=f"{v.src_params.get('bits', '?')}→{v.out_params.get('bits', '?')}",
            sc=f"{v.src_params.get('channels', '?')}→"
               f"{v.out_params.get('channels', '?')}",
            sf=f"{v.src_frames}→{v.out_frames}",
            sd=f"{v.src_duration:.3f}s→{v.out_duration:.3f}s",
        )
    )
    if v.lossy_reason:
        on_progress(f"        提示：{v.lossy_reason}\n")
    for w in v.warnings:
        on_progress(f"        提示：{w}\n")


def format_convert_summary(results: List[ConvertResult]) -> str:
    """把转换结果渲染为可读汇总，含逐项校验结论。"""
    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    verified = any(r.verify is not None for r in results)
    lines: List[str] = []
    lines.append("─" * 52)
    lines.append("直接转换汇总（含无损校验）" if verified else "直接转换汇总")
    lines.append(f"  处理文件数：{len(results)}")
    lines.append(f"  成功      ：{len(ok)}")
    lines.append(f"  失败      ：{len(failed)}")
    lines.append("─" * 52)
    for r in results:
        mark = "✓" if r.ok else "✗"
        lines.append(
            f"  {mark} {r.name} → {os.path.basename(r.out_path or '')}（{r.status}）"
        )
        v = r.verify
        if v is not None:
            lines.append(
                "      采样率 {sr} / 位深 {sb} / 声道 {sc} / 帧数 {sf} / 时长 {sd}".format(
                    sr=f"{v.src_params.get('sample_rate', '?')}→"
                       f"{v.out_params.get('sample_rate', '?')}",
                    sb=f"{v.src_params.get('bits', '?')}→"
                       f"{v.out_params.get('bits', '?')}",
                    sc=f"{v.src_params.get('channels', '?')}→"
                       f"{v.out_params.get('channels', '?')}",
                    sf=f"{v.src_frames}→{v.out_frames}",
                    sd=f"{v.src_duration:.3f}s→{v.out_duration:.3f}s",
                )
            )
        if r.note:
            lines.append(f"      说明：{r.note}")
        if v is not None and v.lossy_reason:
            lines.append(f"      提示：{v.lossy_reason}")
        for w in r.warnings:
            lines.append(f"      提示：{w}")
        for e in r.errors:
            lines.append(f"      {e}")
    lines.append("─" * 52)
    return "\n".join(lines)
