"""折行布局算法的单元测试（纯计算，不依赖 tkinter）。"""

from cue2flac.layout import plan_rows


def test_all_fit_in_one_row():
    assert plan_rows([100, 100, 100], 400) == [[0, 1, 2]]


def test_wrap_when_exceeding_width():
    # 第三个放不下 → 折到第二行
    assert plan_rows([100, 100, 100], 250) == [[0, 1], [2]]
    assert plan_rows([100, 100, 100], 199) == [[0], [1], [2]]


def test_oversized_item_gets_its_own_row():
    # 单个控件宽度超过可用宽度时独占一行，保证不被压缩裁切
    assert plan_rows([300, 100], 200) == [[0], [1]]
    assert plan_rows([300, 100], 200)[0] == [0]


def test_every_item_placed_exactly_once():
    sizes = [80, 120, 60, 200, 40]
    for avail in (1, 50, 150, 300, 600, 10000):
        rows = plan_rows(sizes, avail)
        flat = [i for row in rows for i in row]
        assert flat == list(range(len(sizes))), f"avail={avail} 漏排或重复"


def test_zero_or_negative_avail_one_per_row():
    assert plan_rows([100, 100], 0) == [[0], [1]]
    assert plan_rows([100, 100], -5) == [[0], [1]]


def test_empty_input():
    assert plan_rows([], 300) == []
