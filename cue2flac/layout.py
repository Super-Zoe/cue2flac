"""布局辅助：与 Tk 无关的纯几何计算，便于单独测试。

图形界面中，横向排列的按钮行在窗口变窄时会溢出被裁切（Tkinter 的 pack 不换行）。
这里提供「按可用宽度贪心折行」的计算，供界面层据此用 grid 重新排布。
"""

from __future__ import annotations

from typing import List, Sequence


def plan_rows(sizes: Sequence[float], avail: float) -> List[List[int]]:
    """把一系列控件按可用宽度贪心分组成行。

    :param sizes: 每个控件所需的最小宽度
    :param avail: 容器的可用宽度
    :return: 每行包含的控件索引，例如 [[0, 1], [2]]

    保证每个控件都被分到某一行（不会漏排）；当单个控件宽度已超过 ``avail``
    时，它会独占一行——由调用方负责让它完整显示，而不是被压缩裁切。
    """
    if not sizes:
        return []

    # 可用宽度过小（含尚未完成布局时宽度为 0）时，每行只放一个，确保都可见
    if avail <= 0:
        return [[i] for i in range(len(sizes))]

    rows: List[List[int]] = []
    cur: List[int] = []
    used = 0.0
    for i, size in enumerate(sizes):
        if cur and used + size > avail:
            rows.append(cur)
            cur = []
            used = 0.0
        cur.append(i)
        used += size
    if cur:
        rows.append(cur)
    return rows
