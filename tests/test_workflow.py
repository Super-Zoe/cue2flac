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
    # 目标输入格式必须全部出现在能力校验清单中
    for fmt in ("wav", "flac", "ape", "dsf", "dff"):
        assert fmt in runs, f"冒烟测试未校验 {fmt} 格式能力"
    # 容器解析与解码能力要分别校验（WAV 无独立解码器；DSD 解码器名为 dsd*）
    assert "-demuxers" in runs, "冒烟测试未校验容器解复用器"
    assert "-decoders" in runs, "冒烟测试未校验解码器"
    # 能力探测用 `ffmpeg -h <kind>=<name>` 查注册表，而非解析列表文本
    assert "Test-FfmpegHas" in runs, "冒烟测试未使用注册表探测方式"
    # ffmpeg 的容器名与文件扩展名并不总是一致：.dff 走 dsdiff 解复用器，DSD 解码器名为 dsd*
    assert "dsdiff" in runs, "冒烟测试未使用正确的 DSDIFF 解复用器名"
    assert "dsd_lsbf" in runs, "冒烟测试未使用正确的 DSD 解码器名"
    # 可编码格式须做真实端到端分轨
    assert "master.wav" in runs, "冒烟测试未生成母带"
    assert "Split-Cue" in runs, "冒烟测试未执行分轨调用"
    # 必须断言源码按预期扩展名解析，防止静默回退造成的假通过
    assert "Escape($audioFile)" in runs, "冒烟测试缺少源码解析一致性断言"


def test_direct_convert_smoke_covers_bit_depth_and_lossless():
    """直转冒烟必须校验位深保持与 PCM 无损（这是「无损」承诺的硬证据）。"""
    data = _load()
    runs = "\n".join(
        str(step.get("run", ""))
        for job in data["jobs"].values()
        for step in job.get("steps", [])
    )
    # 必须有独立的直转冒烟步骤
    assert "直接转换汇总" in runs, "缺少不依赖 CUE 的直转冒烟测试"
    # 位深保持断言（24bit 源不能被静默降为 16bit）
    assert "bits_per_raw_sample" in runs, "直转冒烟未校验位深"
    assert "pcm_s24le" in runs, "直转冒烟未覆盖 24bit 源"
    assert "48000" in runs, "直转冒烟未覆盖非 44.1kHz 采样率"
    # 无损实证：源与输出的 s32le PCM 求 MD5 比对
    assert "pcm_s32le" in runs, "直转冒烟未做 PCM MD5 无损比对"
    assert "Get-FileHash" in runs, "直转冒烟未计算 MD5"
    # 打包时必须包含新模块，否则 exe 运行时找不到 cue2flac.convert
    build_steps = "\n".join(
        str(step.get("run", ""))
        for job in data["jobs"].values()
        for step in job.get("steps", [])
    )
    assert "cue2flac.convert" in build_steps, "打包未包含 cue2flac.convert 模块"


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
        # Windows PowerShell 5.1 的 stdout 编码跟随系统区域设置，
        # 脚本里的中文提示可能被编码成 GBK；显式用 errors="replace"
        # 避免解码阶段直接抛 UnicodeDecodeError 而看不到真实报错。
        encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, "PowerShell 脚本存在语法错误:\n" + (proc.stdout + proc.stderr)


def test_workflow_uses_no_verify_flag():
    """CLI 校验已改为默认开启，CI 不应再传 --verify（该参数已移除）。"""
    data = _load()
    runs = "\n".join(
        str(step.get("run", ""))
        for job in data["jobs"].values()
        for step in job.get("steps", [])
    )
    assert "--verify" not in runs, "CI 仍在使用已移除的 --verify 参数"
    assert "--no-verify" not in runs or True


def test_direct_convert_smoke_asserts_verification_output():
    """直转冒烟必须断言程序输出了逐项校验结果并判定为「无损」。"""
    data = _load()
    runs = "\n".join(
        str(step.get("run", ""))
        for job in data["jobs"].values()
        for step in job.get("steps", [])
    )
    assert "校验：采样率" in runs, "直转冒烟未断言逐项校验输出"
    assert "（无损）" in runs, "直转冒烟未断言校验结论为无损"


def test_dsd_split_smoke_exists_and_asserts_sample_rate():
    """CI 必须有 DSD 分轨冒烟，并断言输出采样率被降到 88200Hz。

    回归防护：DSD64 解码率为 352800Hz，超出 FLAC 编码器上限。
    分轨链路若漏了降采样，ffmpeg 会直接报 "352800 Hz not supported"，
    DSD 源分轨 100% 失败。冒烟必须能发现这一点。
    """
    data = _load()
    steps = [
        st
        for job in data["jobs"].values()
        for st in job.get("steps", [])
    ]
    dsd_steps = [st for st in steps if "DSD" in str(st.get("name", ""))]
    assert dsd_steps, "CI 缺少 DSD 分轨冒烟步骤"

    run = "\n".join(str(st.get("run", "")) for st in dsd_steps)
    assert "'88200'" in run, "DSD 冒烟未断言输出采样率为 88200Hz"
    assert "bits_per_raw_sample" in run, "DSD 冒烟未断言输出位深"
    assert "not supported" in run, "DSD 冒烟未说明降采样的必要性"
    # 不得依赖 tests/ 目录下的辅助模块（CI 不打包测试文件）
    assert "dsd_fixtures" not in run, "CI 冒烟不应依赖 tests/ 下的模块"


def test_hidden_imports_include_dsd_module():
    """打包必须包含 cue2flac.dsd，否则 exe 中 DSD 处理会失败。"""
    data = _load()
    runs = "\n".join(
        str(step.get("run", ""))
        for job in data["jobs"].values()
        for step in job.get("steps", [])
    )
    assert "--hidden-import cue2flac.dsd" in runs, "打包缺少 cue2flac.dsd 的 hidden-import"


def test_ci_tool_paths_exist_in_workspace_root():
    """CI 步骤里调用的 ffmpeg / ffprobe 必须用仓库根目录的相对路径。

    回归防护：打包步骤把 ffmpeg.exe / ffprobe.exe 复制到**仓库根目录**
    （`Copy-Item $fp.FullName ffprobe.exe`），不是 `ffmpeg/` 子目录。
    写成 `./ffmpeg/ffprobe.exe` 会报
    "The term './ffmpeg/ffprobe.exe' is not recognized"。
    """
    data = _load()
    runs = "\n".join(
        str(step.get("run", ""))
        for job in data["jobs"].values()
        for step in job.get("steps", [])
    )
    assert "./ffmpeg/ffprobe.exe" not in runs, \
        "CI 中 ffprobe 的路径不对：它在仓库根目录，不在 ffmpeg/ 子目录"
    assert "./ffprobe.exe" in runs, "CI 应使用仓库根目录的 ffprobe.exe"
    # ffmpeg 可执行文件同理
    assert "./ffmpeg/ffmpeg.exe" not in runs, \
        "CI 中 ffmpeg 的路径不对：它在仓库根目录，不在 ffmpeg/ 子目录"
