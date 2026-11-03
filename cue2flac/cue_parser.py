"""cue2flac —— 根据 CUE 分轨文件把整轨无损音频拆分为多首独立 FLAC。

本模块负责把 CUE 文本解析成结构化的 CueSheet / Track 对象。
"""

from __future__ import annotations

import difflib
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
    # 解析过程中的提示信息（如音频文件名不一致被自动匹配）
    notes: List[str] = field(default_factory=list)

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

# 常见整轨音频扩展名（用于模糊定位音源时的候选筛选）
_AUDIO_EXTS = (
    ".flac", ".wav", ".wv", ".ape", ".tta", ".tak", ".m4a",
    ".dsf", ".dff", ".aiff", ".aif", ".alac", ".mp3", ".ogg", ".opus",
)

# 扩展名回退顺序：当 CUE 的 FILE 行省略扩展名时依次尝试
_EXT_FALLBACK = (
    ".flac", ".wav", ".wv", ".ape", ".tta", ".tak", ".m4a", ".dsf", ".dff", "",
)


def _unquote(s: str) -> str:
    s = s.strip()
    m = _QUOTED.match(s)
    if m:
        return m.group(1)
    return s


def _norm_stem(stem: str) -> str:
    """把文件名主干归一化：仅保留字母/数字/汉字（去掉空格、标点、连字符）。"""
    return "".join(ch.lower() for ch in stem if ch.isalnum())


def _match_score(want: str, cand_stem: str) -> float:
    """评估候选文件名与 CUE 中 FILE 名���的相似度（0~1）。

    真实场景中 CUE 里的 FILE 行常与磁盘文件名不一致（例如 CUE 写
    ``The Best Moment精选-CD1.ape``，实际文件叫
    ``陈奕迅-The Best Moment精选-CD1.ape``，多出表演者前缀），
    因此需要按「包含关系 + 相似度」做模糊匹配。
    """
    w = _norm_stem(want)
    c = _norm_stem(cand_stem)
    if not w or not c:
        return 0.0
    if w == c:
        return 1.0
    short, long_ = (w, c) if len(w) <= len(c) else (c, w)
    if long_.endswith(short):
        # 候选名以期望名结尾（典型：磁盘文件多了表演者前缀）
        return 0.90 + 0.09 * (len(short) / len(long_))
    if long_.startswith(short):
        return 0.86 + 0.09 * (len(short) / len(long_))
    if short in long_:
        return 0.72 + 0.08 * (len(short) / len(long_))
    return difflib.SequenceMatcher(None, w, c).ratio() * 0.70


def _fuzzy_find_audio(fname: str, cue_dir: str) -> Optional[str]:
    """在 CUE 所在目录中模糊查找最匹配的音频文件，找不到返回 None。"""
    want_base = os.path.splitext(os.path.basename(fname))[0]
    want_ext = os.path.splitext(fname)[1].lower()
    try:
        entries = os.listdir(cue_dir)
    except OSError:
        return None

    cands = [
        e for e in entries
        if os.path.splitext(e)[1].lower() in _AUDIO_EXTS
        and os.path.isfile(os.path.join(cue_dir, e))
    ]
    if not cands:
        return None

    # 目录里只有一个音频文件时直接采用（最常见的单专辑目录情形）
    if len(cands) == 1:
        return os.path.abspath(os.path.join(cue_dir, cands[0]))

    best: Optional[str] = None
    best_score = 0.0
    for e in cands:
        stem = os.path.splitext(e)[0]
        score = _match_score(want_base, stem)
        # 扩展名一致时给予加权，避免误选同目录里的其它格式
        if want_ext and os.path.splitext(e)[1].lower() == want_ext:
            score += 0.03
        if score > best_score:
            best_score = score
            best = e

    if best is not None and best_score >= 0.60:
        return os.path.abspath(os.path.join(cue_dir, best))
    return None


def _resolve_audio(
    fname: str,
    cue_dir: str,
    hint: Optional[str],
    notes: Optional[List[str]] = None,
) -> str:
    """在多处尝试定位音频文件，返回绝对路径（找不到时仍返回基于 CUE 目录的推测路径）。

    定位顺序：
      1. ``--audio`` 显式指定的文件；
      2. CUE 目录下的同名文件（含 FILE 行里可能带的子目录）；
      3. 同主干 + 各种扩展名的回退；
      4. 同目录下的模糊匹配（磁盘文件名与 CUE 不完全一致时，如多出表演者前缀）。
    """
    candidates: List[str] = []
    if hint:
        candidates.append(hint)
    candidates.append(os.path.join(cue_dir, fname))
    candidates.append(fname)
    base, _ext = os.path.splitext(fname)
    for ext in _EXT_FALLBACK:
        candidates.append(os.path.join(cue_dir, base + ext))
    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.abspath(c)

    # 精确匹配全部失败 → 模糊匹配
    fuzzy = _fuzzy_find_audio(fname, cue_dir)
    if fuzzy:
        if notes is not None:
            notes.append(
                f"CUE 中声明的音频文件「{fname}」不存在，"
                f"已自动匹配到同目录下的「{os.path.basename(fuzzy)}」。"
            )
        return fuzzy

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
                current_file = _resolve_audio(fname, cue_dir, audio_hint, sheet.notes)
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
