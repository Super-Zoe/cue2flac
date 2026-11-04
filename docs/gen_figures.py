# -*- coding: utf-8 -*-
"""依据 cue2flac 仓库的真实实现生成流程图 / 架构图（SVG）。

所有节点、分支条件、参数值均来自源码，修改代码后需重新运行本脚本。
输出目录：docs/images/
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "images")


def esc(text: str) -> str:
    """转义 XML 文本内容（SVG 中 & < > 必须转义，否则 XML 非法）。"""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))

# ---- 主题色（与 gui.py 中的配色保持一致）----
C_CUE = "#1a5fb4"        # CUE 行（gui.py CUE_COLOR）
C_AUDIO = "#7a3ea8"      # 直转音频行（gui.py AUDIO_COLOR）
C_FAIL = "#d9534f"       # 失败 / 有损（gui.py FAIL_COLOR）
C_OK = "#2a9d3f"         # 成功 / 无损
C_BOX = "#eef2f8"
C_BOX_D = "#c3cede"
C_TEXT = "#1c2530"
C_MUTED = "#5b6b7c"
C_WHITE = "#ffffff"

FONT = "Microsoft YaHei, Noto Sans SC, sans-serif"
MONO = "Consolas, Noto Sans Mono CJK SC, monospace"


class Svg:
    """极简 SVG 构建器。"""

    def __init__(self, w: int, h: int, title: str = "") -> None:
        self.w, self.h = w, h
        self.parts = []
        self.title = title

    def add(self, s: str) -> None:
        self.parts.append(s)

    def box(self, x, y, w, h, text, fill=C_BOX, stroke=C_BOX_D, fs=13,
            rx=8, bold=False, tc=C_TEXT, dashed=False, sub=None, subfs=12):
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        self.add(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.4"{dash}/>')
        weight = "700" if bold else "400"
        if sub:
            self.add(
                f'<text x="{x + w/2}" y="{y + h/2 - 4}" font-family="{FONT}" '
                f'font-size="{fs}" font-weight="{weight}" fill="{tc}" '
                f'text-anchor="middle">{esc(text)}</text>')
            self.add(
                f'<text x="{x + w/2}" y="{y + h/2 + 13}" font-family="{FONT}" '
                f'font-size="{subfs}" fill="{C_MUTED}" text-anchor="middle">'
                f'{esc(sub)}</text>')
        else:
            self.add(
                f'<text x="{x + w/2}" y="{y + h/2 + fs*0.35}" font-family="{FONT}" '
                f'font-size="{fs}" font-weight="{weight}" fill="{tc}" '
                f'text-anchor="middle">{esc(text)}</text>')

    def label(self, x, y, text, fs=12, tc=C_TEXT, anchor="middle", weight="400",
              family=FONT, style=""):
        self.add(
            f'<text x="{x}" y="{y}" font-family="{family}" font-size="{fs}" '
            f'fill="{tc}" text-anchor="{anchor}" font-weight="{weight}" '
            f'style="{style}">{esc(text)}</text>')

    def arrow(self, x1, y1, x2, y2, color=C_MUTED, dashed=False, width=1.5,
              head=6):
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        self.add(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
            f'stroke-width="{width}"{dash} marker-end="url(#arrow)"/>')

    def elbow(self, pts, color=C_MUTED, dashed=False, width=1.5):
        """折线：最后一段带箭头。"""
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        d = "M " + " L ".join(f"{x} {y}" for x, y in pts)
        self.add(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}"'
            f'{dash} marker-end="url(#arrow)"/>')

    def line(self, x1, y1, x2, y2, color=C_MUTED, dashed=False, width=1.5):
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        self.add(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
            f'stroke-width="{width}"{dash}/>')

    def render(self) -> str:
        head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" '
            f'height="{self.h}" viewBox="0 0 {self.w} {self.h}">'
            f'<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
            f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
            f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{C_MUTED}"/></marker>'
            f'</defs>'
            f'<rect width="{self.w}" height="{self.h}" fill="#ffffff"/>')
        if self.title:
            head += (f'<text x="{self.w/2}" y="30" font-family="{FONT}" '
                     f'font-size="19" font-weight="700" fill="{C_TEXT}" '
                     f'text-anchor="middle">{self.title}</text>')
        return head + "".join(self.parts) + "</svg>"


# ==================================================================
# 图 1：判定链路（分流判定）—— 对应 batch.expand_to_inputs / process_all
# ==================================================================
def gen_decision_flow() -> str:
    s = Svg(1180, 1020, "cue2flac 任务分流判定链路（batch.expand_to_inputs）")

    # ---- 入口 ----
    s.box(330, 52, 520, 46, "输入路径（GUI 拖放区 / CLI 位置参数 / --dir）",
          fill="#f7f2ea", stroke="#c9b79a", fs=14, bold=True)
    s.arrow(590, 98, 590, 124)

    # ---- 分类 ----
    s.box(60, 124, 330, 92,
          "路径是目录", fill=C_BOX, stroke=C_BOX_D, fs=14, bold=True,
          sub="discover_cues() 递归扫描 .cue")
    s.box(425, 124, 330, 92,
          "路径是 .cue 文件", fill=C_BOX, stroke=C_BOX_D, fs=14, bold=True,
          sub="直接纳入 CUE 列表")
    s.box(790, 124, 330, 92,
          "路径是音频文件", fill=C_BOX, stroke=C_BOX_D, fs=14, bold=True,
          sub="纳入音频候选列表")
    s.elbow([(590, 98), (590, 112), (225, 112), (225, 124)])
    s.elbow([(590, 98), (590, 112), (590, 124)])
    s.elbow([(590, 98), (590, 112), (955, 112), (955, 124)])

    # ---- 分流点 ----
    s.box(330, 244, 520, 52, "expand_to_inputs()  ← 关键分流点",
          fill="#fff4d6", stroke="#d9a520", fs=15, bold=True,
          sub="batch.py · 解析每个 CUE，收集 referenced 与 track_titles")
    s.arrow(590, 216, 590, 244)

    # ---- 三路判定 ----
    s.box(40, 330, 350, 96, "绝对路径 ∈ referenced",
          fill="#eef4ff", stroke=C_CUE, fs=13.5, bold=True, tc=C_CUE,
          sub="被 CUE 的 FILE 行引用")
    s.box(415, 330, 350, 96, "文件名匹配「NN. <曲目标题>」",
          fill="#eef4ff", stroke=C_CUE, fs=13.5, bold=True, tc=C_CUE,
          sub="且标题 ∈ track_titles")
    s.box(790, 330, 350, 96, "以上皆否",
          fill="#f4f0fb", stroke=C_AUDIO, fs=13.5, bold=True, tc=C_AUDIO,
          sub="未被任何 CUE 引用")
    s.arrow(450, 296, 215, 330)
    s.arrow(590, 296, 590, 330)
    s.arrow(730, 296, 965, 330)

    s.label(215, 322, "是", fs=12, tc=C_CUE, weight="700")
    s.label(590, 322, "是", fs=12, tc=C_CUE, weight="700")
    s.label(965, 322, "是", fs=12, tc=C_AUDIO, weight="700")

    # ---- 排除说明 ----
    s.box(40, 452, 350, 92, "不加入直转列表",
          fill="#ffffff", stroke=C_CUE, fs=13.5, bold=True, tc=C_CUE,
          sub="母带只参与分轨，不会被再转一次")
    s.box(415, 452, 350, 92, "不加入直转列表",
          fill="#ffffff", stroke=C_CUE, fs=13.5, bold=True, tc=C_CUE,
          sub="判定为本程序生成的分轨产物")
    s.box(790, 452, 350, 92, "加入直转列表",
          fill="#ffffff", stroke=C_AUDIO, fs=13.5, bold=True, tc=C_AUDIO,
          sub="不依赖 CUE 的整轨转换")
    s.arrow(215, 426, 215, 452)
    s.arrow(590, 426, 590, 452)
    s.arrow(965, 426, 965, 452)

    # ---- 两条链路 ----
    s.box(40, 596, 520, 46, "CUE 分轨链路   process_cues() → split()",
          fill="#eef4ff", stroke=C_CUE, fs=14, bold=True, tc=C_CUE)
    s.box(620, 596, 520, 46, "音频直转链路   convert_files() → convert_file()",
          fill="#f4f0fb", stroke=C_AUDIO, fs=14, bold=True, tc=C_AUDIO)
    s.elbow([(215, 544), (215, 570), (300, 570), (300, 596)])
    s.elbow([(965, 544), (965, 570), (880, 570), (880, 596)])

    # ---- 分轨链路细节 ----
    s.box(20, 662, 250, 74, "解析 CUE",
          fill=C_BOX, stroke=C_BOX_D, fs=13, bold=True,
          sub="失败 → 跳过并记录原因")
    s.box(285, 662, 250, 74, "定位音源（四级回退）",
          fill=C_BOX, stroke=C_BOX_D, fs=12.5, bold=True,
          sub="① --audio  ② 同名  ③ 扩展名  ④ 模糊 0.60")
    s.box(20, 752, 250, 74, "判定 DSD？",
          fill=C_BOX, stroke=C_BOX_D, fs=13, bold=True,
          sub="扩展名 .dsf/.dff 或解码器名 dsd*")
    s.box(285, 752, 250, 74, "按 INDEX 01 逐首切段",
          fill=C_BOX, stroke=C_BOX_D, fs=13, bold=True,
          sub="-ss / -to 切段 → 写标签")
    s.arrow(145, 736, 145, 752)
    s.arrow(410, 736, 410, 752)
    s.elbow([(145, 642), (145, 650), (145, 662)])
    s.arrow(270, 699, 285, 699)

    # ---- 直转链路细节 ----
    s.box(620, 662, 250, 74, "探测源类型",
          fill=C_BOX, stroke=C_BOX_D, fs=13, bold=True,
          sub="FLAC / PCM 系 / DSD")
    s.box(885, 662, 250, 74, "选择编码路径",
          fill=C_BOX, stroke=C_BOX_D, fs=13, bold=True,
          sub="copy 流拷贝 / flac 编码")
    s.box(620, 752, 250, 74, "DSD → 低通 + soxr 降采样",
          fill=C_BOX, stroke=C_BOX_D, fs=12, bold=True,
          sub="输出 88200 Hz / s32")
    s.box(885, 752, 250, 74, "PCM → s16 / s32 编码",
          fill=C_BOX, stroke=C_BOX_D, fs=13, bold=True,
          sub="24bit 源用 s32 承载")
    s.arrow(745, 736, 745, 752)
    s.arrow(1010, 736, 1010, 752)
    s.elbow([(745, 642), (745, 650), (745, 662)])
    s.arrow(870, 699, 885, 699)

    # ---- 汇合到校验 ----
    s.box(300, 856, 580, 46, "verify_output()  ← 分轨与直转共用同一套判据",
          fill="#fff4d6", stroke="#d9a520", fs=14, bold=True,
          sub="verify.py · 可播放性 / 采样率 / 位深 / 声道 / 帧数 / 时长 / PCM MD5")
    s.elbow([(145, 826), (145, 840), (590, 840), (590, 856)])
    s.elbow([(1010, 826), (1010, 840), (590, 840)])

    # ---- 结论四态 ----
    s.box(20, 936, 250, 62, "无损",
          fill="#e8f6ec", stroke=C_OK, fs=14, bold=True, tc=C_OK,
          sub="PCM 逐字节 MD5 一致")
    s.box(285, 936, 250, 62, "有损转换",
          fill="#fdecea", stroke=C_FAIL, fs=14, bold=True, tc=C_FAIL,
          sub="DSD：只比声道数 + 时长")
    s.box(550, 936, 250, 62, "参数被改动",
          fill="#fff8e1", stroke="#d9a520", fs=14, bold=True, tc="#a07800",
          sub="采样率/位深/声道/帧数/时长")
    s.box(815, 936, 250, 62, "校验未通过",
          fill="#fdecea", stroke=C_FAIL, fs=14, bold=True, tc=C_FAIL,
          sub="不可播放 / PCM 不一致")
    # 汇合点：先向下到一条水平总线，再分四路，避免箭头压住 verify_output 框
    s.elbow([(590, 902), (590, 918)])
    s.line(145, 918, 940, 918)
    for x in (145, 410, 675, 940):
        s.arrow(x, 918, x, 936)

    return s.render()


# ==================================================================
# 图 2：转换关系（数据 / 格式在各阶段的转换）
# ==================================================================
def gen_convert_flow() -> str:
    s = Svg(1220, 1000, "cue2flac 转换关系（数据 / 格式在各阶段的转换）")

    # ---- PCM 系 ----
    s.label(30, 44, "PCM 系无损源（WAV / APE / WavPack / TTA / TAK / ALAC / AIFF / M4A）",
            fs=14, tc=C_TEXT, anchor="start", weight="700")
    stages = [
        ("源文件", ".wav / .ape 等", "#f7f2ea", "#c9b79a"),
        ("解码", "ffmpeg → s16/s32 PCM", C_BOX, C_BOX_D),
        ("切段 / 不切段", "分轨：按 INDEX 01\n直转：整轨不切", C_BOX, C_BOX_D),
        ("FLAC 编码", "s16 或 s32", "#eef4ff", C_CUE),
        ("输出", "NN. 标题.flac", "#e8f6ec", C_OK),
    ]
    x0, bw, gap = 30, 196, 44
    for i, (t, sub, fill, stroke) in enumerate(stages):
        x = x0 + i * (bw + gap)
        s.box(x, 58, bw, 78, t, fill=fill, stroke=stroke, fs=13.5, bold=True,
              sub=sub.replace("\n", " · "))
        if i:
            s.arrow(x - gap + 4, 97, x - 6, 97)
    s.box(30, 148, 1160, 40,
          "采样率 / 位深 / 声道数 / 帧数 / 时长 / PCM 全部不变  ⇒  逐样本无损"
          "（24bit 源用 s32 承载，bits_per_raw_sample 仍记 24）",
          fill="#e8f6ec", stroke=C_OK, fs=13, bold=True, tc=C_OK)

    # ---- FLAC 源 ----
    s.label(30, 232, "FLAC 源（.flac）", fs=14, tc=C_TEXT, anchor="start",
            weight="700")
    s.box(30, 246, 240, 78, "源文件", fill="#f7f2ea", stroke="#c9b79a",
          fs=13.5, bold=True, sub=".flac")
    s.arrow(276, 285, 320, 285)
    s.box(326, 246, 420, 78, "-c:a copy（流拷贝，不重新编码）",
          fill="#eef4ff", stroke=C_CUE, fs=13.5, bold=True, tc=C_CUE,
          sub="+ 显式 -ar <源采样率>：流拷贝时 ffmpeg 不自动填充流参数")
    s.arrow(752, 285, 796, 285)
    s.box(802, 246, 316, 78, "输出", fill="#e8f6ec", stroke=C_OK, fs=13.5,
          bold=True, tc=C_OK, sub="源名.flac")
    s.box(30, 336, 1160, 40,
          "音频数据字节级等价（仅容器标签可能被重写）",
          fill="#e8f6ec", stroke=C_OK, fs=13, bold=True, tc=C_OK)

    # ---- DSD 源 ----
    s.label(30, 420, "DSD 源（.dsf / .dff）", fs=14, tc=C_TEXT, anchor="start",
            weight="700")
    dstages = [
        ("源文件", ".dsf / .dff", "#f7f2ea", "#c9b79a"),
        ("DSD 解码", "352800 Hz（DSD64）", C_BOX, C_BOX_D),
        ("低通 + 降采样", "lowpass 24kHz\nsoxr → 88200 Hz", "#fdecea", C_FAIL),
        ("FLAC 编码", "s32", "#eef4ff", C_CUE),
        ("输出", "NN. 标题.flac", "#fdecea", C_FAIL),
    ]
    for i, (t, sub, fill, stroke) in enumerate(dstages):
        x = x0 + i * (bw + gap)
        s.box(x, 434, bw, 78, t, fill=fill, stroke=stroke, fs=13.5, bold=True,
              sub=sub.replace("\n", " · "))
        if i:
            s.arrow(x - gap + 4, 473, x - 6, 473)
    s.box(30, 524, 1160, 40,
          "1-bit 比特流无法无损映射为 PCM  ⇒  抽样丢弃，信息不可逆  ⇒  判定为「有损转换」",
          fill="#fdecea", stroke=C_FAIL, fs=13, bold=True, tc=C_FAIL)

    # ---- DSD 档位表 ----
    s.label(30, 608, "DSD 档位对照（按 ffmpeg 解码输出采样率查表 · dsd.py DSD_TARGETS）",
            fs=13.5, tc=C_TEXT, anchor="start", weight="700")
    cols = ["DSD 档位", "容器声明采样率", "ffmpeg 解码输出", "目标采样率", "低通截止"]
    widths = [190, 265, 265, 235, 205]
    x = 30
    for i, (cname, w) in enumerate(zip(cols, widths)):
        s.box(x, 622, w, 34, cname, fill="#e9eef6", stroke="#b9c6da", fs=12.5,
              bold=True, rx=4)
        x += w
    rows = [
        ("DSD64", "2 822 400 Hz", "352 800 Hz", "88 200 Hz", "24 000 Hz"),
        ("DSD128", "5 644 800 Hz", "705 600 Hz", "176 400 Hz", "40 000 Hz"),
        ("DSD256", "11 289 600 Hz", "1 411 200 Hz", "352 800 Hz", "80 000 Hz"),
    ]
    y = 656
    for r in rows:
        x = 30
        for i, (val, w) in enumerate(zip(r, widths)):
            fill = "#ffffff" if i == 0 else "#f7f9fc"
            s.box(x, y, w, 32, val, fill=fill, stroke="#d5dde8", fs=12.5,
                  rx=3, tc=C_CUE if i == 0 else C_TEXT, bold=(i == 0))
            x += w
        y += 32
    s.label(30, y + 20,
            "容器声明的 2.8224MHz 是 1-bit 流的比特率，ffmpeg 解码后输出 352.8kHz PCM（差 8 倍）"
            "，必须用解码率查表；352.8kHz 远超 FLAC 上限，不先降采样会报 not supported。",
            fs=11.5, tc=C_MUTED, anchor="start")

    # ---- 校验数据流 ----
    s.label(30, y + 56, "校验阶段的数据流（分轨与直转共用 · verify.py）",
            fs=13.5, tc=C_TEXT, anchor="start", weight="700")
    by = y + 68
    s.box(30, by, 330, 92, "源音频",
          fill="#f7f2ea", stroke="#c9b79a", fs=13.5, bold=True,
          sub="分轨：对应区段 / 直转：整文件")
    s.arrow(366, by + 46, 400, by + 46)
    s.box(406, by, 330, 92, "ffprobe",
          fill=C_BOX, stroke=C_BOX_D, fs=13.5, bold=True,
          sub="采样率 / 位深 / 声道数 / 解码器名")
    s.arrow(742, by + 46, 776, by + 46)
    s.box(782, by, 368, 92, "ffmpeg 解码为 s32le 裸 PCM",
          fill=C_BOX, stroke=C_BOX_D, fs=13.5, bold=True,
          sub="帧数 = 字节数 ÷ (4 × 声道数)  ·  MD5 逐字节比对")
    s.box(30, by + 106, 1160, 40,
          "⇒  无损  /  有损转换  /  参数被改动  /  校验未通过",
          fill="#fff4d6", stroke="#d9a520", fs=13.5, bold=True, tc="#8a6400")
    return s.render()


# ==================================================================
# 图 3：模块架构
# ==================================================================
def gen_architecture() -> str:
    s = Svg(1180, 820, "cue2flac 模块架构与调用关系")

    # ---- 入口层 ----
    s.box(40, 56, 260, 62, "gui.py", fill="#eef4ff", stroke=C_CUE, fs=15,
          bold=True, tc=C_CUE, sub="tkinter 图形界面（980×860）")
    s.box(330, 56, 260, 62, "cue2flac/cli.py", fill="#eef4ff", stroke=C_CUE,
          fs=15, bold=True, tc=C_CUE, sub="命令行入口（argparse）")
    s.box(620, 56, 260, 62, "main.py", fill="#eef4ff", stroke=C_CUE, fs=15,
          bold=True, tc=C_CUE, sub="双击无参 → GUI / 带参 → CLI")
    s.box(910, 56, 230, 62, "dnd.py", fill="#eef4ff", stroke=C_CUE, fs=15,
          bold=True, tc=C_CUE, sub="Windows 原生 COM 拖放")

    # 入口层 → 调度层（汇入 batch.py 顶边）
    s.elbow([(170, 118), (170, 138), (430, 138), (430, 158)])
    s.elbow([(460, 118), (460, 158)])
    s.elbow([(750, 118), (750, 138), (660, 138), (660, 158)])
    s.elbow([(1025, 118), (1025, 138), (750, 138), (750, 158)])

    # ---- 调度层 ----
    s.box(340, 158, 500, 60, "cue2flac/batch.py", fill="#fff4d6",
          stroke="#d9a520", fs=15, bold=True,
          sub="expand_to_inputs() 分流 · process_cues() · process_all()")

    # ---- 业务层 ----
    s.box(30, 268, 260, 74, "cue2flac/cue_parser.py",
          fill=C_BOX, stroke=C_BOX_D, fs=13, bold=True,
          sub="CUE 解析 · 四级音源定位 · 模糊匹配 0.60")
    s.box(305, 268, 260, 74, "cue2flac/splitter.py",
          fill=C_BOX, stroke=C_BOX_D, fs=13, bold=True,
          sub="按 INDEX 01 切段 · 写标签 · 封面")
    s.box(580, 268, 260, 74, "cue2flac/convert.py",
          fill=C_BOX, stroke=C_BOX_D, fs=13, bold=True,
          sub="整轨直转 FLAC · 流拷贝 / s16 / s32")
    s.box(855, 268, 260, 74, "cue2flac/dsd.py",
          fill=C_BOX, stroke=C_BOX_D, fs=13, bold=True,
          sub="DSD 档位表 · 低通 + soxr 降采样")

    # 调度层 → 业务层：从 batch.py 底边分四路，各自落到对应框顶边
    s.elbow([(430, 218), (430, 240), (160, 240), (160, 268)])
    s.elbow([(480, 218), (480, 240), (435, 240), (435, 268)])
    s.elbow([(700, 218), (700, 240), (710, 240), (710, 268)])
    s.elbow([(750, 218), (750, 240), (985, 240), (985, 268)])

    # ---- 支撑层 ----
    s.box(30, 402, 300, 74, "cue2flac/verify.py",
          fill="#f4f0fb", stroke=C_AUDIO, fs=13, bold=True, tc=C_AUDIO,
          sub="verify_output() · 7 项校验 · PCM MD5")
    s.box(350, 402, 260, 74, "cue2flac/ffmpeg_provider.py",
          fill=C_BOX, stroke=C_BOX_D, fs=12.5, bold=True,
          sub="定位 ffmpeg / ffprobe")
    s.box(630, 402, 230, 74, "cue2flac/config.py",
          fill=C_BOX, stroke=C_BOX_D, fs=13, bold=True,
          sub="默认输出目录持久化")
    s.box(880, 402, 235, 74, "cue2flac/layout.py",
          fill=C_BOX, stroke=C_BOX_D, fs=13, bold=True,
          sub="窗口内控件布局")

    # 业务层 → 支撑层：splitter/convert → verify（分轨与直转共用校验）
    s.elbow([(380, 342), (380, 372), (180, 372), (180, 402)])
    s.elbow([(710, 342), (710, 372), (250, 372), (250, 402)])
    # convert/dsd → ffmpeg_provider（两条并行，落在框顶边）
    s.elbow([(600, 342), (600, 360), (330, 360), (330, 372), (480, 372),
             (480, 402)])
    s.elbow([(900, 342), (900, 384), (530, 384), (530, 402)])
    # gui → layout（虚线，界面布局辅助；走右侧通道，箭头落在框左边）
    s.elbow([(60, 342), (60, 380), (1160, 380), (1160, 439), (1120, 439)],
            dashed=True)

    # ---- 外部依赖 ----
    s.box(340, 546, 500, 74, "ffmpeg / ffprobe（外部二进制）",
          fill="#f7f2ea", stroke="#c9b79a", fs=14, bold=True,
          sub="便携 exe 内嵌；源码运行时需自行准备（CI 自动下载）")
    # ffmpeg_provider → 外部二进制
    s.arrow(480, 476, 480, 546)
    # 业务层直接调用外部二进制（dashed）：convert → ffmpeg（走左侧外通道）
    s.elbow([(580, 342), (580, 360), (20, 360), (20, 520), (340, 520),
             (340, 546)], dashed=True)

    # ---- 输出 ----
    s.box(340, 646, 500, 62, "输出：FLAC 文件",
          fill="#e8f6ec", stroke=C_OK, fs=14, bold=True, tc=C_OK,
          sub="分轨：各专辑子文件夹 NN. 表演者 - 标题.flac ｜ 直转：整轨一个 FLAC")
    # 业务层 → 输出（分轨走左通道，直转走 convert 右侧短通道）
    s.elbow([(160, 342), (160, 620), (450, 620), (450, 646)])
    s.elbow([(760, 342), (760, 358), (1060, 358), (1060, 640), (790, 640),
             (790, 646)])

    # ---- 测试 ----
    s.box(40, 646, 260, 62, "tests/", fill="#f7f9fc", stroke="#b9c6da",
          fs=13, bold=True, sub="78 passed / 1 skipped")
    s.box(880, 646, 235, 62, ".github/workflows/build.yml",
          fill="#f7f9fc", stroke="#b9c6da", fs=12, bold=True,
          sub="CI：测试 + 打包 + Release")

    s.line(300, 677, 340, 677, dashed=True)
    s.line(840, 677, 880, 677, dashed=True)
    return s.render()


FIGURES = {
    "decision-flow.svg": gen_decision_flow,
    "convert-flow.svg": gen_convert_flow,
    "architecture.svg": gen_architecture,
}


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    for name, fn in FIGURES.items():
        path = os.path.join(OUT_DIR, name)
        svg = fn()
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(svg)
        print(f"生成 {name}  ({len(svg):,} 字节)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
