# cue2flac

> 根据 CUE 分轨文件，把整轨无损音频（FLAC / WAV / APE / WavPack / TTA / TAK / M4A / …）拆分为多首独立 FLAC 曲目。
> 便携、跨平台，可打包为**双击即用的单文件 exe（已内嵌 ffmpeg/ffprobe，无需安装任何环境）**。

---

## 目录

- [特性](#特性)
- [工作原理（无损链路）](#工作原理无损链路)
- [下载与安装](#下载与安装)
- [使用](#使用)
  - [图形界面（单一拖放区）](#图形界面单一拖放区)
  - [批量处理（扫描整个文件夹）](#批量处理扫描整个文件夹)
  - [命令行](#命令行)
- [输出文件与文件夹命名规则](#输出文件与文件夹命名规则)
- [输出验证（可播放 / 参数匹配 / 无损无截断）](#输出验证可播放--参数匹配--无损无截断)
- [配置持久化](#配置持久化)
- [项目结构](#项目结构)
- [从源码打包为便携 exe](#从源码打包为便携-exe)
- [开发与测试](#开发与测试)
- [License](#license)

---

## 特性

- **无损分轨**：整轨音频解码为 PCM → 按 CUE 的 `INDEX 01` / 下一条 `INDEX 01` 切出区段 → FLAC 编码器重新封装。只要不改变采样率与位深，输出 PCM 与源对应区段**逐样本一致**（可用 `--verify` 校验）。
- **多格式输入**：凡 ffmpeg 支持的整轨音频均可，包括但不限于 FLAC、WAV、APE、WavPack（.wv）、TTA、TAK、M4A。
- **CUE 兼容**：自动识别 `INDEX 00/01`、`PERFORMER` / `TITLE`（专辑级与曲目级）、`REM GENRE/DATE/COMMENT`、`FILE` 指向；支持 GBK / UTF-8(-SIG) / Shift-JIS / CP1252 / Latin-1 多种编码自动探测。
- **元数据保留**：专辑名、曲目名、表演者、流派、年份、曲目序号（`NN/总数`）写入每个 FLAC；源文件内嵌的封面会自动复制（可用 `--no-cover` 关闭）。
- **两种用法**：图形界面（双击即用，单一拖放区）与命令行（适合批量 / 脚本）。
- **批量处理**：把一个文件夹（可递归子目录）丢进去，自动发现其中**全部 `.cue`**，逐个解析并分轨；解析失败 / 音频缺失的会**跳过并记录原因**，最后给出**成功与跳过的汇总**。
- **纯净输出**：文件名形如 `01. 曲目名.flac`（有表演者时为 `01. 表演者 - 曲目名.flac`），自动清理非法字符。
- **响应式界面**：窗口缩放时，文字自动换行、按钮自动折行，内容过长可滚动到达，不会被裁切或“消失”。

---

## 工作原理（无损链路）

```
整轨音频 → ffmpeg 解码为 PCM → 取 [INDEX 01, 下一条 INDEX 01) 区段 → FLAC 编码器重新封装
```

FLAC 是无损压缩，对 PCM 做有损重编码在技术上不可能；因此“解码整轨 → 用 FLAC 编码重新封装”得到的 PCM 与直接从源文件取对应区段完全一致。`--verify` 会对“源对应区段”与“输出文件”各自解码为 `s32le` 裸 PCM 求 MD5，确保分轨结果逐字节无损、无截断。

---

## 下载与安装

### 方式一：直接使用便携 exe（推荐）

前往 **Releases** 页面下载 `cue2flac.exe`，双击即可运行（已内嵌 ffmpeg/ffprobe，不依赖任何外部环境）。

- **双击（无参数）** → 启动图形界面，把文件夹或 `.cue` 文件拖入「拖放区域」，预览解析结果后一键转换。
- **命令行**（在终端里运行） → 见[命令行](#命令行)一节。

> 便携 exe 是“控制台构建”：双击进入 GUI 时，`main.py` 会自动隐藏启动用的黑框，保持体验干净；带参数走 CLI 时则保留控制台以显示进度与错误。

### 方式二：从源码运行

```bash
# 需要 Python 3.9+（CI 构建使用 3.11），并自行准备 ffmpeg / ffprobe
#   —— 放到 PATH 中，或放到项目根目录的 ffmpeg/ 文件夹下
pip install -r requirements.txt

python main.py            # 启动图形界面
python main.py album.cue  # 命令行分轨
```

---

## 使用

### 图形界面（单一拖放区）

> 窗口可自由缩放：拖放提示、状态栏、输出说明等文字会在方框变窄时**自动换行**；按钮在宽度不足时**自动折行**；内容过长时通过**可滚动**面板（结果列表自带滚动条，整体内容另有右侧滚动条）到达，不会因尺寸缩小而裁切或“消失”。

1. 把 **文件夹** 或 **一个或多个 `.cue` 文件** 直接拖进窗口顶部的「拖放区域」；也可点击「**选择文件…**」/「**选择文件夹…**」按钮（非 Windows 下为回退入口）。
2. 程序自动解析路径、**递归遍历其中的全部 `.cue`**，并在下方「解析结果」列表中展示每个 CUE 的**文件名、路径、专辑、表演者、曲目数**，展开可见每首曲目的**序号 / 标题 / 表演者 / 时长**（解析失败的也会列出并标注原因）。
   - 不需要的条目可**右键点击**该 CUE（或选中后点「**删除选中**」/ 按 `Delete` 键）将其从解析结果中移除；内容过长时拖动结果面板**右侧的滚动条**即可顺畅浏览。
3. （可选）在「**输出目录**」栏设置统一输出位置：点「**选择…**」挑一个文件夹作为所有专辑的统一根目录；留空则使用**默认行为**——每个专辑输出到其 CUE 所在目录下的 `<专辑名>` 子文件夹；点「**恢复默认**」可清除自定义选择。该选择会被记住，下次启动自动复用（见[配置持久化](#配置持久化)）。
4. 确认解析结果无误后，点击「**开始转换**」，程序即依据各 CUE 信息对相关音频进行无损分轨（输出到上述目录）。
5. 完成后弹出汇总，底部日志区列出成功项与跳过项及原因，并附「输出验证汇总」（每首逐项校验：可播放 / 参数匹配 / 无损无截断，标注 ✓ 或 ✗ 及原因）。**验证默认开启**，任一首未通过则该 CUE 被跳过。

> 拖放支持单个文件、多个文件与整个文件夹；拖入时区域高亮（橙），放下成功闪绿、未找到 CUE 闪红。

### 批量处理（扫描整个文件夹）

当手头是一个装满整轨专辑的文件夹（含多层子目录）时，无需逐个挑选 CUE：

- **图形界面（推荐）**：直接在唯一的「**拖放区域**」把整个文件夹（或同时拖入多个 `.cue` 文件）拖进去，也可点「**选择文件夹…**」按钮。程序会递归遍历其中的**全部 `.cue`**，在「**解析结果**」列表中展示（每 CUE 一行：文件名 / 路径 / 专辑 / 表演者 / 曲目数，展开见每首明细），点击「**开始转换**」即可批量分轨。
- **命令行**：把目录作为位置参数，或用 `--dir` 指定：

```bash
# 递归扫描 D:/Music/整轨 下的全部 CUE 并分轨
python main.py "D:/Music/整轨"
python main.py --dir "D:/Music/整轨"

# 不递归子目录（仅处理该目录下的 .cue）
python main.py "D:/Music/整轨" --no-recursive

# 统一输出到一个根目录（每个专辑一个子文件夹）
python main.py --dir "D:/Music/整轨" -o "D:/Music/拆分结果"

# 批量时也支持 --audio / --no-cover / --verify
python main.py --dir "D:/Music/整轨" --verify
```

批量模式的行为：
- 自动发现目录（含子目录）下所有 `.cue`，**按小写路径排序、去重**后逐个处理；
- 逐个解析音轨元数据；解析失败（编码/语法错误、无 `TRACK`）或分轨失败（音频缺失等）的文件会**跳过**，并在汇总中附上原因；
- 最终返回汇总：**扫描总数、成功数、跳过数**，以及成功列表与跳过列表（含原因）。

### 命令行

```bash
# 基本用法：按 CUE 拆分，输出到默认目录
python main.py "示例专辑.cue"

# 指定输出目录与音频文件
python main.py album.cue -o "D:/Music/拆分结果" -a "album.flac"

# 不复制封面 / 分轨后校验 PCM 无损
python main.py album.cue --no-cover --verify
```

参数说明：

| 参数 | 说明 |
| --- | --- |
| `cue` | CUE 文件路径；也可传**目录**，自动批量扫描其中的 `.cue` 文件 |
| `-o, --out` | 输出目录；批量模式下作为统一输出根目录。**不指定时依次回退**：配置文件中的默认值 → 各 CUE 所在目录下的「专辑名」子文件夹 |
| `-a, --audio` | 指定整轨音频文件（覆盖 CUE 中 `FILE` 指向） |
| `--no-cover` | 不复制封面 |
| `--verify` | 分轨后逐文件验证输出：可播放性、音频参数（采样率 / 位深 / 声道数）与源匹配、PCM 逐字节无损且无截断。**CLI 默认关闭**（以省去全量解码耗时）；GUI 默认开启 |
| `--dir` | 要批量扫描的目录（等价于把目录作为位置参数传入） |
| `-r, --recursive` | 递归遍历子目录（默认开启） |
| `--no-recursive` | 不遍历子目录，仅处理指定目录下的 `.cue` |
| `-v, --version` | 显示版本号 |

---

## 输出文件与文件夹命名规则

### 输出文件夹
- **默认**（未指定 `-o` / 未设置配置默认值）：`<CUE 所在目录>/<专辑名>`
- **指定了统一输出目录 `<out>`**：`<out>/<专辑名>`
- **专辑名来源**：优先用 CUE 中的 `album_title` 字段；若没有则用 CUE 文件名（去扩展名）；个别单文件场景兜底为 `output`。
- **安全化**：把 `\ / : * ? " < > |` 以及回车/换行/制表符替换成 `_`，去掉首尾空格/点/下划线，为空时兜底 `output`，总长度截断到 120 字符。
- **防重名**：同一批处理内若出现完全相同的输出目录路径，第二个起自动加 `_1`、`_2` 后缀。

### 每首音轨文件
- **基本格式**：`NN. 标题.flac`（`NN` 为两位补零的序号，`01`、`02`…；标题取自 CUE，缺失时兜底 `Track NN`）。
- **去重优化**：若该曲目有表演者，则改用 `NN. 表演者 - 标题.flac`（仅当总长度 ≤ 120 字符时采用，否则退回 `NN. 标题`）。
- **安全化**：非法字符同样替换成 `_`，去首尾 `. _ 空格`，为空兜底 `TrackNN`，总长度截断到 120 字符。
- 后缀固定 `.flac`。

### 同时写入的标签（metadata，不是文件名）
`title`、`artist`（曲目 performer，缺失则用专辑 performer）、`album`、`genre`、`date`、`track=NN/总数`。

示例：CUE 文件 `D:\Music\Album A\album.cue`（`album_title="My Album"`，2 首）——默认输出到 `D:\Music\Album A\My Album\`，生成 `01. First Song.flac`、`02. Second Song.flac`；若指定 `out=E:\Out`，则输出到 `E:\Out\My Album\`。

---

## 输出验证（可播放 / 参数匹配 / 无损无截断）

转换完成后，程序可对每个输出 FLAC 做三项校验，确保结果可靠：

1. **可播放性**：`ffprobe` 能识别音轨，且 `ffmpeg` 能将其完整解码到 null（无报错），排除文件损坏、截断到无法解码的情况。
2. **参数匹配**：输出流的 **采样率 / 声道数 / 位深** 与源文件音频流逐一比对，确认没有因重封装而丢失或多出声道、改变采样精度。
3. **无损 + 无截断**：把「源对应区段」与「输出文件」各自解码为同一 `s32le` PCM 求 MD5，逐字节一致即证明音质无损且未发生截断。

- **图形界面**：「开始转换」默认开启验证，结果打印在底部日志区的「输出验证汇总」中（每首标注 ✓/✗ 及失败原因），全部通过才视为成功；任一首不通过则该 CUE 会被跳过。
- **命令行**：加 `--verify` 开启（默认关闭）；验证失败的音轨会让该 CUE 处理失败并在汇总中报错。

> 说明：FLAC 本身是无损压缩，对 PCM 做有损重编码在技术上不可能。验证的核心价值在于**确认整条处理链路（解码 → 切割 → 重新封装）没有引入截断、读错区段或参数漂移**，并对源文件本身是否完好给出反馈。

---

## 配置持久化

输出目录偏好写入配置文件，下次启动自动复用：

- **便携 exe**：与 `cue2flac.exe` 同目录的 `cue2flac_config.json`
- **源码运行**：用户配置目录 `~/.config/cue2flac/config.json`

读取失败或文件不存在时返回空配置，不影响主流程。GUI 中「选择…」写入、「恢复默认」清除。

---

## 项目结构

```
cue2flac/
├── cue2flac/              # 核心包
│   ├── cue_parser.py       # CUE 解析（多编码探测、时间码换算、音轨定位）
│   ├── splitter.py         # 分轨核心（ffmpeg 切片 + 元数据 + 文件名安全化）
│   ├── batch.py            # 批量扫描目录 / 汇总结果 / 输出目录防重名
│   ├── verify.py           # 转换后验证（可播放 / 参数匹配 / 无损无截断）
│   ├── config.py           # 轻量配置持久化（默认输出目录）
│   ├── layout.py           # 按钮自动折行算法（纯函数，供 GUI 复用）
│   ├── ffmpeg_provider.py  # ffmpeg / ffprobe 定位（打包内嵌 / 本地 / PATH）
│   ├── cli.py              # 命令行入口
│   └── __init__.py         # 版本号等
├── gui.py                 # Tkinter 图形界面（单一拖放区 + 可滚动画布 + 响应式布局）
├── dnd.py                 # Windows 原生拖放支持（COM IDropTarget + ctypes，零依赖）
├── main.py                # 程序入口（有参数走 CLI，否则启动 GUI；双击自动隐藏黑框）
├── main.spec              # PyInstaller 规格（本地打包参考）
├── requirements.txt       # 运行/开发依赖
├── tests/                 # 单元测试与 GUI 冒烟测试
│   ├── test_cue_parser.py
│   ├── test_batch.py
│   ├── test_verify.py
│   ├── test_layout.py
│   └── test_gui_smoke.py  # GUI 布局冒烟（需 tkinter；无显示环境自动跳过）
├── .github/workflows/     # 自动构建便携 exe 并发布到 Release
├── README.md
├── LICENSE
└── .gitignore
```

---

## 从源码打包为便携 exe

仓库已包含 GitHub Actions（`.github/workflows/build.yml`）：推送 `main` 分支或手动触发，即可在 Windows runner 上自动下载 ffmpeg、打包并发布到 Release 与 Artifacts。本地手动打包命令与 CI 一致（**onefile 控制台构建，已内嵌 ffmpeg**）：

```bash
pip install pyinstaller
pyinstaller --noconfirm --onefile --name cue2flac ^
  --hidden-import cue2flac ^
  --hidden-import cue2flac.cli ^
  --hidden-import cue2flac.cue_parser ^
  --hidden-import cue2flac.splitter ^
  --hidden-import cue2flac.batch ^
  --hidden-import cue2flac.config ^
  --hidden-import cue2flac.verify ^
  --hidden-import cue2flac.layout ^
  --hidden-import cue2flac.ffmpeg_provider ^
  --hidden-import tkinter ^
  --hidden-import dnd ^
  --add-binary "ffmpeg.exe;ffmpeg" ^
  --add-binary "ffprobe.exe;ffmpeg" ^
  main.py
```

> 注意：`--hidden-import` 必须包含 `config` / `verify` / `layout` 等所有被动态导入或反射引用的子模块，否则打包后运行会因找不到模块而报错。ffmpeg/ffprobe 通过 `--add-binary` 内嵌到 `ffmpeg/` 子目录下，由 `ffmpeg_provider.resolve_tool` 在运行时定位。

---

## 开发与测试

```bash
pip install -r requirements.txt   # 含 pytest 等
python -m pytest -q                # 运行全部测试
```

- `tests/test_cue_parser.py`、`test_batch.py`、`test_verify.py`：纯逻辑单测，无需界面即可运行。
- `tests/test_layout.py`：按钮折行算法的纯函数测试。
- `tests/test_gui_smoke.py`：GUI 布局冒烟（构造窗口、控件可见性、缩窄不裁切、滚动条可驱动画布等）；**需要 tkinter 与显示环境**，无显示环境时自动跳过。
- CI 在打包前会先跑 `test_layout.py` 与 `test_gui_smoke.py`，打包后还会做端到端分轨冒烟 + 批量扫描冒烟，确保产物可用。

---

## License

[MIT](LICENSE)
