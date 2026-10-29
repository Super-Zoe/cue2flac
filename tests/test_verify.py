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
