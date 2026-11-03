"""测试夹具：生成真实的 DSF / DFF(DSDIFF) 容器。

ffmpeg 没有 DSF/DFF 封装器（只有解复用器），因此测试要覆盖 DSD 路径
就必须自己按格式规范写出合法容器。字段布局对齐 ffmpeg 源码：

- ``libavformat/dsfdec.c``
  - ``'DSD '`` + u64(28) + u64(总长) + u64(id3 位置)
  - ``'fmt '`` + u64(52) + 40 字节 fmt 数据（chunk size 含 12 字节头）
  - ``'data'`` + u64(数据长 + 12) + 数据
  - 采样率字段被 ffmpeg **除以 8**：容器写 2822400 → 解码输出 352800 Hz
- ``libavformat/iff.c``
  - ``'FRM8'`` + u64 + ``'DSD '`` + 各级 chunk（长度一律大端 64 位）
  - ``CMPR`` 的 codec tag 必须是 ``'DSD '``
  - ``CHNL`` 每通道 4 字节布局 ID；``FS`` 采样率同样被除以 8

数据为确定性的 1-bit 图案（每 4096 字节一块、按声道 planar 交织），
因此同一输入必然产生同一输出，可用于逐字节比对。
"""

from __future__ import annotations

import struct

#: 每声道每块字节数（DSF / DSDIFF 规范值）。
BLOCK = 4096

#: DSD64 采样率。
DSRATE = 2822400


def _blocks(nbytes_per_ch: int, channels: int) -> bytes:
    """按 planar 交织生成 DSD 数据块：block0-ch0, block0-ch1, block1-ch0 …"""
    nblocks = (nbytes_per_ch + BLOCK - 1) // BLOCK
    out = bytearray()
    for b in range(nblocks):
        for c in range(channels):
            out += bytes([0x69 if ((b + c) % 2 == 0) else 0x96]) * BLOCK
    return bytes(out)


def _plan(seconds: float, rate: int) -> tuple:
    """算出 (总采样数, 每声道字节数, 数据块)。"""
    nblocks = max(1, int(seconds * rate) // (8 * BLOCK))
    total_samples = nblocks * 8 * BLOCK
    per_ch_bytes = total_samples // 8
    return total_samples, per_ch_bytes, _blocks(per_ch_bytes, 2)


def write_dsf(path: str, seconds: float = 4.0, rate: int = DSRATE,
              channels: int = 2) -> None:
    """写 DSF（DSD Stream File）。"""
    total_samples, per_ch_bytes, payload = _plan(seconds, rate)

    # fmt 数据恰好 40 字节（chunk size 字段 52 = 12 字节头 + 40 字节数据）
    fmt = struct.pack(
        "<IIIIIIQI",
        1,                                   # format version
        0,                                   # format id: 0 = DSD raw
        2 if channels == 2 else 1,           # channel type (1=mono, 2=stereo)
        channels,
        rate,                                # sampling frequency（ffmpeg 会除以 8）
        1,                                   # bits per sample: 1 = LSBF planar
        total_samples,                       # sample count
        BLOCK,                               # block size per channel
    ) + b"\x00" * 4                         # reserved（ffmpeg 会 skip 4 字节）

    data_hdr = struct.pack("<Q", len(payload) + 12)
    total = 12 + 16 + 12 + 52 + 12 + len(payload)
    dsd_hdr = struct.pack("<QQ", total, 0)

    with open(path, "wb") as f:
        f.write(b"DSD " + struct.pack("<Q", 28) + dsd_hdr)
        f.write(b"fmt " + struct.pack("<Q", 52) + fmt)
        f.write(b"data" + data_hdr + payload)


def write_dff(path: str, seconds: float = 4.0, rate: int = DSRATE,
              channels: int = 2) -> None:
    """写 DFF / DSDIFF（FRM8 容器，chunk 长度一律大端 64 位）。"""
    total_samples, per_ch_bytes, payload = _plan(seconds, rate)

    def chunk(cid: bytes, data: bytes) -> bytes:
        out = cid + struct.pack(">Q", len(data)) + data
        if len(data) & 1:          # 块长度需偶数对齐
            out += b"\x00"
        return out

    fver = chunk(b"FVER", struct.pack(">I", 0x01050000))
    fs = chunk(b"FS  ", struct.pack(">I", rate))
    # CHNL：2 字节通道数 + 每通道 4 字节布局 ID
    chnl = chunk(b"CHNL", struct.pack(">H", channels)
                 + b"".join(struct.pack("<I", 0x00000001 if i == 0 else 0x00000002)
                            for i in range(channels)))
    # CMPR：4 字节 codec tag 必须是 'DSD '（ffmpeg 用它查 dsd_codec_tags）
    cmpr = chunk(b"CMPR", b"DSD ")
    prop = chunk(b"PROP", b"SND " + fs + chnl + cmpr)
    dsd = chunk(b"DSD ", payload)

    body = fver + prop + dsd
    frm8 = b"FRM8" + struct.pack(">Q", len(body) + 4) + b"DSD " + body
    with open(path, "wb") as f:
        f.write(frm8)
