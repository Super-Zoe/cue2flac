"""回归测试：中文 Windows 场景下的「中文路径 / 中文文件名」处理。

除已有的「含中文标签」用例（``test_subprocess_encoding.py``）之外，这里补测
**源文件 / 输出目录 / 输出文件名本身含中文** 这一更常见的真实场景：

* 中文 Windows 用户的音乐库常在 ``C:\\用户\\中文名\\音乐\\...`` 下，
  源与输出都带中文；
* 子进程按 UTF-8 传参、临时 ffmetadata 文件用 LF 行尾、路径拼接与
  ``os.makedirs`` 都不能因非 ASCII 字符而出错。

覆盖两个层面：
1. **真实 ffmpeg 端到端**（``skipif`` 无可用 ffmpeg）——在中文字目录里跑完整
   转换，直接验证中文路径在真实子进程调用下不崩（恰是中文 Windows 的痛点）；
2. **mock 子进程端到端**（始终执行）——在没有 ffmpeg 的环境里，保证中文路径
   在 Python 层（目录创建、路径拼接、ffmetadata 写出、``os.replace``）不崩，
   并且子进程确实收到了带中文的完整命令行参数。
"""
import io
import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from cue2flac.convert import convert_file, _copy_tags          # noqa: E402
from cue2flac.ffmpeg_provider import resolve_tool              # noqa: E402


# ---------------------------------------------------------------------------
# 可用 ffmpeg 探测：必须能真正执行才算可用，避免 Linux 上把 .exe 误判为可用
# ---------------------------------------------------------------------------
def _tool_available(path: str) -> bool:
    try:
        proc = subprocess.run([path, "-version"], capture_output=True, timeout=20)
        return proc.returncode == 0
    except Exception:
        return False


def _find(name: str):
    for cand in (
        os.path.join(ROOT, "ffmpeg", f"{name}.exe"),
        os.path.join(ROOT, "ffmpeg", name),
        shutil.which(name) or "",
    ):
        if cand and os.path.isfile(cand) and _tool_available(cand):
            return cand
    return None


_FF = _find("ffmpeg")
_FP = _find("ffprobe")
_HAVE_FFMPEG = bool(_FF and _FP)


# ===========================================================================
# 固定探测结果：含中文标签，中文路径场景同样要能把这些标签复制过去
# ===========================================================================
_PROBE_JSON = json.dumps({
    "streams": [{
        "codec_type": "audio",
        "sample_rate": "44100",
        "bits_per_raw_sample": "16",
        "bits_per_sample": "16",
        "channels": "2",
        "codec_name": "flac",
        "duration": "2.000000",
    }],
    "format": {
        "tags": {
            "title": "中文=标题;A#B",
            "artist": "歌手\\名字",
            "album": "中文专辑",
        }
    },
})

#: 固定的 s32le 裸 PCM，源与输出都解码出相同字节 → 判定逐字节无损。
_PCM = b"\x00" * (44100 * 2 * 4 * 2)


# ---------------------------------------------------------------------------
# 子进程 mock：伪造 ffmpeg / ffprobe 的行为，重点验证「中文路径」在 Python 层
# 不被破坏，而非真正解码音频。
# ---------------------------------------------------------------------------
_RECORDED = []


class _FakePopen:
    """模拟 ``subprocess.Popen``：向 stdout 写固定 PCM，stderr 留空。

    ``_decode_pcm`` 从 stdout 流式读 PCM 求 MD5，并从 stderr 解析解码后参数；
    这里 stdout 给固定字节（源/输出一致 → 无损），stderr 留空 → 解码后参数
    回退到 ffprobe 的容器参数，长度比对仍然成立。
    """

    def __init__(self, stdout_bytes: bytes):
        self.stdout = io.BytesIO(stdout_bytes)
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def _fake_run(cmd, *args, **kw):
    prog = os.path.basename(str(cmd[0]))
    _RECORDED.append(list(cmd))

    if "ffprobe" in prog:
        return subprocess.CompletedProcess(cmd, 0, stdout=_PROBE_JSON, stderr="")

    # ---- ffmpeg ----
    if "-map_metadata" in cmd or ("-f" in cmd and "s32le" in cmd):
        # 元数据回写（或 pcm_bytes 变体）：把第一个 -i 的输入复制到最后一个参数
        if "-f" in cmd and "s32le" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        src = cmd[cmd.index("-i") + 1]
        out = cmd[-1]
        if os.path.isfile(src):
            with open(src, "rb") as f:
                data = f.read()
        else:
            data = b"dummy"
        with open(out, "wb") as f:
            f.write(data)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    # 主编码：把第一个 -i 的输入（源）复制到输出（最后一个参数）
    src = cmd[cmd.index("-i") + 1]
    out = cmd[-1]
    if os.path.isfile(src):
        with open(src, "rb") as f:
            data = f.read()
    else:
        data = b"dummy"
    with open(out, "wb") as f:
        f.write(data)
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


# ---------------------------------------------------------------------------
# 辅助：读取真实 flac 的标签（仅真实 ffmpeg 用例使用）
# ---------------------------------------------------------------------------
def _read_tags(p: str, ffprobe: str) -> dict:
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format_tags",
         "-of", "json", p], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    ).stdout
    return {k.lower(): v for k, v in
            (json.loads(out).get("format", {}) or {}).get("tags", {}).items()
            if k.lower() != "encoder"}


def _gen_tone_with_tags(dst_wav, dst_flac, ff, tags):
    subprocess.run(
        [ff, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", dst_wav],
        check=True, capture_output=True,
    )
    cmd = [ff, "-y", "-i", dst_wav, "-c:a", "flac"]
    for k, v in tags.items():
        cmd += ["-metadata", f"{k}={v}"]
    cmd.append(dst_flac)
    subprocess.run(cmd, check=True, capture_output=True)


# ===========================================================================
# 用例 1：真实 ffmpeg 端到端（中文目录 + 中文文件名 + 中文标签）
# ===========================================================================
@pytest.mark.skipif(not _HAVE_FFMPEG, reason="需要可执行的 ffmpeg/ffprobe")
def test_real_ffmpeg_chinese_paths_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "cue2flac.ffmpeg_provider.resolve_tool",
        lambda n: _FP if n == "ffprobe" else _FF,
    )

    # 中文目录 + 中文源文件名 + 中文输出目录
    music = tmp_path / "音乐库" / "陈奕迅精选"
    music.mkdir(parents=True)
    tone = music / "tone.wav"
    src = music / "第一首歌.flac"
    out_dir = tmp_path / "输出目录" / "中文专辑"

    _gen_tone_with_tags(
        str(tone), str(src), _FF,
        {"title": "中文标题", "artist": "歌手名字", "album": "中文专辑名"},
    )

    r = convert_file(str(src), out_dir=str(out_dir), verify=True)
    assert r.ok, r.errors
    assert r.lossless, r.status
    assert r.out_path and os.path.isfile(r.out_path)

    # 输出位于中文目录，文件名由中文源名派生
    assert "中文专辑" in r.out_path
    assert os.path.basename(r.out_path).startswith("第一首歌")

    # 中文标签应被完整复制（与含中文标签的回归用例互为补充）
    out_tags = _read_tags(str(r.out_path), _FP)
    assert out_tags.get("title") == "中文标题"
    assert out_tags.get("artist") == "歌手名字"


# ===========================================================================
# 用例 2：mock 子进程端到端（始终执行，验证中文路径在 Python 层不崩）
# ===========================================================================
def test_mock_chinese_paths_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: _FakePopen(_PCM)
    )
    _RECORDED.clear()

    music = tmp_path / "音乐库" / "陈奕迅精选"
    music.mkdir(parents=True)
    src = music / "第一首歌.flac"
    # 需要一个真实存在的源文件（_fake_run 会把它当作输入复制）
    src.write_bytes(b"fake-flac-bytes")
    out_dir = tmp_path / "输出目录" / "中文专辑"

    r = convert_file(str(src), out_dir=str(out_dir), verify=True)
    assert r.ok, r.errors
    assert r.lossless, r.status
    assert r.out_path and os.path.isfile(r.out_path)
    assert "中文专辑" in r.out_path
    assert os.path.basename(r.out_path).startswith("第一首歌")

    # 子进程确实收到了带中文的完整命令行（证明路径没有被破坏 / 编码错误）
    joined = [" ".join(c) for c in _RECORDED]
    assert any("中文专辑" in line for line in joined), (
        "子进程命令行未包含中文输出目录：" + "\n".join(joined)
    )
    assert any("第一首歌" in line for line in joined)


# ===========================================================================
# 用例 3：_copy_tags 直接处理中文 dst / src（ffmetadata 写出 + os.replace）
# ===========================================================================
def test_copy_tags_with_chinese_dst_and_src(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run)
    _RECORDED.clear()

    out_dir = tmp_path / "输出目录" / "子目录"
    out_dir.mkdir(parents=True)
    src = tmp_path / "源文件" / "原始.flac"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x")
    dst = out_dir / "结果.flac"
    dst.write_bytes(b"orig")

    tags = {"title": "中文=标题;A#B", "artist": "歌手\\名字", "album": "中文专辑"}
    # 直接传入 tags：_copy_tags 只发起一次 ffmpeg（元数据回写），
    # 但因为命令行带中文，仍会走到中文路径拼接 / ffmetadata 临时文件写出。
    _copy_tags(str(src), str(dst), resolve_tool("ffmpeg"),
               tags, resolve_tool("ffprobe"))

    # 回写成功：dst 仍存在（os.replace 完成），且 tmp 已被清理
    assert dst.is_file()
    tmp_leftover = dst.with_suffix(".flac.tmp.flac")
    assert not tmp_leftover.exists()

    # 至少一次子进程调用包含了中文输出目录路径
    joined = [" ".join(c) for c in _RECORDED]
    assert any("输出目录" in line for line in joined), (
        "元数据回写子进程未包含中文路径：" + "\n".join(joined)
    )


# ===========================================================================
# 用例 4：纯函数单测——文件名清洗 / 重名后缀在中文下行为正确
# ===========================================================================
def test_sanitize_and_unique_with_chinese():
    from cue2flac._util import sanitize_name, unique_name

    # 纯中文合法名不受影响
    assert sanitize_name("中文 文件名") == "中文 文件名"
    # 非法字符被替换为下划线（与既有规则一致）
    assert "/" not in sanitize_name("a/b:c*?")
    # 中文路径下重名后缀加在扩展名前（与 convert._unique_path 的用法一致）
    used: dict = {}
    p1 = unique_name("/目录/中文.flac", used, in_stem=True)
    assert p1 == "/目录/中文.flac"
    p2 = unique_name("/目录/中文.flac", used, in_stem=True)
    assert p2 == "/目录/中文_1.flac"
