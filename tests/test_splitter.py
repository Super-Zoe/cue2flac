"""splitter 单元测试：聚焦 DSD 检测等不依赖 ffmpeg 的逻辑。

第 4 批（P1-2）后，DSD 判定已统一到 :func:`cue2flac.dsd.is_dsd`，
``splitter._is_dsd`` 是保留同名的兼容别名。因此这里伪造的是统一实现
依赖的 ``probe_audio`` 结果，而不是旧版 splitter 自拼 ffprobe 命令时
``-of default=nw=1:nk=1`` 输出的裸 codec 文本。
"""

import subprocess
from unittest import mock

from cue2flac import dsd as dsd_mod
from cue2flac.splitter import _is_dsd


def test_is_dsd_by_extension():
    # .dsf/.dff 仅凭扩展名即可判定（含大写），不应启动 ffprobe；
    # 其它扩展名仍需回落到 codec 探测。
    with mock.patch.object(dsd_mod, "probe_audio", return_value=None) as probe:
        assert _is_dsd("/music/album.dsf", "ffprobe") is True
        assert _is_dsd("/music/album.DSF", "ffprobe") is True
        assert _is_dsd("/music/album.dff", "ffprobe") is True
        assert _is_dsd("/music/album.flac", "ffprobe") is False
        assert _is_dsd("/music/album.wav", "ffprobe") is False
        assert _is_dsd("/music/album.ape", "ffprobe") is False
        assert probe.call_count == 3, "扩展名短路时不应启动 ffprobe"


def test_is_dsd_by_codec():
    # 扩展名非 DSD 时，回落到 ffprobe 的 codec 判断（统一口径：dsd 前缀）
    with mock.patch.object(dsd_mod, "probe_audio") as probe:
        probe.return_value = {"codec": "dsd_lsbf"}
        assert _is_dsd("/music/album.ext", "ffprobe") is True

        probe.return_value = {"codec": "dsd_msbf_planar"}
        assert _is_dsd("/music/album.ext", "ffprobe") is True

        probe.return_value = {"codec": "flac"}
        assert _is_dsd("/music/album.ext", "ffprobe") is False

        probe.return_value = {"codec": "pcm_s16le"}
        assert _is_dsd("/music/album.ext", "ffprobe") is False

        # 探测失败（无结果 / 抛异常）时安全回退为 False
        probe.return_value = None
        assert _is_dsd("/music/album.ext", "ffprobe") is False

        probe.side_effect = subprocess.SubprocessError("boom")
        assert _is_dsd("/music/album.ext", "ffprobe") is False


def test_is_dsd_reuses_probed_params():
    # 传入已有探测结果时不再启动 ffprobe（供分轨链路后续复用）
    with mock.patch.object(dsd_mod, "probe_audio") as probe:
        assert _is_dsd("/music/a.ext", "ffprobe", {"codec": "dsd_msbf"}) is True
        assert _is_dsd("/music/a.ext", "ffprobe", {"codec": "flac"}) is False
        probe.assert_not_called()


def test_splitter_is_dsd_delegates_to_shared_implementation():
    # 兼容别名必须真的转发，避免分轨链路又长出一判定实现
    with mock.patch("cue2flac.splitter._is_dsd_shared", return_value=True) as shared:
        assert _is_dsd("/music/a.ext", "ffprobe") is True
        shared.assert_called_once_with("/music/a.ext", "ffprobe", None)


if __name__ == "__main__":
    test_is_dsd_by_extension()
    test_is_dsd_by_codec()
    test_is_dsd_reuses_probed_params()
    test_splitter_is_dsd_delegates_to_shared_implementation()
    print("All splitter tests passed.")
