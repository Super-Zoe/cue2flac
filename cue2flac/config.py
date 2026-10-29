"""轻量配置：持久化用户偏好（如默认输出目录）。

配置文件位置：
- 打包后的便携 exe：与可执行文件同目录的 cue2flac_config.json；
- 源码运行：用户配置目录（~/.config/cue2flac/config.json）。

读取失败或文件不存在时返回空配置，不影响主流程。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional

CONFIG_FILENAME = "cue2flac_config.json"


def _config_path() -> str:
    if getattr(sys, "frozen", False) and getattr(sys, "executable", None):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.join(os.path.expanduser("~"), ".config", "cue2flac")
    return os.path.join(base, CONFIG_FILENAME)


def load_config() -> Dict[str, Any]:
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_config(data: Dict[str, Any]) -> None:
    path = _config_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


def get_out_dir() -> Optional[str]:
    """返回用户配置的输出目录；未配置或为空时返回 None（表示使用内置默认）。"""
    val = load_config().get("out_dir")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def set_out_dir(out_dir: Optional[str]) -> None:
    """写入 / 清除（传 None 或空字符串时清除）输出目录偏好。"""
    data = load_config()
    if out_dir and str(out_dir).strip():
        data["out_dir"] = str(out_dir).strip()
    else:
        data.pop("out_dir", None)
    save_config(data)
