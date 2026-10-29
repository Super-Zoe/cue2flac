"""定位 ffmpeg / ffprobe 可执行文件。

运行方式有两种：
1. 源码模式：从 PATH 查找，或放在项目根目录的 ffmpeg/ 文件夹下。
2. 打包后的便携 EXE：ffmpeg/ffprobe 已通过 PyInstaller 内嵌到 _MEIPASS 中。
"""

from __future__ import annotations

import os
import shutil
import sys


def resolve_tool(name: str) -> str:
    """返回 ffmpeg 或 ffprobe 的可执行文件路径，找不到时抛错。"""
    if name not in ("ffmpeg", "ffprobe"):
        raise ValueError(f"不支持的工具名: {name}")

    # 1) 打包后的便携模式：内嵌在 _MEIPASS/ffmpeg/ 下
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        for sub in (f"ffmpeg/{name}.exe", f"ffmpeg/{name}"):
            p = os.path.join(base, sub)
            if os.path.isfile(p):
                return p

    # 2) 源码模式：项目根目录下的 ffmpeg/ 文件夹
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local = os.path.join(project_root, "ffmpeg", f"{name}.exe")
    if os.path.isfile(local):
        return local

    # 3) PATH
    found = shutil.which(name)
    if found:
        return found

    raise FileNotFoundError(
        f"未找到 {name}。\n"
        f"便携版已内嵌 {name}；若以源码运行，请将其加入 PATH，\n"
        f"或把 {name}.exe 放到项目根目录的 ffmpeg/ 文件夹下。"
    )
