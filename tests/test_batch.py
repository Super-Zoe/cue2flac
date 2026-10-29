"""batch 模块单元测试：扫描发现、递归、跳过损坏文件、汇总。

为避免依赖 ffmpeg，这里用 monkeypatch 替换 batch.split，仅验证
「扫描 → 解析 → 路由 → 跳过 → 汇总」的编排逻辑。
"""

import os
import tempfile
from dataclasses import replace

import pytest

from cue2flac import batch
from cue2flac.cue_parser import parse_cue


VALID_CUE = """FILE "audio.wav" WAVE
  TRACK 01 AUDIO
    TITLE "Track One"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Track Two"
    INDEX 01 01:00:00
"""

BROKEN_CUE = """REM this cue has no TRACK
FILE "nope.wav" WAVE
"""


@pytest.fixture
def fake_tree():
    """构造一个含子目录、有效与损坏 CUE 的目录树。"""
    d = tempfile.mkdtemp()
    # 顶层有效
    with open(os.path.join(d, "top.cue"), "w", encoding="utf-8") as f:
        f.write(VALID_CUE)
    # 子目录有效
    sub = os.path.join(d, "sub")
    os.makedirs(sub)
    with open(os.path.join(sub, "album.cue"), "w", encoding="utf-8") as f:
        f.write(VALID_CUE)
    # 子目录损坏（无 TRACK）
    with open(os.path.join(sub, "broken.cue"), "w", encoding="utf-8") as f:
        f.write(BROKEN_CUE)
    return d


def test_discover_cues_recursive(fake_tree):
    cues = batch.discover_cues(fake_tree, recursive=True)
    assert len(cues) == 3
    # 按路径排序
    assert cues == sorted(cues, key=lambda p: p.lower())


def test_discover_cues_no_recursive(fake_tree):
    cues = batch.discover_cues(fake_tree, recursive=False)
    # 仅顶层 1 个
    assert len(cues) == 1
    assert os.path.basename(cues[0]) == "top.cue"


def test_discover_cues_missing_dir():
    with pytest.raises(FileNotFoundError):
        batch.discover_cues("__not_a_real_dir__/xyz")


def test_process_directory_skips_broken(fake_tree, monkeypatch):
    calls = []

    def fake_split(sheet, out_dir, **kwargs):
        calls.append((sheet.album_title, out_dir))
        return [os.path.join(out_dir, "x.flac")]

    monkeypatch.setattr(batch, "split", fake_split)

    result = batch.process_directory(fake_tree, recursive=True)
    # 顶层 + 子目录各 1 个有效；broken 被跳过
    assert result.scanned == 3
    assert result.success_count == 2
    assert result.skip_count == 1
    assert len(calls) == 2
    assert result.scanned == len(result.processed) + len(result.skipped)
    # 跳过项应记录原因且指向 broken.cue
    skipped = result.skipped[0]
    assert skipped.cue.endswith("broken.cue")
    assert "解析失败" in skipped.reason


def test_process_directory_unified_out(fake_tree, monkeypatch):
    captured = []

    def fake_split(sheet, out_dir, **kwargs):
        captured.append(out_dir)
        return ["f.flac"]

    monkeypatch.setattr(batch, "split", fake_split)

    out_root = os.path.join(fake_tree, "ALL")
    result = batch.process_directory(fake_tree, recursive=True, out=out_root)
    # 两个专辑都输出到统一根目录下的子文件夹
    assert all(c.startswith(out_root) for c in captured)
    assert result.success_count == 2


def test_format_summary_contains_counts(fake_tree, monkeypatch):
    monkeypatch.setattr(batch, "split", lambda sheet, out_dir, **k: ["f.flac"])
    result = batch.process_directory(fake_tree, recursive=True)
    text = batch.format_summary(result)
    assert "批量处理汇总" in text
    assert "成功处理" in text
    assert "跳过" in text
    # 成功列表与跳过列表都应出现
    assert os.path.basename("top.cue") in text
    assert os.path.basename("broken.cue") in text


def test_expand_to_cues_mixed_inputs(fake_tree):
    # fake_tree 含：顶层 top.cue（有效）、sub/album.cue（有效）、sub/broken.cue（无 TRACK）
    # 直接给文件夹 + 直接给一个 .cue 文件 + 一个无关文件
    extra_cue = os.path.join(fake_tree, "sub", "album.cue")  # 与文件夹扫描重复，应去重
    noise = os.path.join(fake_tree, "top.cue") + ".bak"      # 非 cue，忽略
    cues = batch.expand_to_cues([fake_tree, extra_cue, noise], recursive=True)
    # 3 个不重复的 .cue，且排序
    assert len(cues) == 3
    assert cues == sorted(cues, key=lambda p: p.lower())
    basenames = sorted(os.path.basename(c) for c in cues)
    assert basenames == ["album.cue", "broken.cue", "top.cue"]


def test_expand_to_cues_no_recursive(fake_tree):
    cues = batch.expand_to_cues([fake_tree], recursive=False)
    assert len(cues) == 1
    assert os.path.basename(cues[0]) == "top.cue"


def test_expand_to_cues_empty():
    assert batch.expand_to_cues([]) == []
    assert batch.expand_to_cues(["/nonexistent/path.cue"]) == []


def test_process_cues_explicit_list(fake_tree, monkeypatch):
    captured = []

    def fake_split(sheet, out_dir, **kwargs):
        captured.append(out_dir)
        return ["a.flac", "b.flac"]

    monkeypatch.setattr(batch, "split", fake_split)
    top = os.path.join(fake_tree, "top.cue")
    result = batch.process_cues([top], copy_cover=False)
    assert result.scanned == 1
    assert result.success_count == 1
    assert result.skip_count == 0
    assert result.processed[0].tracks == 2

