"""verify 模块测试：用真实 ffmpeg 生成音频，验证「可播放 / 参数匹配 / 无损无截断」。

与 test_batch 不同，这里需要真实解码，因此无 ffmpeg 时整体跳过。
"""

import os
import subprocess
import tempfile

import pytest

from cue2flac import splitter, verify as verify_mod
from cue2flac.cue_parser import parse_cue
from cue2flac.ffmpeg_provider import resolve_tool

try:
    _ff = resolve_tool("ffmpeg")
    _ffprobe = resolve_tool("ffprobe")
except Exception:  # noqa: BLE001
    _ff = _ffprobe = None

pytestmark = pytest.mark.skipif(
    not (_ff and _ffprobe), reason="需要 ffmpeg/ffprobe 才能验证解码"
)

# 6 秒、44100/立体声/16bit 的整轨；两首分别落在 0s 与 3s
CUE = """FILE "src.wav" WAVE
  TRACK 01 AUDIO
    TITLE "One"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Two"
    INDEX 01 00:03:00
"""


def _make_wav(path, dur=6, sr=44100, ch=2):
    subprocess.run(
        [
            _ff, "-y", "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={dur}",
            "-ar", str(sr), "-ac", str(ch), "-c:a", "pcm_s16le", path,
        ],
        check=True, capture_output=True,
    )


@pytest.fixture
def album():
    d = tempfile.mkdtemp()
    wav = os.path.join(d, "src.wav")
    _make_wav(wav)
    cue_path = os.path.join(d, "cd.cue")
    with open(cue_path, "w", encoding="utf-8") as f:
        f.write(CUE)
    return d, wav, cue_path


def test_split_with_verify_passes(album):
    d, wav, cue_path = album
    sheet = parse_cue(cue_path)
    out = os.path.join(d, "out")
    results = splitter.split(sheet, out, verify=True)
    assert len(results) == 2
    for r in results:
        assert os.path.isfile(r)
    report = verify_mod.verify_outputs(sheet, results, _ff, _ffprobe)
    assert report.all_ok, verify_mod.format_verification_report(report)
    assert report.passed == 2


def test_verify_detects_corruption(album):
    d, wav, cue_path = album
    sheet = parse_cue(cue_path)
    out = os.path.join(d, "out")
    results = splitter.split(sheet, out, verify=False)
    bad = results[0]
    # 用全零覆盖，破坏文件
    with open(bad, "wb") as f:
        f.write(b"\x00" * 4096)
    r = verify_mod.verify_track(
        wav, sheet.tracks[0].start, sheet.tracks[0].end, bad, 1, _ff, _ffprobe
    )
    assert not r.ok
    assert not r.playable


def test_verify_detects_param_mismatch(album):
    d, wav, cue_path = album
    sheet = parse_cue(cue_path)
    # 生成一个不同采样率的 FLAC（22050 ≠ 44100）
    bad = os.path.join(d, "wrong.flac")
    subprocess.run(
        [_ff, "-y", "-i", wav, "-ar", "22050", "-c:a", "flac", bad],
        check=True, capture_output=True,
    )
    r = verify_mod.verify_track(wav, 0.0, None, bad, 1, _ff, _ffprobe)
    assert not r.params_match


# ---------------------------------------------------------------------------
# 统一校验：判定条件与逐项参数校验
# ---------------------------------------------------------------------------


def _make_wav24(path, dur=6, sr=48000, ch=2):
    subprocess.run(
        [
            _ff, "-y", "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={dur}",
            "-ar", str(sr), "-ac", str(ch), "-c:a", "pcm_s24le", path,
        ],
        check=True, capture_output=True,
    )


def test_probe_audio_returns_real_params(album):
    """probe_audio 必须真正返回参数。

    回归防护：ffprobe 的 -show_entries 是**白名单**语义，若不把 codec_type
    列入白名单，用它过滤流会把所有流都跳过、恒返回 None，
    导致参数比对形同虚设。
    """
    d, wav, cue_path = album
    p = verify_mod.probe_audio(wav, _ffprobe)
    assert p is not None, "probe_audio 返回 None，参数探测失效"
    assert p["sample_rate"] == 44100
    assert p["channels"] == 2
    assert p["bits"] == 16
    assert p["codec"].startswith("pcm_")


def test_verify_reports_frame_and_duration(album):
    """校验结果必须带帧数与时长对照，且与源一致。"""
    d, wav, cue_path = album
    sheet = parse_cue(cue_path)
    out = os.path.join(d, "out")
    results = splitter.split(sheet, out, verify=False)
    r = verify_mod.verify_track(
        wav, sheet.tracks[0].start, sheet.tracks[0].end, results[0], 1,
        _ff, _ffprobe,
    )
    assert r.ok, verify_mod.format_verification_report(
        verify_mod.VerificationReport(results=[r])
    )
    assert r.src_frames == r.out_frames > 0
    assert abs(r.src_duration - r.out_duration) < 1e-3
    assert r.length_match and r.no_truncation


def test_verify_detects_bit_depth_drop(album):
    """24bit 源被降为 16bit 时必须报「参数被改动」。"""
    d = tempfile.mkdtemp()
    wav = os.path.join(d, "src24.wav")
    _make_wav24(wav)
    bad = os.path.join(d, "dropped.flac")
    subprocess.run(
        [_ff, "-y", "-i", wav, "-sample_fmt", "s16", "-c:a", "flac", bad],
        check=True, capture_output=True,
    )
    r = verify_mod.verify_whole_file(wav, bad, _ff, _ffprobe)
    assert not r.params_match
    assert not r.lossless
    assert r.status == "参数被改动"
    assert any("位深" in e for e in r.errors)


def test_verify_detects_channel_change(album):
    """声道数被改时必须报「参数被改动」。"""
    d, wav, cue_path = album
    bad = os.path.join(d, "mono.flac")
    subprocess.run(
        [_ff, "-y", "-i", wav, "-ac", "1", "-c:a", "flac", bad],
        check=True, capture_output=True,
    )
    r = verify_mod.verify_whole_file(wav, bad, _ff, _ffprobe)
    assert not r.params_match
    assert r.status == "参数被改动"
    assert any("声道数" in e for e in r.errors)


def test_verify_detects_truncation(album):
    """输出被截断时必须报长度不一致。"""
    d, wav, cue_path = album
    full = os.path.join(d, "full.flac")
    subprocess.run(
        [_ff, "-y", "-i", wav, "-c:a", "flac", full], check=True, capture_output=True,
    )
    raw = open(full, "rb").read()
    with open(full, "wb") as f:
        f.write(raw[: len(raw) // 2])
    r = verify_mod.verify_whole_file(wav, full, _ff, _ffprobe)
    assert not r.length_match
    assert not r.no_truncation
    assert r.out_frames < r.src_frames
    assert r.status == "参数被改动"


def test_verify_flags_resample_by_duration(album):
    """重采样（帧数变、时长不变）时时长判据应生效且参数被判为改动。"""
    d, wav, cue_path = album
    bad = os.path.join(d, "resample.flac")
    subprocess.run(
        [_ff, "-y", "-i", wav, "-ar", "22050", "-c:a", "flac", bad],
        check=True, capture_output=True,
    )
    r = verify_mod.verify_whole_file(wav, bad, _ff, _ffprobe)
    assert not r.params_match, "重采样后采样率应被判为改动"
    assert any("采样率" in e for e in r.errors)
    # 采样率不同 → 帧数本就不同，时长应仍然一致
    assert abs(r.src_duration - r.out_duration) < 1e-3


def test_verify_dsd_marks_lossy_not_pass(album):
    """DSD 源必须被判为「有损转换」，不得混称无损。"""
    d, wav, cue_path = album
    bad = os.path.join(d, "x.flac")
    subprocess.run(
        [_ff, "-y", "-i", wav, "-c:a", "flac", bad], check=True, capture_output=True,
    )
    r = verify_mod.verify_whole_file(
        wav, bad, _ff, _ffprobe, dsd_note="DSD 转换 → 88200 Hz FLAC"
    )
    assert r.lossy_reason, "有损原因必须记录"
    assert r.status == "有损转换"
    assert not r.lossless


def test_decoded_frames_from_pcm_length():
    """帧数推算：s32le 每样本 4 字节。"""
    assert verify_mod.decoded_frames(0, 2) == 0
    assert verify_mod.decoded_frames(8, 2) == 1
    assert verify_mod.decoded_frames(8 * 100, 2) == 100


def test_decoded_stream_info_reports_rate(album):
    """解码后的真实采样率必须可获取（容器率可能与解码率不同）。"""
    d, wav, cue_path = album
    info = verify_mod.decoded_stream_info(wav, _ff)
    assert info.get("decoded_sample_rate") == 44100
    assert info.get("decoded_channels") == 2
