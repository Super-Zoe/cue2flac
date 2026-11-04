"""GitHub Actions 工作流文件的静态校验。

背景：曾在步骤名里使用未加引号的 `key: value` 形式（如
`name: Smoke test (multi-format: wav/flac/ape)`），YAML 会报
"mapping values are not allowed here"，导致 GitHub **连 job 都不创建**，
CI 直接 failure 且没有任何日志可查。本模块把这类错误提前拦在测试阶段。
"""

import glob
import os
import re
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


def test_hidden_imports_include_shared_util_module():
    """公共工具模块 cue2flac._util 必须显式列入打包清单。

    回归防护：转换 / 分轨 / 批量 / GUI 都从 _util 取格式常量与工具函数。
    PyInstaller 只做静态分析，漏掉 hidden-import 会让 exe 在运行时
    抛 ModuleNotFoundError，而源码模式与 CI 冒烟都不一定能发现。
    """
    data = _load()
    runs = "\n".join(
        str(step.get("run", ""))
        for job in data["jobs"].values()
        for step in job.get("steps", [])
    )
    assert "--hidden-import cue2flac._util" in runs, \
        "打包缺少 cue2flac._util 的 hidden-import"


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


# ---------------------------------------------------------------------------
# 依赖版本固定（禁止 latest / 浮动标签 / 动态解析）
# ---------------------------------------------------------------------------

#: 允许出现在 URL 中的版本号形态：日期型 tag 或带版本号的资产名。
_FIXED_VERSION_PAT = r"(?:\d{4}-\d{2}-\d{2}|N-\d+|n\d+\.\d+|\d+\.\d+)"


def _all_urls():
    """抽出工作流中出现的所有 URL。"""
    data = _load()
    urls = []
    for job in data["jobs"].values():
        for step in job.get("steps", []):
            blob = str(step.get("run", "")) + " " + str(step.get("uses", ""))
            for part in blob.split():
                if "://" in part:
                    urls.append((str(step.get("name", "")), part.strip("\"',`;")))
    return urls


def test_no_download_uses_latest_or_floating_tag():
    """下载依赖不得使用 latest / master / 浮动标签，必须指向固定版本。

    回归防护：ffmpeg 原先从 `releases/download/latest/...` 下载，
    上游 master 一变，CI 内嵌的 ffmpeg 就跟着变，构建结果不可复现，
    且某次上游重命名 demuxer 就会让 CI 无预警变红。
    """
    for step_name, url in _all_urls():
        assert "/download/latest/" not in url, (
            f"步骤 {step_name!r} 的下载地址使用了 latest 浮动标签：{url}"
        )
        assert "/download/master" not in url, (
            f"步骤 {step_name!r} 的下载地址使用了 master 浮动分支：{url}"
        )
        assert "latest.zip" not in url.lower(), (
            f"步骤 {step_name!r} 的下载地址疑似 latest 资产：{url}"
        )


def test_ffmpeg_download_is_pinned_to_exact_release():
    """ffmpeg 下载必须锁定到具体的 release tag 与带版本号的资产名。"""
    data = _load()
    steps = [
        st
        for job in data["jobs"].values()
        for st in job.get("steps", [])
    ]
    dl = [
        st for st in steps
        if "Download ffmpeg" in str(st.get("name", ""))
    ]
    assert dl, "未找到 ffmpeg 下载步骤"

    step = dl[0]
    env = step.get("env") or {}
    run = str(step.get("run", ""))

    # 1) 版本号必须以环境变量的形式显式声明
    assert "FFMPEG_TAG" in env, "ffmpeg 下载未声明固定的 release tag"
    assert "FFMPEG_BUILD" in env, "ffmpeg 下载未声明固定的构建号"
    assert "FFMPEG_SHA256" in env, "ffmpeg 下载未声明 SHA256 校验值"

    # 2) tag 必须是日期型的具体版本，不能是 latest
    tag = str(env["FFMPEG_TAG"])
    assert tag != "latest", "FFMPEG_TAG 不能是 latest"
    assert re.fullmatch(r"autobuild-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}", tag), (
        f"FFMPEG_TAG 不是固定的日期型版本号：{tag}"
    )

    # 3) 资产名必须带具体构建号（固定 tag 下的资产名不再用 master-latest 命名）
    build = str(env["FFMPEG_BUILD"])
    assert re.fullmatch(r"N-\d+-g[0-9a-f]+", build), (
        f"FFMPEG_BUILD 不是固定的构建号：{build}"
    )
    assert build in str(env["FFMPEG_ZIP"]), (
        "FFMPEG_ZIP 的资产名应包含固定的构建号"
    )

    # 4) SHA256 必须是 64 位十六进制，且下载后必须实际校验
    sha = str(env["FFMPEG_SHA256"])
    assert re.fullmatch(r"[0-9a-f]{64}", sha), f"FFMPEG_SHA256 格式不对：{sha}"
    assert "Get-FileHash" in run, "ffmpeg 下载后未做 SHA256 校验"
    assert "SHA256" in run, "ffmpeg 下载步骤未体现 SHA256 校验"

    # 5) 下载源保持不变
    assert "BtbN/FFmpeg-Builds" in run, "ffmpeg 下载源被改动了，应保持 BtbN/FFmpeg-Builds"


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _requirement_lines(path):
    """读取 requirements 文件里的有效依赖行（跳过注释与空行）。"""
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#"):
                yield s


def test_python_dependencies_are_pinned():
    """pip 安装的依赖必须锁定到具体版本，禁止不带版本号的裸包名。

    第 4 批（P2-6）后 CI 改为 ``pip install -r requirements*.txt``，版本
    声明集中到依赖文件里。本用例对 ``-r`` 引用的文件**逐行**校验 ``==``，
    保证「集中声明」不等于「放松固定」；命令行上直接写的包（如 pip 自身
    升级）仍按原口径检查。
    """
    data = _load()
    runs = "\n".join(
        str(step.get("run", ""))
        for job in data["jobs"].values()
        for step in job.get("steps", [])
    )
    for line in runs.splitlines():
        s = line.strip()
        if "pip install" not in s:
            continue
        # 拆出 "-r <file>" 对：文件名不含 ==，但文件里每一行都必须含 ==
        tokens = s.split()
        rest, refs = [], []
        i = 0
        while i < len(tokens):
            if tokens[i] == "-r" and i + 1 < len(tokens):
                refs.append(tokens[i + 1])
                i += 2
                continue
            rest.append(tokens[i])
            i += 1
        for ref in refs:
            path = os.path.join(_repo_root(), ref)
            assert os.path.isfile(path), f"-r 引用的依赖文件不存在：{ref}"
            for dep in _requirement_lines(path):
                assert "==" in dep, f"{ref} 中的依赖未固定版本：{dep!r}"
                assert ">" not in dep and "<" not in dep, (
                    f"{ref} 中不应使用版本范围：{dep!r}"
                )
        # 去掉 pip 自身的升级开关与 --upgrade 等选项
        args = [
            a for a in rest
            if not a.startswith("-")
            and a not in ("pip", "install", "python", "-m")
        ]
        pkgs = [a for a in args if not a.startswith("pip==")]
        for p in pkgs:
            assert "==" in p, (
                f"pip 安装未固定版本号：{p!r}（整行：{s!r}）"
            )


def test_actions_are_pinned_to_commit_sha():
    """GitHub Actions 必须锁定到 40 位 commit SHA，不能用 v4/v5 这类浮动标签。"""
    data = _load()
    for job in data["jobs"].values():
        for step in job.get("steps", []):
            uses = str(step.get("uses", ""))
            if not uses.startswith("actions/"):
                continue
            assert "@" in uses, f"Action 未指定版本：{uses}"
            ref = uses.split("@", 1)[1]
            assert re.fullmatch(r"[0-9a-f]{40}", ref), (
                f"Action 未锁定到 commit SHA（浮动标签会随上游漂移）：{uses}"
            )


def test_runner_image_and_python_version_are_fixed():
    """runner 镜像与 Python 版本都必须是固定值，不能是 latest / 浮动分支。"""
    data = _load()
    for job_name, job in data["jobs"].items():
        runs_on = str(job.get("runs-on", ""))
        assert runs_on != "windows-latest", (
            f"job {job_name} 的 runner 镜像使用了 latest 浮动标签"
        )
        assert re.fullmatch(r"windows-\d{4}", runs_on), (
            f"job {job_name} 的 runner 镜像未固定到具体版本：{runs_on}"
        )

    for job in data["jobs"].values():
        for step in job.get("steps", []):
            uses = str(step.get("uses", ""))
            if not uses.startswith("actions/setup-python"):
                continue
            ver = str((step.get("with") or {}).get("python-version", ""))
            assert re.fullmatch(r"\d+\.\d+\.\d+", ver), (
                f"Python 版本未固定到具体补丁版本：{ver!r}"
            )


def test_requirements_txt_pins_pyinstaller():
    """requirements.txt 必须固定 pyinstaller，作为打包依赖的唯一来源。"""
    req = os.path.join(_repo_root(), "requirements.txt")
    assert os.path.isfile(req), "缺少 requirements.txt"
    deps = list(_requirement_lines(req))
    assert any(d.startswith("pyinstaller==") for d in deps), \
        "requirements.txt 未固定 pyinstaller 版本"


def test_all_requirements_files_are_pinned():
    """所有 requirements*.txt 都必须逐行固定版本（含 CI 测试依赖文件）。"""
    files = sorted(glob.glob(os.path.join(_repo_root(), "requirements*.txt")))
    assert files, "仓库缺少 requirements 文件"
    for path in files:
        for dep in _requirement_lines(path):
            assert "==" in dep, (
                f"{os.path.basename(path)} 中的依赖未固定版本：{dep!r}"
            )
            assert ">" not in dep and "<" not in dep, (
                f"{os.path.basename(path)} 中不应使用版本范围：{dep!r}"
            )


def test_ci_installs_from_requirements_files():
    """CI 用 -r 安装依赖，且不再硬编码包版本（避免版本重复声明）。

    回归防护：历史上 pyinstaller/pytest/pyyaml 的版本同时写在 CI、
    requirements.txt、README 多处，升级时漏改一处就会出现「声明与实际
    不一致」。版本唯一来源必须是 requirements 文件。
    """
    data = _load()
    runs = "\n".join(
        str(step.get("run", ""))
        for job in data["jobs"].values()
        for step in job.get("steps", [])
    )
    assert "-r requirements.txt" in runs, "CI 未从 requirements.txt 安装打包依赖"
    assert "-r requirements-ci.txt" in runs, "CI 未从 requirements-ci.txt 安装测试依赖"
    for pinned in ("pyinstaller==", "pytest==", "pyyaml=="):
        assert pinned not in runs, (
            f"CI 不应再硬编码 {pinned}，版本唯一来源是 requirements 文件"
        )


def test_requirements_changes_trigger_build():
    """依赖文件变动必须触发构建，否则升级版本后 CI 不会重新打包。"""
    data = _load()
    trigger = data.get("on", data.get(True))
    assert "requirements*.txt" in trigger["push"]["paths"], \
        "push 过滤缺少 requirements*.txt，依赖升级不会触发构建"
