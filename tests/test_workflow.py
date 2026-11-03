"""GitHub Actions 工作流文件的静态校验。

背景：曾在步骤名里使用未加引号的 `key: value` 形式（如
`name: Smoke test (multi-format: wav/flac/ape)`），YAML 会报
"mapping values are not allowed here"，导致 GitHub **连 job 都不创建**，
CI 直接 failure 且没有任何日志可查。本模块把这类错误提前拦在测试阶段。
"""

import os
import shutil
import subprocess

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


def _pwsh_blocks():
    """抽出所有 shell=pwsh 的 run 脚本块。"""
    data = _load()
    blocks = []
    for job in data["jobs"].values():
        for step in job.get("steps", []):
            if step.get("shell") == "pwsh" and step.get("run"):
                blocks.append((str(step.get("name", "step")), step["run"]))
    return blocks


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
    """冒烟测试应覆盖多种输入格式，并逐一校验目标格式的解码器可用。"""
    data = _load()
    runs = "\n".join(
        str(step.get("run", ""))
        for job in data["jobs"].values()
        for step in job.get("steps", [])
    )
    # 目标输入格式必须全部出现在解码器校验清单中
    for fmt in ("wav", "flac", "ape", "dsf", "dff"):
        assert fmt in runs, f"冒烟测试未校验 {fmt} 解码器"
    # 可编码格式须做真实端到端分轨
    assert "master.wav" in runs, "冒烟测试未生成母带"
    assert "Split-Cue" in runs, "冒烟测试未执行分轨调用"
    # 必须断言源码按预期扩展名解析，防止静默回退造成的假通过
    assert "Escape($audioFile)" in runs, "冒烟测试缺少源码解析一致性断言"


CHECKER = r"""
$bad = 0
foreach ($f in $args) {
  $tokens = $null; $errs = $null
  [System.Management.Automation.Language.Parser]::ParseFile($f, [ref]$tokens, [ref]$errs) | Out-Null
  if ($errs -and $errs.Count -gt 0) {
    $bad++
    Write-Output ("FAIL " + $f)
    $errs | ForEach-Object { Write-Output ("  line " + $_.Extent.StartLineNumber + ": " + $_.Message) }
  }
}
if ($bad -gt 0) { exit 1 }
exit 0
"""


def _find_powershell():
    """定位可用的 PowerShell 解释器（含 Windows 常见安装路径的兜底）。"""
    for name in ("pwsh", "powershell"):
        p = shutil.which(name)
        if p:
            return p
    candidates = [
        r"C:\Program Files\PowerShell\7\pwsh.exe",
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def test_powershell_blocks_parse_cleanly(tmp_path):
    """所有 pwsh 步骤必须能被 PowerShell 解析器无错解析。

    曾在 `Write-Error "$label: ..."` 处因 `:` 被当作变量作用域限定符而报
    ParserError；同时 `name:` 里的未加引号冒号会让整个工作流 YAML 解析失败、
    GitHub 连 job 都不创建。本用例把两类错误提前拦在测试阶段。
    """
    exe = _find_powershell()
    if not exe:
        pytest.skip("环境无 PowerShell，跳过脚本语法校验")

    blocks = _pwsh_blocks()
    assert blocks, "未找到任何 pwsh 步骤"

    files = []
    for i, (name, script) in enumerate(blocks):
        # 带 BOM 写出：Windows PowerShell 5.1 默认按 ANSI 读取 .ps1，
        # 无 BOM 会让中文乱码并产生假的解析错误
        p = tmp_path / f"step{i:02d}.ps1"
        p.write_text(script, encoding="utf-8-sig", newline="\n")
        files.append(str(p))

    checker = tmp_path / "check.ps1"
    checker.write_text(CHECKER, encoding="utf-8-sig", newline="\n")

    proc = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-File", str(checker), *files],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, "PowerShell 脚本存在语法错误:\n" + (proc.stdout + proc.stderr)
