"""程序入口：带参数时走命令行，否则启动图形界面。"""

from __future__ import annotations

import os
import sys


def _maybe_hide_console() -> None:
    """打包后的控制台 exe 在「双击（无参数）进入 GUI」模式下，隐藏黑框，保持体验干净；
    CLI 模式（带参数）则保留控制台以显示进度与错误。"""
    if getattr(sys, "frozen", False) and sys.platform == "win32" and len(sys.argv) <= 1:
        try:
            import ctypes

            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        except Exception:
            pass


def main() -> None:
    # 注意：不要在这里 chdir 到 exe 目录。ffmpeg_provider 通过 _MEIPASS / __file__
    # 用绝对路径定位 ffmpeg，与 cwd 无关；而用户输入的相对 CUE/音频路径必须基于
    # 启动时的 cwd 解析，chdir 会让相对路径失效。

    _maybe_hide_console()

    if len(sys.argv) > 1:
        from cue2flac.cli import main as cli_main

        sys.exit(cli_main())
    else:
        try:
            from gui import run_gui

            run_gui()
        except Exception as e:  # noqa: BLE001
            print(f"无法启动界面：{e}")
            sys.exit(1)


if __name__ == "__main__":
    main()
