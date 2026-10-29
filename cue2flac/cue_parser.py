"""cue2flac —— 根据 CUE 分轨文件把整轨无损音频拆分为多首独立 FLAC。

本模块负责把 CUE 文本解析成结构化的 CueSheet / Track 对象。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

# CDDA 红皮书标准：每秒 75 帧
CD_FRAMES_PER_SEC = 75


@dataclass
class Track:
    """一条曲目。"""

    number: int
    title: Optional[str] = None
    performer: Optional[str] = None
    start: float = 0.0  # 起点（秒，来自 INDEX 01）
    end: Optional[float] = None  # 终点（秒）；None 表示到文件末尾
    file: Optional[str] = None  # 解析后的音频文件绝对路径
    pregap: Optional[float] = None  # INDEX 00 / PREGAP 起点


@dataclass
class CueSheet:
    """整张唱片的 CUE 信息。"""

    album_title: Optional[str] = None
    album_performer: Optional[str] = None
    genre: Optional[str] = None
    date: Optional[str] = None
    comment: Optional[str] = None
    tracks: List[Track] = field(default_factory=list)

    @property
    def total_tracks(self) -> int:
        return len(self.tracks)


def _decode(path: str) -> str:
    """读取 CUE 文件，依次尝试多种编码（应对中文/日文/西欧字符）。"""
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-8", "gbk", "shift_jis", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _parse_time(s: str) -> float:
    """解析时间码 MM:SS:FF（或 MM:SS）为秒。

    FF 为帧，按 75 帧/秒换算。
    """
    s = s.strip().strip('"')
    parts = s.split(":")
    try:
        if len(parts) == 3:
            m, sec, fr = (int(x) for x in parts)
            return m * 60 + sec + fr / CD_FRAMES_PER_SEC
        if len(parts) == 2:
            m, sec = (int(x) for x in parts)
            return m * 60 + sec
    except ValueError:
        pass
    raise ValueError(f"无法解析时间码: {s!r}")


_QUOTED = re.compile(r'^"(.*)"$')


def _unquote(s: str) -> str:
    s = s.strip()
    m = _QUOTED.match(s)
    if m:
        return m.group(1)
    return s


def _resolve_audio(fname: str, cue_dir: str, hint: Optional[str]) -> str:
    """在多处尝试定位音频文件，返回绝对路径（找不到时仍返回基于 CUE 目录的推测路径）。"""
    candidates: List[str] = []
    if hint:
        candidates.append(hint)
    candidates.append(os.path.join(cue_dir, fname))
    candidates.append(fname)
    base, _ext = os.path.splitext(fname)
    for ext in (".flac", ".wav", ".wv", ".ape", ".tta", ".tak", ".m4a", ""):
        candidates.append(os.path.join(cue_dir, base + ext))
    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return os.path.abspath(os.path.join(cue_dir, fname))


def parse_cue(path: str, audio_hint: Optional[str] = None) -> CueSheet:
    """解析 CUE 文件为 CueSheet。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"找不到 CUE 文件: {path}")
    text = _decode(path)
    cue_dir = os.path.dirname(os.path.abspath(path))
    sheet = CueSheet()

    current_file: Optional[str] = None
    current_track: Optional[Track] = None
    in_track = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        up = line.upper()
        if up.startswith("REM"):
            m = re.match(r"REM\s+(\S+)\s+(.*)$", line, re.IGNORECASE)
            if m:
                key, val = m.group(1).upper(), _unquote(m.group(2))
                if key == "GENRE":
                    sheet.genre = val
                elif key == "DATE":
                    sheet.date = val
                elif key == "COMMENT":
                    sheet.comment = val
            continue

        if up.startswith("PERFORMER"):
            val = _unquote(line[len("PERFORMER") :].strip())
            if in_track and current_track is not None:
                current_track.performer = val
            else:
                sheet.album_performer = val
            continue

        if up.startswith("TITLE"):
            val = _unquote(line[len("TITLE") :].strip())
            if in_track and current_track is not None:
                current_track.title = val
            else:
                sheet.album_title = val
            continue

        if up.startswith("FILE"):
            m = (
                re.match(r'FILE\s+"([^"]+)"', line)
                or re.match(r"FILE\s+'([^']+)'", line)
                or re.match(r"FILE\s+(\S+)", line)
            )
            fname = m.group(1) if m else None
            if fname:
                current_file = _resolve_audio(fname, cue_dir, audio_hint)
            in_track = False
            current_track = None
            continue

        if up.startswith("TRACK"):
            m = re.match(r"TRACK\s+(\d+)", line, re.IGNORECASE)
            num = int(m.group(1)) if m else (len(sheet.tracks) + 1)
            current_track = Track(number=num, file=current_file)
            sheet.tracks.append(current_track)
            in_track = True
            continue

        if up.startswith("INDEX"):
            m = re.match(r"INDEX\s+(\d+)\s+(\S+)", line, re.IGNORECASE)
            if m and current_track is not None:
                idx = int(m.group(1))
                t = _parse_time(m.group(2))
                if idx == 1:
                    current_track.start = t
                elif idx == 0:  # pregap
                    current_track.pregap = t
                    if current_track.start == 0.0:
                        current_track.start = t
            continue

        if up.startswith("PREGAP"):
            m = re.match(r"PREGAP\s+(\S+)", line, re.IGNORECASE)
            if m and current_track is not None:
                current_track.pregap = _parse_time(m.group(1))
            continue
        # FLAGS / ISRC / CATALOG / POSTGAP 等：忽略

    # 用专辑级信息补全缺失的曲目信息
    for tr in sheet.tracks:
        if not tr.performer:
            tr.performer = sheet.album_performer
        if not tr.title:
            tr.title = f"Track {tr.number:02d}"

    _assign_ends(sheet)

    if not sheet.tracks:
        raise ValueError("CUE 中未发现任何 TRACK，无法分轨。")

    return sheet


def _assign_ends(sheet: CueSheet) -> None:
    """为每条曲目计算终点：取同文件下一条曲目的起点。"""
    for i, tr in enumerate(sheet.tracks):
        nxt = None
        for j in range(i + 1, len(sheet.tracks)):
            if sheet.tracks[j].file == tr.file:
                nxt = sheet.tracks[j]
                break
        tr.end = nxt.start if nxt is not None else None
