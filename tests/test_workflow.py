"""GitHub Actions 工作流文件的静态校验。

背景：曾在步骤名里使用未加引号的 `key: value` 形式（如
`name: Smoke test (multi-format: wav/flac/ape)`），YAML 会报
"mapping values are not allowed here"，导致 GitHub **连 job 都不创建**，
CI 直接 failure 且没有任何日志可查。本模块把这类错误提前拦在测试阶段。
"""

import os

import pytest

yaml = pytest.importorskip("yaml", reason="未安装 PyYAML，跳过工作流校验")

WORKFLOW = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".github",
    "workflows",
    "build.yml",
)


def _load():
    if not os.path.isfile(WORKFLOW):
        pytest.skip("未找到工作流文件")
    with open(WORKFLOW, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_workflow_yaml_is_valid():
    """工作流必须是合法 YAML（否则 GitHub 不会创建任何 job）。"""
    data = _load()
    assert isinstance(data, dict)
    assert "jobs" in data and data["jobs"], "工作流缺少 jobs 定义"


def test_steps_have_no_ambiguous_colons():
    """步骤名不得包含未加引号的 `key: value`（会破坏 YAML 解析）。"""
    data = _load()
    for job_name, job in data["jobs"].items():
        for step in job.get("steps", []):
            name = str(step.get("name", ""))
            assert ":" not in name or name.startswith(('"', "'")), (
                f"步骤名 {name!r} 含未加引号的冒号，会导致 YAML 解析失败"
            )


def test_push_filter_only_builds_on_source_changes():
    """仅源码/打包相关文件变动才触发打包，纯文档改动应跳过。"""
    data = _load()
    # PyYAML 会把裸 `on` 解析成布尔 True，需兼容两种键名
    trigger = data.get("on", data.get(True))
    assert trigger, "未找到 push 触发器"
    paths = trigger["push"]["paths"]
    for p in ("cue2flac/**", "gui.py", "main.py", "tests/**"):
        assert p in paths, f"paths 过滤缺少 {p}"
    # 文档类文件不应触发打包
    assert "README.md" not in paths
    assert "USAGE.md" not in paths


def test_smoke_test_covers_multiple_formats():
    """冒烟测试应覆盖 wav/flac/ape 三种 PCM 源码，并校验 DSD 解码器可用。"""
    data = _load()
    runs = "\n".join(
        str(step.get("run", ""))
        for job in data["jobs"].values()
        for step in job.get("steps", [])
    )
    for token in ("test.wav", "test.flac", "test.ape"):
        assert token in runs, f"冒烟测试未覆盖 {token}"
    assert "dsf" in runs and "dff" in runs, "冒烟测试未校验 DSD(dsf/dff) 解码器"
