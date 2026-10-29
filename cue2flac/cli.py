"""命令行入口。双击（无参数）由 main.py 启动 GUI；带参数时走这里。"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from .cue_parser import parse_cue
from .splitter import split, SplitterError
from . import __version__


def _setup_windowed_logging() -> Optional[str]:
    """便携 EXE（--windowed）下 stdout 为 None，把输出重定向到日志文件并返回其路径。"""
    if getattr(sys, "frozen", False) and sys.stdout is None:
        log_path = os.path.join(os.path.dirname(sys.executable), "cue2flac_cli.log")
        f = open(log_path, "w", encoding="utf-8", errors="replace")
        sys.stdout = f
        sys.stderr = f
        return log_path
    return None


def _show_gui_message(kind: str, title: str, message: str) -> None:
    """在窗口化便携模式下用消息框反馈结果（仅当 stdout 被重定向时）。"""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        if kind == "error":
            messagebox.showerror(title, message)
        else:
            messagebox.showinfo(title, message)
        root.destroy()
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cue2flac",
        description="根据 CUE 分轨文件，把整轨无损音频（FLAC/WAV/APE/...）拆分为多首独立 FLAC。",
    )
    p.add_argument("cue", nargs="?", help="CUE 文件路径")
    p.add_argument("-o", "--out", help="输出目录（默认：CUE 同目录下的 <专辑名> 文件夹）")
    p.add_argument("-a", "--audio", help="指定整轨音频文件（覆盖 CUE 中 FILE 指向）")
    p.add_argument("--no-cover", action="store_true", help="不复制封面")
    p.add_argument("--verify", action="store_true", help="分轨后校验每条音轨的 PCM 无损")
    p.add_argument("-v", "--version", action="version", version=f"cue2flac {__version__}")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    # 必须在解析参数前重定向：--windowed 打包后 sys.stdout 为 None，
    # argparse 的 --version/--help 会直接写 sys.stdout，不提前重定向会崩溃。
    log_path = _setup_windowed_logging()

    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.cue:
        parser.print_help()
        return 2

    try:
        sheet = parse_cue(args.cue, audio_hint=args.audio)
        out_dir = args.out or os.path.join(
            os.path.dirname(os.path.abspath(args.cue)),
            sheet.album_title or "output",
        )
        print(f"解析 CUE：专辑《{sheet.album_title or '未命名'}》，共 {sheet.total_tracks} 首")
        print(f"音频文件：{sheet.tracks[0].file}")
        results = split(
            sheet,
            out_dir,
            audio_hint=args.audio,
            copy_cover=not args.no_cover,
            verify=args.verify,
            on_progress=lambda m: print(m, end=""),
        )
        msg = f"分轨完成，已生成 {len(results)} 个文件：\n{out_dir}"
        print(msg)
        if log_path:
            _show_gui_message("info", "cue2flac 完成", msg)
        return 0
    except (FileNotFoundError, SplitterError, ValueError) as e:
        print(f"错误：{e}", file=sys.stderr)
        if log_path:
            _show_gui_message("error", "cue2flac 出错", str(e))
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"未预期错误：{e}", file=sys.stderr)
        if log_path:
            _show_gui_message("error", "cue2flac 出错", str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
