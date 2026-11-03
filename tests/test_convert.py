"""convert 模块测试：不依赖 CUE 的整轨直转 FLAC。

覆盖关键保证：
1. 支持的格式识别（.flac/.wav/.ape/.dsf/.dff…）；
2. **位深保持**：24bit 源转 FLAC 后仍是 24bit（FLAC 编码器只支持 s16/s32，
   24bit 必须用 s32 承载；若误传 s24，ffmpeg 会直接报错）；
3. **逐样本无损**：源与输出的 s32le PCM 的 MD5 一致；
4. 输出文件命名与目录规则；
5. 不支持的格式被拒绝并给出清晰原因。

与 test_batch 不同，这里需要真实 ffmpeg 来转码，因此无 ffmpeg 时整体跳过。
"""

import os
import subprocess
import tempfile

import pytest

from cue2flac import convert as convert_mod
from cue2flac.convert import (
    SUPPORTED_EXTS,
    convert_file,
    convert_files,
    format_convert_summary,
    is_supported_audio,
)
from cue2flac.ffmpeg_provider import resolve_tool

try:
    _ff = resolve_tool("ffmpeg")
    _ffprobe = resolve_tool("ffprobe")
except Exception:  # noqa: BLE001
    _ff = _ffprobe = None

pytestmark = pytest.mark.skipif(
    not (_ff and _ffprobe), reason="需要 ffmpeg/ffprobe 才能验证转码"
)


def _make_wav(path, dur=3, sr=44100, ch=2, bits=16):
    """生成指定位深的 WAV 测试素材。"""
    codec = {16: "pcm_s16le", 24: "pcm_s24le", 32: "pcm_s32le"}[bits]
    subprocess.run(
        [
            _ff, "-y", "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={dur}",
            "-ar", str(sr), "-ac", str(ch), "-c:a", codec, path,
        ],
        check=True, capture_output=True,
    )


def _probe(path):
    """读取输出文件的音频流参数，返回 {字段: 值}。"""
    out = subprocess.run(
        [
            _ffprobe, "-v", "error", "-select_streams", "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels,bits_per_raw_sample",
            "-of", "default=nw=1", path,
        ],
        check=True, capture_output=True, text=True,
    ).stdout
    d = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def _pcm_md5(path):
    proc = subprocess.run(
        [_ff, "-v", "error", "-i", path, "-f", "s32le", "-c:a", "pcm_s32le", "-"],
        check=True, capture_output=True,
    )
    import hashlib

    return hashlib.md5(proc.stdout).hexdigest()


# --------------------------------------------------------------------- 纯逻辑


def test_supported_formats_include_requested_ones():
    """用户明确要求的五种格式必须都在支持列表里。"""
    for ext in (".dff", ".dsf", ".ape", ".wav", ".flac"):
        assert ext in SUPPORTED_EXTS, f"缺少对 {ext} 的支持"


def test_is_supported_audio():
    assert is_supported_audio("a.flac")
    assert is_supported_audio("A.WAV")  # 大小写不敏感
    assert is_supported_audio("x.dsf")
    assert not is_supported_audio("y.cue")
    assert not is_supported_audio("z.txt")


def test_unsupported_format_is_rejected_with_reason():
    """不支持的格式应被拒绝，并给出可读的失败原因（而不是抛异常）。"""
    with tempfile.TemporaryDirectory() as d:
        bogus = os.path.join(d, "note.txt")
        with open(bogus, "w", encoding="utf-8") as f:
            f.write("not audio")
        r = convert_file(bogus, out_dir=os.path.join(d, "out"))
        assert not r.ok
        assert r.errors
        assert "不支持" in r.errors[0]


def test_missing_file_reports_error():
    r = convert_file(os.path.join("__no_such__", "x.flac"))
    assert not r.ok
    assert "不存在" in r.errors[0]


# --------------------------------------------------------------- 真实转码验证


def test_convert_wav_preserves_params_and_is_lossless():
    """16bit WAV 直转 FLAC：参数保持 + PCM 逐字节一致。"""
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "src.wav")
        _make_wav(src, bits=16, sr=44100, ch=2)
        out_dir = os.path.join(d, "out")

        r = convert_file(src, out_dir=out_dir, copy_cover=False, verify=True)
        assert r.ok, r.errors
        assert r.out_path and os.path.isfile(r.out_path)
        assert r.lossless, "PCM MD5 不一致，转换不是无损的"

        info = _probe(r.out_path)
        assert info["codec_name"] == "flac"
        assert int(info["sample_rate"]) == 44100
        assert int(info["channels"]) == 2
        assert int(info["bits_per_raw_sample"]) == 16
        assert _pcm_md5(src) == _pcm_md5(r.out_path)


def test_convert_24bit_wav_keeps_24bit():
    """24bit 源必须保持 24bit——FLAC 编码器只支持 s16/s32，需用 s32 承载。

    回归点：若把 24bit 映射成 ``-sample_fmt s24``，ffmpeg 会报
    "Invalid sample format 's24'" 而转换失败。
    """
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "src24.wav")
        _make_wav(src, bits=24, sr=48000, ch=2)
        out_dir = os.path.join(d, "out")

        r = convert_file(src, out_dir=out_dir, copy_cover=False, verify=True)
        assert r.ok, r.errors
        assert r.lossless

        info = _probe(r.out_path)
        assert int(info["sample_rate"]) == 48000
        assert int(info["bits_per_raw_sample"]) == 24, (
            f"24bit 源被降位深：输出 {info['bits_per_raw_sample']}"
        )
        assert _pcm_md5(src) == _pcm_md5(r.out_path)


def test_convert_flac_source_uses_stream_copy():
    """FLAC 源走流拷贝，不重新编码，结果与源字节级等价。"""
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "src.flac")
        subprocess.run(
            [
                _ff, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-ar", "44100", "-ac", "2", "-c:a", "flac", src,
            ],
            check=True, capture_output=True,
        )
        out_dir = os.path.join(d, "out")

        r = convert_file(src, out_dir=out_dir, copy_cover=False, verify=True)
        assert r.ok, r.errors
        assert r.lossless
        assert "流拷贝" in r.note
        assert _pcm_md5(src) == _pcm_md5(r.out_path)


def test_convert_multiple_files_and_naming():
    """多文件批量转换：输出命名为源文件名（去扩展名）+ .flac。"""
    with tempfile.TemporaryDirectory() as d:
        a = os.path.join(d, "Track One.wav")
        b = os.path.join(d, "Track Two.wav")
        _make_wav(a, bits=16)
        _make_wav(b, bits=16)
        out_dir = os.path.join(d, "out")

        results = convert_files(
            [a, b], out_dir=out_dir, copy_cover=False, verify=True
        )
        assert len(results) == 2
        assert all(r.ok for r in results), [r.errors for r in results]
        names = sorted(os.path.basename(r.out_path) for r in results)
        assert names == ["Track One.flac", "Track Two.flac"]

        text = format_convert_summary(results)
        assert "直接转换汇总" in text
        assert "成功" in text


def test_convert_to_same_dir_writes_next_to_source():
    """未指定输出目录时，输出到源文件所在目录。"""
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "song.wav")
        _make_wav(src)
        r = convert_file(src, out_dir=None, copy_cover=False, verify=True)
        assert r.ok, r.errors
        assert os.path.dirname(os.path.abspath(r.out_path)) == os.path.abspath(d)
        assert os.path.basename(r.out_path) == "song.flac"


def test_duplicate_names_do_not_overwrite():
    """不同目录下同名文件批量转换时不应互相覆盖。"""
    with tempfile.TemporaryDirectory() as d:
        d1 = os.path.join(d, "a")
        d2 = os.path.join(d, "b")
        os.makedirs(d1)
        os.makedirs(d2)
        f1 = os.path.join(d1, "same.wav")
        f2 = os.path.join(d2, "same.wav")
        _make_wav(f1)
        _make_wav(f2)
        out_dir = os.path.join(d, "out")

        results = convert_files(
            [f1, f2], out_dir=out_dir, copy_cover=False, verify=True
        )
        assert all(r.ok for r in results), [r.errors for r in results]
        outs = {os.path.basename(r.out_path) for r in results}
        assert len(outs) == 2, f"同名文件被覆盖了：{outs}"
