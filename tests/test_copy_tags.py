"""_copy_tags 的 ffmetadata 健壮性测试（纯逻辑，不依赖真实 ffmpeg）。

第 4 批 P2-5：验证文件头只写一次、键值转义、非法键跳过、
ffmpeg 失败时不覆盖输出、异常被吞但写 debug 日志、复用调用方的探测结果。
"""

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from cue2flac import convert as convert_mod


def _proc(rc=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


def test_ffmetadata_lines_escape_values_and_skip_bad_keys():
    lines = convert_mod._ffmetadata_lines({
        "title": "中文标题",
        "artist": "A=B;C#D\\E",
        "comment": "第一行\r\n第二行\r第三行",
        "empty": "",
        "": "坏键",
        "bad=key": "坏键",
        "bad\nkey": "坏键",
        "bad#key": "坏键",
    })
    assert lines[0] == "title=中文标题"
    assert lines[1] == r"artist=A\=B\;C\#D\\E"
    assert lines[2] == "comment=第一行\\\n第二行\\\n第三行"
    assert len(lines) == 3, "含特殊字符 / 控制字符 / 空的键必须被跳过"
    assert not any(line.startswith(";FFMETADATA") for line in lines), \
        "文件头由调用方在循环外只写一次"


def test_copy_tags_writes_single_header_and_replaces_output(monkeypatch, tmp_path):
    dst = tmp_path / "out.flac"
    dst.write_bytes(b"ORIGINAL")
    seen = {}

    def fake_run(cmd, **kw):
        metas = [c for c in cmd if str(c).endswith(".ffmeta")]
        assert len(metas) == 1
        seen["meta"] = Path(metas[0]).read_text(encoding="utf-8")
        seen["raw"] = Path(metas[0]).read_bytes()
        (tmp_path / "out.flac.tmp.flac").write_bytes(b"TAGGED")
        return _proc(0)

    monkeypatch.setattr(convert_mod, "subprocess", SimpleNamespace(run=fake_run))
    convert_mod._copy_tags(
        "src.flac", str(dst), "ffmpeg",
        {"title": "中文", "artist": "A;B", "album": "X",
         "comment": "第一行\n第二行"},
    )
    assert seen["meta"].startswith(";FFMETADATA1\n")
    assert seen["meta"].count(";FFMETADATA1") == 1, "每个 tag 前重复写头会破坏后续行"
    assert "artist=A\\;B" in seen["meta"]
    # 回归：Windows 文本模式把 \n 写成 \r\n 时，ffmetadata 的
    # 「反斜杠 + 换行」会变成「反斜杠 + \r」，实测标签值会被截断成 '第一行\r'。
    assert b"\r\n" not in seen["raw"], "ffmetadata 必须使用 LF 行尾"
    assert b"\\\n" in seen["raw"], "换行应写成反斜杠 + LF（与 FFmpeg 导出一致）"
    assert dst.read_bytes() == b"TAGGED"
    assert not (tmp_path / "out.flac.tmp.flac").exists(), "临时文件应被清理"


def test_copy_tags_keeps_original_output_when_ffmpeg_fails(monkeypatch, tmp_path):
    dst = tmp_path / "out.flac"
    dst.write_bytes(b"ORIGINAL")

    def fake_run(cmd, **kw):
        # 模拟 ffmpeg 失败却留下一个损坏的临时文件
        (tmp_path / "out.flac.tmp.flac").write_bytes(b"BROKEN")
        return _proc(1, stderr="Invalid data found")

    monkeypatch.setattr(convert_mod, "subprocess", SimpleNamespace(run=fake_run))
    convert_mod._copy_tags("src.flac", str(dst), "ffmpeg", {"title": "T"})
    assert dst.read_bytes() == b"ORIGINAL", "失败时不得用坏文件覆盖已生成的 FLAC"
    assert not (tmp_path / "out.flac.tmp.flac").exists()


def test_copy_tags_reuses_caller_probe_results(monkeypatch, tmp_path):
    dst = tmp_path / "out.flac"
    dst.write_bytes(b"ORIGINAL")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if str(cmd[0]) == "my-ffprobe":
            return _proc(0, stdout=json.dumps({"format": {"tags": {"title": "T"}}}))
        (tmp_path / "out.flac.tmp.flac").write_bytes(b"TAGGED")
        return _proc(0)

    monkeypatch.setattr(convert_mod, "subprocess", SimpleNamespace(run=fake_run))
    monkeypatch.setattr(
        convert_mod, "resolve_tool",
        lambda name: pytest.fail("传入 ffprobe 后不应重新定位工具"),
    )
    # tags=None 但 ffprobe 已定位：不再额外启动探测 / 不再 resolve_tool
    convert_mod._copy_tags("src.flac", str(dst), "ffmpeg", None, "my-ffprobe")
    assert calls[0][0] == "my-ffprobe"
    assert calls[0][-1] == "src.flac"
    assert dst.read_bytes() == b"TAGGED"


def test_copy_tags_skips_ffmpeg_when_no_valid_tags(monkeypatch):
    monkeypatch.setattr(
        convert_mod, "subprocess",
        SimpleNamespace(run=lambda *a, **k: pytest.fail("无有效标签时不应启动 ffmpeg")),
    )
    convert_mod._copy_tags(
        "src.flac", "out.flac", "ffmpeg", {"bad=key": "v", "empty": ""}
    )


def test_copy_tags_never_raises_and_logs_failure(monkeypatch, tmp_path, caplog):
    dst = tmp_path / "out.flac"
    dst.write_bytes(b"ORIGINAL")

    def boom(*a, **k):
        raise OSError("boom")

    monkeypatch.setattr(convert_mod, "subprocess", SimpleNamespace(run=boom))
    with caplog.at_level(logging.DEBUG, logger="cue2flac.convert"):
        convert_mod._copy_tags("src.flac", str(dst), "ffmpeg", {"title": "T"})
    assert dst.read_bytes() == b"ORIGINAL"
    assert "复制元数据失败" in caplog.text, "静默吞异常应改为至少写 debug 日志"
