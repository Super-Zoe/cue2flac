"""cue_parser 单元测试。"""

import os
import tempfile
from cue2flac.cue_parser import parse_cue

SAMPLE = """REM GENRE "Classical"
REM DATE 2021
PERFORMER "Album Artist"
TITLE "Demo Album"
FILE "demo.flac" WAVE
  TRACK 01 AUDIO
    TITLE "First Song"
    PERFORMER "Singer A"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Second Song"
    INDEX 01 03:21:30
  TRACK 03 AUDIO
    TITLE "Third Song"
    INDEX 00 07:10:00
    INDEX 01 07:12:45
"""

# 3:21:30 -> 3*60+21 + 30/75 = 201.4 s
# 7:10:00 -> 430.0 s ; 7:12:45 -> 432 + 45/75 = 432.6 s


def test_parse_basic():
    with tempfile.TemporaryDirectory() as d:
        cue = os.path.join(d, "test.cue")
        with open(cue, "w", encoding="utf-8") as f:
            f.write(SAMPLE)
        # 音频文件不存在时，file 仍是推测的绝对路径；解析本身不应抛错
        sheet = parse_cue(cue)
        assert sheet.album_title == "Demo Album"
        assert sheet.album_performer == "Album Artist"
        assert sheet.genre == "Classical"
        assert sheet.date == "2021"
        assert sheet.total_tracks == 3

        t1, t2, t3 = sheet.tracks
        assert t1.title == "First Song"
        assert t1.performer == "Singer A"
        assert abs(t1.start - 0.0) < 1e-6
        assert abs(t1.end - 201.4) < 1e-3

        assert t2.title == "Second Song"
        # 未单独指定的表演者回退到专辑级
        assert t2.performer == "Album Artist"
        assert abs(t2.start - 201.4) < 1e-3
        # 第 2 首切到第 3 首的 INDEX 01（7:12:45）
        assert abs(t2.end - 432.6) < 1e-3

        # 第三首使用 INDEX 01（7:12:45）
        assert abs(t3.start - 432.6) < 1e-3
        assert t3.end is None  # 最后一首到文件末尾


def test_timecode_seconds():
    from cue2flac.cue_parser import _parse_time

    assert _parse_time("00:00:00") == 0.0
    assert abs(_parse_time("01:00:00") - 60.0) < 1e-9
    assert abs(_parse_time("03:21:30") - 201.4) < 1e-9


def test_gbk_decoding():
    cue = """PERFORMER "测试艺术家"
TITLE "测试专辑"
FILE "音乐.flac" WAVE
  TRACK 01 AUDIO
    TITLE "中文曲目"
    INDEX 01 00:00:00
"""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "gbk.cue")
        with open(path, "w", encoding="gbk") as f:
            f.write(cue)
        sheet = parse_cue(path)
        assert sheet.album_title == "测试专辑"
        assert sheet.tracks[0].title == "中文曲目"


if __name__ == "__main__":
    test_parse_basic()
    test_timecode_seconds()
    test_gbk_decoding()
    print("All tests passed.")
