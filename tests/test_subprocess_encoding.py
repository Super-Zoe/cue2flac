"""回归测试：修复 PyInstaller 在中文 Windows(cp936) 下 text 模式解码崩溃。

历史缺陷：``subprocess.run(text=True)`` 不显式指定 encoding 时，后台读取
线程按系统区域(cp936) 解码 ffprobe/ffmpeg 输出的 UTF-8 JSON；遇到中文标签
就抛 ``UnicodeDecodeError``，且该异常发生在子线程、主线程 try 抓不到，
最终 ``CompletedProcess.stdout`` 变成 ``None``，后续 ``out.stdout.strip()``
直接 ``AttributeError`` 崩溃（exe 形态复现，源码模式因默认 UTF-8 不暴露）。

修复：所有 text 模式调用显式 ``encoding="utf-8", errors="replace"``，
并对 ``.stdout`` 访问加 ``or ""`` 兜底。
"""
import ast
import os
import subprocess
import sys

import pytest

_ff = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ffmpeg", "ffmpeg.exe",
)
_ffprobe = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ffmpeg", "ffprobe.exe",
)
_HAVE_FFMPEG = os.path.isfile(_ff) and os.path.isfile(_ffprobe)


def _production_modules():
    root = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cue2flac"
    )
    mods = []
    for name in os.listdir(root):
        if name.endswith(".py") and not name.startswith("__"):
            mods.append(os.path.join(root, name))
    return mods


def test_text_mode_subprocess_always_pins_encoding():
    """所有 ``subprocess.run(text=True)`` 必须显式声明 ``encoding=``。

    防止有人新增或改回不带 encoding 的 text 模式调用，导致 cp936 崩溃回退。
    """
    for path in _production_modules():
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "run"):
                continue
            # 仅检查 subprocess.run（通过模块名或别名判断）
            if isinstance(fn.value, ast.Name) and fn.value.id != "subprocess":
                continue
            has_text = False
            has_encoding = False
            for kw in node.keywords:
                if kw.arg == "text" and getattr(kw.value, "value", False) is True:
                    has_text = True
                if kw.arg == "encoding":
                    has_encoding = True
            if has_text:
                assert has_encoding, (
                    f"{os.path.basename(path)}:{node.lineno} 存在 text=True 但"
                    f"未显式指定 encoding（会在 cp936 下崩溃）"
                )


def _gen_tone_with_tags(dst_wav, dst_flac, tags):
    subprocess.run(
        [_ff, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", dst_wav],
        check=True, capture_output=True,
    )
    cmd = [_ff, "-y", "-i", dst_wav, "-c:a", "flac"]
    for k, v in tags.items():
        cmd += ["-metadata", f"{k}={v}"]
    cmd.append(dst_flac)
    subprocess.run(cmd, check=True, capture_output=True)


def _read_tags(p):
    out = subprocess.run(
        [_ffprobe, "-v", "error", "-show_entries", "format_tags",
         "-of", "json", p], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    ).stdout
    import json
    return {k.lower(): v for k, v in
            (json.loads(out).get("format", {}) or {}).get("tags", {}).items()
            if k.lower() != "encoder"}


def _force_gbk(run):
    """复现缺陷：text 模式无论调用方传什么 encoding，都强制 gbk，
    使后台解码线程在遇到中文标签的 UTF-8 输出时失败、stdout 变 None。"""
    def wrapper(cmd, *args, **kw):
        if kw.get("text"):
            kw["encoding"] = "gbk"
            kw.pop("errors", None)
        return run(cmd, *args, **kw)
    return wrapper


@pytest.mark.skipif(not _HAVE_FFMPEG, reason="需要本地 ffmpeg/ffprobe")
def test_chinese_tags_survive_utf8(tmp_path):
    """默认 UTF-8 环境：含中文/特殊字符标签应被完整复制、转换判无损。"""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from cue2flac.convert import convert_file

    tags = {
        "title": "中文=标题;A#B",
        "artist": "第一行\n第二行",
        "album": "专辑\\名字",
        "comment": "普通备注",
    }
    wav = tmp_path / "tone.wav"
    src = tmp_path / "src.flac"
    _gen_tone_with_tags(str(wav), str(src), tags)
    before = _read_tags(str(src))

    r = convert_file(str(src), out_dir=str(tmp_path / "out"), verify=True)
    assert r.ok, r.errors
    assert r.lossless, r.status
    after = _read_tags(r.out_path)
    diff = {k: (before.get(k), after.get(k))
            for k in sorted(set(before) | set(after))
            if before.get(k) != after.get(k)}
    assert not diff, f"标签复制出现差异: {diff}"


@pytest.mark.skipif(not _HAVE_FFMPEG, reason="需要本地 ffmpeg/ffprobe")
def test_gbk_decode_failure_does_not_crash(monkeypatch, tmp_path):
    """最严格的回归：即使 text 解码在子线程里失败（stdout→None），
    转换主流程也必须成功，不得抛 AttributeError。"""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import cue2flac.convert as convert_mod
    from cue2flac.convert import convert_file

    # 真实 subprocess.run 被所有模块共用，直接 patch 它即可复现子线程解码失败
    monkeypatch.setattr(subprocess, "run", _force_gbk(subprocess.run))
    monkeypatch.setattr(convert_mod, "resolve_tool",
                        lambda n: _ffprobe if n == "ffprobe" else _ff)

    wav = tmp_path / "tone.wav"
    src = tmp_path / "src.flac"
    _gen_tone_with_tags(str(wav), str(src),
                        {"title": "中文标签", "artist": "歌手"})

    # 不应抛 AttributeError('NoneType' object has no attribute 'strip')
    r = convert_file(str(src), out_dir=str(tmp_path / "out"), verify=True)
    assert r.ok, f"转换应仍成功（标签可降级跳过），但实际: {r.errors}"
    assert r.out_path and os.path.isfile(r.out_path)
