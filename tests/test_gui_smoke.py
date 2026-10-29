"""GUI 布局冒烟测试：需要真实 tkinter 环境（无显示环境时自动跳过）。

回归点：Tkinter 的 pack 不会换行，窗口缩小时横向排列的按钮会溢出容器被裁切，
导致「开始转换 / 删除选中 / 清空」等文字看不见。这里在真实窗口上验证：
1. 应用能正常构造，关键控件均已映射；
2. 窗口缩到很窄并触发重排后，按钮仍完整落在其容器内（右边界不超出），
   即没有被裁切，且宽度大于 0；
3. 长文字标签在缩窄后启用了换行，且换行宽度不超过自身可用宽度。

注意：同一个进程内反复创建 / 销毁 Tk 根窗口偶发失败，因此整个模块共用
一个 App 实例（module 级 fixture），避免重建窗口导致用例被跳过。
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
            # 环境无真实显示服务（窗口无法映射），布局断言无意义 → 跳过
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


def _assert_inside_container(widget, container, label):
    """断言控件完整位于容器可视范围内（未被裁切）。"""
    assert widget.winfo_ismapped(), f"{label} 未映射（不可见）"
    assert widget.winfo_width() > 0, f"{label} 宽度为 0（被压缩不可见）"
    right = widget.winfo_x() + widget.winfo_width()
    avail = container.winfo_width()
    assert right <= avail + 2, (
        f"{label} 超出容器被裁切：右边界 {right} > 容器宽 {avail}"
    )


def test_gui_constructs_with_key_widgets(app):
    app.geometry("900x700")
    app.update()
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


def test_buttons_not_clipped_when_window_narrowed(app):
    app.geometry("900x700")
    app.update()

    # 缩到很窄：这是原先按钮文字消失的场景
    app.geometry("280x700")
    app.update()
    app._relayout()
    app.update()

    # 按钮行的三个按钮必须完整可见、未被裁切
    act = app.btn_convert.master
    for widget, label in (
        (app.btn_convert, "开始转换"),
        (app.btn_delete, "删除选中"),
        (app.btn_clear, "清空"),
    ):
        _assert_inside_container(widget, act, label)

    # 拖放区按钮行与输出目录行同样不能被裁切
    _assert_inside_container(app.btn_files, app.btn_files.master, "选择文件…")
    _assert_inside_container(app.btn_folder, app.btn_folder.master, "选择文件夹…")
    _assert_inside_container(app.btn_pick_out, app.btn_pick_out.master, "选择…")
    _assert_inside_container(app.btn_reset_out, app.btn_reset_out.master, "恢复默认")


def test_long_labels_wrap_when_narrowed(app):
    app.geometry("900x700")
    app.update()
    app.geometry("300x700")
    app.update()
    app._relayout()
    app.update()

    # 长文字标签应启用换行，且换行宽度不超过自身可用宽度
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
