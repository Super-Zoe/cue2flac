"""程序入口：带参数时走命令行，否则启动图形界面。"""

from __future__ import annotations

import os
import sys


def main() -> None:
    # 打包后把工作目录切到 exe 所在目录，保证相对资源（如内嵌 ffmpeg）可定位
    if getattr(sys, "frozen", False):
        os.chdir(os.path.dirname(sys.executable))

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
