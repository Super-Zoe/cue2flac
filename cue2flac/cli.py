"""命令行入口。双击（无参数）由 main.py 启动 GUI；带参数时走这里。"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional, Tuple

from .cue_parser import parse_cue
from .splitter import split, SplitterError
from .batch import process_all
from .convert import SUPPORTED_EXTS
from . import __version__


def configure_stdio() -> None:
    """把 stdout/stderr 配置为 UTF-8 + errors=replace，避免在非中文代码页的控制台
    （如 CI 的 cp1252）打印中文时抛 UnicodeEncodeError 而中断分轨。"""
    for s in (sys.stdout, sys.stderr):
        if s is None:
            continue
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


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


def _report_error(
    exc: Exception,
    log_path: Optional[str],
    title: str = "cue2flac 出错",
    unexpected: bool = False,
) -> int:
    """统一的错误出口：stderr 文案 + 窗口化弹窗 + 退出码 1。

    三个入口的**异常类型集合**不同（单 CUE 额外捕获 SplitterError），
    由各入口自行 ``except``；这里只收敛重复的打印 / 弹窗 / return 样板。
    """
    prefix = "未预期错误" if unexpected else "错误"
    print(f"{prefix}：{exc}", file=sys.stderr)
    if log_path:
        _show_gui_message("error", title, str(exc))
    return 1


def _finish(
    msg: str,
    log_path: Optional[str],
    title: str,
    kind: str = "info",
    code: int = 0,
) -> int:
    """统一的成功 / 提示出口：stdout 文案 + 窗口化弹窗 + 退出码。

    ``msg`` 与 ``title`` 由各入口自己构造——单 CUE / 混合 / 批量三种
    汇总文案不同，不能合并成一份通用文案。
    """
    print(msg)
    if log_path:
        _show_gui_message(kind, title, msg)
    return code


def _count_convert(convert_results) -> Tuple[int, int]:
    """返回 (直转成功数, 直转失败数)。"""
    ok = sum(1 for r in convert_results if r.ok)
    return ok, len(convert_results) - ok


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cue2flac",
        description=(
            "根据 CUE 分轨文件，把整轨无损音频（FLAC/WAV/APE/...）拆分为多首独立 FLAC；"
            "也可不使用 CUE，直接把整轨无损音频转换为 FLAC。"
        ),
    )
    p.add_argument(
        "cue",
        nargs="*",
        help=(
            "CUE 文件路径；也可传入【目录】，自动批量扫描其中的 .cue 文件。"
            "还可直接传入无损音频文件（"
            + "、".join(SUPPORTED_EXTS[:6])
            + "、"
            + "、".join(SUPPORTED_EXTS[-2:])
            + " 等），不经 CUE 直接转换为 FLAC；可同时给出多个路径。"
        ),
    )
    p.add_argument(
        "-o", "--out",
        help="输出目录；批量模式下作为统一输出根目录。"
             "不指定时依次回退：配置文件中的默认值 → 各 CUE 所在目录下的专辑名子文件夹",
    )
    p.add_argument("-a", "--audio", help="指定整轨音频文件（覆盖 CUE 中 FILE 指向）")
    p.add_argument("--no-cover", action="store_true", help="不复制封面")
    p.add_argument(
        "--no-verify",
        action="store_true",
        help="跳过转换后的无损校验（默认开启：校验可播放性 / 采样率 / 位深 / "
             "声道数 / 帧数 / 时长 / PCM 逐字节一致性，并输出判定结果）",
    )
    p.add_argument(
        "--dir",
        metavar="DIR",
        help="要批量扫描的目录（等价于把目录作为位置参数传入）",
    )
    p.add_argument(
        "-r", "--recursive", action="store_true", default=True, help="递归遍历子目录（默认开启）"
    )
    p.add_argument(
        "--no-recursive",
        action="store_true",
        help="不遍历子目录，仅处理指定目录下的 .cue",
    )
    p.add_argument("-v", "--version", action="version", version=f"cue2flac {__version__}")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    # 必须在解析参数前：① 配置编码安全的 stdio；② 窗口化 exe 下重定向 stdout。
    configure_stdio()
    log_path = _setup_windowed_logging()

    parser = build_parser()
    args = parser.parse_args(argv)

    # 未显式指定 -o 时，回退到配置中的默认输出目录（若已设置）。
    if not args.out:
        try:
            from .config import get_out_dir

            args.out = get_out_dir()
        except Exception:  # noqa: BLE001
            args.out = None

    # 批量模式：--dir 指向目录，或位置参数里含目录
    inputs = list(args.cue or [])
    batch_root = args.dir or next((p for p in inputs if os.path.isdir(p)), None)
    if batch_root:
        return _run_batch(batch_root, args, log_path)

    if not inputs:
        parser.print_help()
        return 2

    # 单个 CUE → 原有分轨流程（保持向后兼容）
    if len(inputs) == 1 and inputs[0].lower().endswith(".cue"):
        return _run_single_cue(inputs[0], args, log_path)

    # 其余情况：混合处理（多个 CUE / 音频文件 / 两者混合）
    return _run_mixed(inputs, args, log_path)


def _run_single_cue(cue: str, args, log_path: Optional[str]) -> int:
    """处理单个 CUE 文件（原有行为）。"""
    try:
        sheet = parse_cue(cue, audio_hint=args.audio)
        out_dir = args.out or os.path.join(
            os.path.dirname(os.path.abspath(cue)),
            sheet.album_title or "output",
        )
        print(f"解析 CUE：专辑《{sheet.album_title or '未命名'}》，共 {sheet.total_tracks} 首")
        print(f"音频文件：{sheet.tracks[0].file}")
        results = split(
            sheet,
            out_dir,
            audio_hint=args.audio,
            copy_cover=not args.no_cover,
            verify=not args.no_verify,
            on_progress=lambda m: print(m, end=""),
        )
        msg = f"分轨完成，已生成 {len(results)} 个文件：\n{out_dir}"
        return _finish(msg, log_path, "cue2flac 完成")
    except (FileNotFoundError, SplitterError, ValueError) as e:
        return _report_error(e, log_path)
    except Exception as e:  # noqa: BLE001
        return _report_error(e, log_path, unexpected=True)


def _run_mixed(inputs: List[str], args, log_path: Optional[str]) -> int:
    """混合处理：多个 CUE（分轨）与音频文件（直转）可在一次调用中完成。"""
    try:
        result, convert_results = process_all(
            inputs,
            recursive=not args.no_recursive,
            out=args.out,
            audio_hint=args.audio,
            copy_cover=not args.no_cover,
            verify=not args.no_verify,
            on_progress=lambda m: print(m, end=""),
        )
    except (FileNotFoundError, ValueError) as e:
        return _report_error(e, log_path)
    except Exception as e:  # noqa: BLE001
        return _report_error(e, log_path, unexpected=True)

    print()
    cue_ok = result.success_count
    cue_skip = result.skip_count
    conv_ok, conv_fail = _count_convert(convert_results)

    if cue_ok == 0 and conv_ok == 0:
        return _finish(
            "未成功处理任何文件，请检查日志中的跳过原因。",
            log_path, "cue2flac", kind="error", code=1,
        )

    msg = (
        f"处理完成：分轨成功 {cue_ok} 个（跳过 {cue_skip} 个），"
        f"直接转换成功 {conv_ok} 个（失败 {conv_fail} 个）。"
    )
    return _finish(msg, log_path, "cue2flac 完成")


def _run_batch(root: str, args, log_path: Optional[str]) -> int:
    """批量扫描目录：处理其中的全部 CUE，并对未被 CUE 引用的音频做直接转换。"""
    recursive = args.recursive and not args.no_recursive
    try:
        result, convert_results = process_all(
            [root],
            recursive=recursive,
            out=args.out,
            audio_hint=args.audio,
            copy_cover=not args.no_cover,
            verify=not args.no_verify,
            on_progress=lambda m: print(m, end=""),
        )
    except (FileNotFoundError, ValueError) as e:
        return _report_error(e, log_path)
    except Exception as e:  # noqa: BLE001
        return _report_error(e, log_path, unexpected=True)

    print()
    conv_ok, _conv_fail = _count_convert(convert_results)
    if result.scanned == 0 and not convert_results:
        return _finish(
            f"在 {root} 下未发现任何 .cue 文件或可转换的音频文件。",
            log_path, "cue2flac 批量", kind="error", code=1,
        )
    if result.success_count == 0 and conv_ok == 0:
        return _finish(
            "未发现可成功处理的文件，请检查日志中的跳过原因。",
            log_path, "cue2flac 批量", kind="error", code=1,
        )

    msg = (
        f"批量处理完成：分轨成功 {result.success_count} 个（跳过 {result.skip_count} 个），"
        f"直接转换成功 {conv_ok} 个。"
    )
    return _finish(msg, log_path, "cue2flac 批量完成")


if __name__ == "__main__":
    sys.exit(main())
