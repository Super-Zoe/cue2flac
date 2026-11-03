"""DSD（.dsf / .dff）分轨与直转的回归测试。

这些用例覆盖两个曾经真实存在的缺陷：

1. **分轨链路缺少低通+降采样** —— DSD64 解码率为 352800 Hz，
   远超 FLAC 编码器上限，ffmpeg 直接报 "2822400 Hz not supported"，
   分轨 100% 失败。现已与直转链路统一为「低通 + 抽取降采样」。
2. **DSD 档位查表永远落空** —— 旧表以 DSD 原始位率（2822400）为键，
   而 ffprobe 报告的是「容器声明值 ÷ 8」（352800），查表必落空、
   永远走 fallback（目标采样率取错档）。

DSD 源无法逐样本无损地映射为 PCM，因此断言的是：
- 转换**成功**且输出为合法 FLAC
- 目标采样率 / 低通截止 / 位深符合预期
- 每首**时长精确**（分轨定位正确、无截断）
- 结论被明确标注为「有损转换」而非「无损」
"""

from __future__ import annotations

import os
import subprocess

import pytest

from cue2flac import dsd
from cue2flac.batch import process_cues
from cue2flac.cue_parser import parse_cue
from cue2flac.dsd import DSD_TARGETS, dsd_target
from cue2flac.ffmpeg_provider import resolve_tool
from cue2flac.splitter import split
from cue2flac.verify import probe_audio

from dsd_fixtures import write_dff, write_dsf

_FF = resolve_tool("ffmpeg")
_FFPROBE = resolve_tool("ffprobe")

#: DSD64 的解码输出采样率（容器声明 2822400 ÷ 8）。
_DSD64_DECODED = 352800


def _write_cue(directory: str, src_name: str, points) -> str:
    """写一个把整轨切成若干首的 CUE。points 为各首起始秒数。"""
    lines = ['FILE "%s" WAVE' % src_name]
    for i, start in enumerate(points, 1):
        mm = int(start // 60)
        ss = int(start % 60)
        ff = int(round((start - mm * 60 - ss) * 75))
        lines += [
            "  TRACK %02d AUDIO" % i,
            '    TITLE "Track %02d"' % i,
            "    INDEX 01 %02d:%02d:%02d" % (mm, ss, ff),
        ]
    path = os.path.join(directory, "album.cue")
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# 纯逻辑：档位查表
# ---------------------------------------------------------------------------


def test_dsd_targets_use_decoded_rate_keys():
    """档位表的键必须是「解码输出采样率」，而非 DSD 原始位率。

    回归防护：旧表用 2822400（DSD64 位率）做键，而 ffprobe 报告的是
    容器声明值 ÷ 8 = 352800，查表永远落空 → 目标采样率取错档。
    """
    assert _DSD64_DECODED in DSD_TARGETS
    assert 2822400 not in DSD_TARGETS, "不应再以 DSD 原始位率作键"
    assert DSD_TARGETS[_DSD64_DECODED] == (88200, 24000)


def test_dsd_target_lookup():
    """DSD64 / 128 / 256 的目标采样率与低通截止应逐档递增。"""
    assert dsd_target(352800) == (88200, 24000)
    assert dsd_target(705600) == (176400, 40000)
    assert dsd_target(1411200) == (352800, 80000)
    # 未识别档位不得升采样
    target, _cut = dsd_target(100000)
    assert target <= 100000


def test_dsd_filter_contains_lowpass_and_soxr():
    """滤镜链必须同时含低通与 soxr 重采样。"""
    filt = dsd.dsd_filter(88200, 24000)
    assert "lowpass=f=24000" in filt
    assert "aresample" in filt and "soxr" in filt
    assert "osr=88200" in filt


def test_dsd_note_declares_lossy():
    """性质说明必须明确写「非逐样本无损」。"""
    note = dsd.dsd_note(88200, 24000)
    assert "并非逐样本无损" in note
    assert "88200" in note


# ---------------------------------------------------------------------------
# 真实文件：容器构造正确性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("writer,suffix", [(write_dsf, ".dsf"), (write_dff, ".dff")])
def test_dsd_container_is_readable(tmp_path, writer, suffix):
    """构造出的 DSF / DFF 必须能被 ffmpeg 正确识别。"""
    path = os.path.join(str(tmp_path), "master" + suffix)
    writer(path, seconds=4.0)

    params = probe_audio(path, _FFPROBE)
    assert params is not None, "ffprobe 无法解析构造的 DSD 容器"
    assert params["codec"].startswith("dsd")
    assert params["sample_rate"] == _DSD64_DECODED
    assert params["channels"] == 2
    # 4 秒 DSD64 → 约 4 秒（受 4096 字节块对齐影响略有出入）
    assert 3.9 <= params["duration"] <= 4.1


# ---------------------------------------------------------------------------
# 真实文件：分轨链路
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("writer,suffix", [(write_dsf, ".dsf"), (write_dff, ".dff")])
def test_dsd_split_succeeds(tmp_path, writer, suffix):
    """DSD 分轨必须成功，且每首时长精确。

    回归防护：分轨链路过去没有做降采样，ffmpeg 报
    "2822400 Hz not supported"，DSD 源分轨 100% 失败。
    """
    d = str(tmp_path)
    src = os.path.join(d, "master" + suffix)
    writer(src, seconds=12.0)
    cue_path = _write_cue(d, "master" + suffix, [0.0, 4.0, 8.0])

    sheet = parse_cue(cue_path)
    out = os.path.join(d, "out")
    logs = []
    results = split(sheet, out, verify=True, on_progress=logs.append)

    assert len(results) == 3, "应生成 3 首\n" + "".join(logs)

    for i, path in enumerate(results):
        assert os.path.isfile(path)
        params = probe_audio(path, _FFPROBE)
        assert params["codec"] == "flac"
        # 目标采样率必须是档位表规定的值
        assert params["sample_rate"] == 88200, f"第 {i+1} 首采样率不符"
        # 位深应为 24（s32 承载 24 位有效数据）
        assert params["bits"] == 24, f"第 {i+1} 首位深不符"
        assert params["channels"] == 2

    # 每首时长必须精确到 4 秒（末首受块对齐影响略短）
    durations = [probe_audio(p, _FFPROBE)["duration"] for p in results]
    assert abs(durations[0] - 4.0) < 0.05, f"第 1 首时长偏差过大: {durations[0]}"
    assert abs(durations[1] - 4.0) < 0.05, f"第 2 首时长偏差过大: {durations[1]}"
    assert 3.9 <= durations[2] <= 4.1, f"第 3 首时长偏差过大: {durations[2]}"

    # 结论必须标注为有损转换，且不得出现失败
    text = "".join(logs)
    assert "有损转换" in text
    assert "未通过    ：0" in text, "DSD 分轨不应被判为校验失败\n" + text


def test_dsd_split_does_not_leak_raw_rate(tmp_path):
    """输出采样率不得是 DSD 原始解码率（352800）——那会被 FLAC 编码器拒绝。"""
    d = str(tmp_path)
    src = os.path.join(d, "master.dsf")
    write_dsf(src, seconds=4.0)
    cue_path = _write_cue(d, "master.dsf", [0.0, 2.0])

    sheet = parse_cue(cue_path)
    out = os.path.join(d, "out")
    results = split(sheet, out, verify=False)

    for path in results:
        rate = probe_audio(path, _FFPROBE)["sample_rate"]
        assert rate != _DSD64_DECODED, "输出仍是 DSD 原始解码率"
        assert rate == 88200


# ---------------------------------------------------------------------------
# 真实文件：直转链路
# ---------------------------------------------------------------------------


def test_dsd_direct_convert_uses_correct_target(tmp_path):
    """直转 DSD 时目标采样率必须来自正确的档位查表。"""
    from cue2flac.convert import convert_file

    d = str(tmp_path)
    src = os.path.join(d, "master.dsf")
    write_dsf(src, seconds=4.0)
    out = os.path.join(d, "out")

    res = convert_file(src, out, verify=True)
    assert res.ok, res.errors
    assert res.out_params["sample_rate"] == 88200
    assert "并非逐样本无损" in res.note
    assert not res.lossless


def test_dsd_rate_matches_target_table(tmp_path):
    """探测到的解码率必须能命中档位表（这是查表落空的回归防护）。"""
    d = str(tmp_path)
    src = os.path.join(d, "master.dsf")
    write_dsf(src, seconds=4.0)

    rate = dsd.dsd_rate(src, _FFPROBE)
    assert rate == _DSD64_DECODED
    assert rate in DSD_TARGETS, "探测到的解码率必须能命中档位表"
