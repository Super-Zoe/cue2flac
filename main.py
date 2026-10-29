"""程序入口：带参数时走命令行，否则启动图形界面。"""

from __future__ import annotations

import sys


def main() -> None:
    # 注意：不要在这里 chdir 到 exe 目录。ffmpeg_provider 通过 _MEIPASS / __file__
    # 用绝对路径定位 ffmpeg，与 cwd 无关；而用户输入的相对 CUE/音频路径必须基于
    # 启动时的 cwd 解析，chdir 会让相对路径失效。

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
