"""splitter 单元测试：聚焦 DSD 检测等不依赖 ffmpeg 的逻辑。"""

import subprocess
from unittest import mock

from cue2flac.splitter import _is_dsd


def test_is_dsd_by_extension():
    # 仅凭扩展名即可判定，无需调用 ffprobe
    assert _is_dsd("/music/album.dsf", "ffprobe") is True
    assert _is_dsd("/music/album.DSF", "ffprobe") is True
    assert _is_dsd("/music/album.dff", "ffprobe") is True
    assert _is_dsd("/music/album.flac", "ffprobe") is False
    assert _is_dsd("/music/album.wav", "ffprobe") is False
    assert _is_dsd("/music/album.ape", "ffprobe") is False


def test_is_dsd_by_codec():
    # 扩展名非 DSD 时，回落到 ffprobe 的 codec 判断
    with mock.patch("cue2flac.splitter.subprocess.run") as run:
        run.return_value = mock.Mock(
            stdout="dsd\n", returncode=0, stderr=""
        )
        assert _is_dsd("/music/album.ext", "ffprobe") is True

        run.return_value = mock.Mock(
            stdout="flac\n", returncode=0, stderr=""
        )
        assert _is_dsd("/music/album.ext", "ffprobe") is False

        # ffprobe 异常时安全回退为 False
        run.side_effect = subprocess.SubprocessError("boom")
        assert _is_dsd("/music/album.ext", "ffprobe") is False


if __name__ == "__main__":
    test_is_dsd_by_extension()
    test_is_dsd_by_codec()
    print("All splitter tests passed.")
