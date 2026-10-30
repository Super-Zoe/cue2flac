"""GUI 布局冒烟测试：需要真实 tkinter 环境（无显示环境时部分用例自动跳过）。

回归点：Tkinter 的 pack 不会换行，窗口缩小时横向排列的按钮会溢出容器被裁切，
导致「开始转换 / 删除选中 / 清空」等文字看不见。

说明：CI（无真实窗口管理器）下调用 geometry() 缩窄窗口会让窗口取消映射，
此时 winfo_ismapped() 恒为 0，几何断言无意义——这类断言会在检测到窗口未映射时
自动跳过；在有显示服务的环境（如本机 Windows）则会完整执行。

同一个进程内反复创建 / 销毁 Tk 根窗口偶发失败，因此整个模块共用一个 App 实例。
"""

import os
import sys

import pytest

# 保证能导入仓库根目录的 gui.py（无论以何种方式调用 pytest）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("tkinter")


@pytest.fixture(scope="module")
def app():
    """构造一次 App 供本模块所有用例复用；无显示环境时整体跳过。"""
    try:
        from gui import App
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"无法导入 gui：{e}")

    instance = None
    try:
        instance = App()
        instance.update()
        if not instance.winfo_ismapped():
            pytest.skip("窗口未映射（无显示环境），跳过 GUI 布局测试")
    except Exception as e:  # noqa: BLE001
        if instance is not None:
            try:
                instance.destroy()
            except Exception:  # noqa: BLE001
                pass
        pytest.skip(f"无法创建窗口（无显示环境？）：{e}")

    yield instance
    try:
        instance.destroy()
    except Exception:  # noqa: BLE001
        pass


def test_gui_constructs_with_key_widgets(app):
    """构造后关键控件必须已映射且尺寸正常（不依赖事件回调才可见）。"""
    for widget, label in (
        (app.btn_convert, "开始转换"),
        (app.btn_delete, "删除选中"),
        (app.btn_clear, "清空"),
        (app.btn_files, "选择文件…"),
        (app.btn_folder, "选择文件夹…"),
        (app.out_entry, "输出目录输入框"),
        (app.tree, "解析结果列表"),
    ):
        assert widget.winfo_ismapped(), f"{label} 未映射"
        assert widget.winfo_width() > 0, f"{label} 宽度为 0"


def test_reflow_keeps_all_widgets_laid_out(app):
    """重排后所有控件仍被 grid 管理，且不会重叠在同一格。"""
    app._relayout()
    app.update()

    for parent, items in app._flows:
        seen = set()
        for widget, _padx, _expand in items:
            info = widget.grid_info()
            assert info and "row" in info and "column" in info, (
                f"{widget} 未纳入 grid 布局"
            )
            assert int(info["row"]) >= 0 and int(info["column"]) >= 0
            assert widget.winfo_reqwidth() > 0, f"{widget} 请求宽度为 0"
            cell = (int(info["row"]), int(info["column"]))
            assert cell not in seen, f"控件重叠在同一格 {cell}"
            seen.add(cell)


def test_buttons_not_clipped_when_window_narrowed(app):
    """窗口缩到很窄后，按钮仍完整落在容器内（未被裁切）。"""
    app.geometry("280x700")
    app.update()
    app._relayout()
    app.update()

    # 无真实窗口管理器时，改几何后子控件不会重新映射，几何断言无意义 → 跳过
    if not app.btn_convert.winfo_ismapped():
        pytest.skip("缩窄后控件未映射（无显示环境），跳过几何断言")

    act = app.btn_convert.master
    for widget, label in (
        (app.btn_convert, "开始转换"),
        (app.btn_delete, "删除选中"),
        (app.btn_clear, "清空"),
    ):
        assert widget.winfo_ismapped(), f"{label} 未映射（不可见）"
        assert widget.winfo_width() > 0, f"{label} 宽度为 0"
        right = widget.winfo_x() + widget.winfo_width()
        assert right <= act.winfo_width() + 2, (
            f"{label} 超出容器被裁切：右边界 {right} > 容器宽 {act.winfo_width()}"
        )

    for widget, label in (
        (app.btn_files, "选择文件…"),
        (app.btn_folder, "选择文件夹…"),
        (app.btn_pick_out, "选择…"),
        (app.btn_reset_out, "恢复默认"),
    ):
        container = widget.master
        assert widget.winfo_ismapped(), f"{label} 未映射（不可见）"
        right = widget.winfo_x() + widget.winfo_width()
        assert right <= container.winfo_width() + 2, (
            f"{label} 超出容器被裁切：右边界 {right} > 容器宽 {container.winfo_width()}"
        )


def test_long_labels_wrap_when_narrowed(app):
    """缩窄后长文字标签启用换行，且换行宽度不超过自身可用宽度。"""
    app.geometry("300x700")
    app.update()
    app._relayout()
    app.update()

    for label, name in (
        (app.drop_label, "拖放提示"),
        (app.out_desc, "输出目录说明"),
        (app.status_label, "状态栏"),
        (app.lbl_hint, "操作提示"),
    ):
        wrap = label.cget("wraplength")
        assert wrap and wrap > 0, f"{name} 未设置换行宽度"
        assert wrap <= max(label.winfo_width(), 1), (
            f"{name} 换行宽度 {wrap} 超过自身宽 {label.winfo_width()}"
        )


def test_sections_reachable_when_window_short(app):
    """窗口缩得很矮时，下半部分（输出目录 / 结果 / 按钮 / 日志）不会被裁切消失。

    回归点：以前这些分区直接 pack 进根窗口，窗口变矮后它们被推出可视区且无法到达；
    现在放进可滚动画布，缩矮后它们仍被 Tk 保持 realized（可映射），并由右侧滚动条到达。
    """
    app.geometry("980x400")
    app.update()
    app._relayout()
    app.update()

    # 无真实窗口管理器时，几何 / 映射断言无意义 → 跳过
    if not app.canvas.winfo_ismapped():
        pytest.skip("缩矮后画布未映射（无显示环境），跳过断言")

    # 外层滚动条必须存在
    assert app.vsb.winfo_ismapped(), "垂直滚动条未映射"
    # 下半部分控件仍被 Tk 保持 realized（未因窗口变矮而被销毁 / 取消映射）
    for widget, label in (
        (app.out_entry, "输出目录输入框"),
        (app.tree, "解析结果列表"),
        (app.btn_convert, "开始转换"),
        (app.text_log, "日志"),
    ):
        assert widget.winfo_ismapped(), f"{label} 未映射（被裁切消失）"

    # 内容确实高于视口：滚动范围底部超出画布底部，说明滚动条确实可用
    bbox = app.canvas.bbox("all")
    assert bbox is not None, "画布滚动范围为空"
    _, _, _, bottom = bbox
    assert bottom > app.canvas.winfo_height(), "内容未超出视口，无需滚动"
