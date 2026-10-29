# cue2flac

> 根据 CUE 分轨文件，把整轨无损音频（FLAC / WAV / APE / WavPack / TTA / …）拆分为多首独立 FLAC 曲目。
> 便携、跨平台，可打包为**双击即用的单文件 exe（已内嵌 ffmpeg，无需安装任何环境）**。

---

## 特性

- **无损分轨**：FLAC 是无损压缩，对整轨解码后用 FLAC 编码器重新封装，得到的 PCM 与直接从源文件取对应区段**逐样本一致**（可用 `--verify` 校验）。
- **多格式输入**：FLAC、WAV、APE、WavPack、TTA 等 ffmpeg 支持的整轨音频。
- **CUE 兼容**：自动识别 `INDEX 00/01`、`PERFORMER`/`TITLE`（专辑级与曲目级）、`REM GENRE/DATE`、`FILE` 指向；支持 GBK / UTF-8 / Shift-JIS / CP1252 等多种编码。
- **元数据保留**：专辑名、曲目名、表演者、流派、年份、曲目序号写入每个 FLAC；源文件内嵌封面会自动复制。
- **两种用法**：图形界面（双击即用）与命令行（适合批量 / 脚本）。
- **批量处理**：可指定一个文件夹，自动扫描（含递归子目录）其中**全部 `.cue` 文件**，逐个解析音轨并分轨；格式错误 / 无法解析 / 音频缺失的文件会**跳过并记录原因**，最后给出**成功与跳过的结果汇总**。
- **拖放支持**：图形界面下可直接把 **CUE 文件 / 音频文件 / 文件夹** 拖到输入框（或「拖放区」整体自动识别），支持单个、多个文件及整个文件夹；拖入时输入框高亮（橙），放下成功闪绿、被拒绝闪红。
- **纯净输出**：文件名形如 `01. 曲目名.flac`，自动清理非法字符。

---

## 下载与安装

### 方式一：直接使用便携 exe（推荐）

前往 **Releases** 页面下载 `cue2flac.exe`，双击即可运行（已内嵌 ffmpeg/ffprobe，不依赖任何外部环境）。
图形界面会让你选择 CUE 文件，预览曲目后一键分轨。

### 方式二：从源码运行

```bash
# 需要 Python 3.9+，并自行准备 ffmpeg / ffprobe（在 PATH 中，或放到项目根目录 ffmpeg/ 下）
python main.py            # 启动图形界面
python main.py album.cue  # 命令行分轨
```

---

## 使用

### 图形界面

1. 点击「浏览…」选择 `.cue` 文件；**或直接把 `.cue` 文件拖进「CUE 文件」输入框 / 拖放区**。
2. 程序会自动填入音频文件与默认输出目录（CUE 同目录下的 `<专辑名>` 文件夹）。如有必要可手动修改，或：
   - 把**整轨音频文件**拖进「音频文件」框；
   - 把**目标文件夹**拖进「输出目录」框（拖文件则取其所在目录）。
3. 在曲目列表中确认曲目、起点、终点无误。
4. 点击「开始分轨」，等待完成。

> 拖放支持单文件、多文件与整个文件夹；拖到最外层「拖放区」时会按内容自动识别并路由到正确字段。

### 批量处理（扫描整个文件夹）

当手头是一个装满整轨专辑的文件夹（含多层子目录）时，无需逐个挑选 CUE：

- **图形界面**：点击「**批量处理文件夹…**」，选择根目录，程序会递归扫描其中所有 `.cue`，逐个解析并分轨（每个专辑输出到其 CUE 所在目录下的 `<专辑名>` 子文件夹），完成后弹出汇总，日志区也会列出成功项与跳过项及原因。
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
- 自动发现目录（含子目录）下所有 `.cue`，**按路径排序**后逐个处理；
- 逐个解析音轨元数据；解析失败（编码/语法错误、无 TRACK）或分轨失败（音频缺失等）的文件会**跳过**，并在汇总中附上原因；
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
| `cue` | CUE 文件路径；也可传**目录**，自动批量扫描其中的 `.cue` |
| `-o, --out` | 输出目录；批量模式下作为统一输出根目录 |
| `-a, --audio` | 指定整轨音频文件（覆盖 CUE 中 `FILE` 指向） |
| `--no-cover` | 不复制封面 |
| `--verify` | 分轨后校验每条音轨 PCM 无损 |
| `--dir` | 要批量扫描的目录（等价于把目录作为位置参数传入） |
| `-r, --recursive` | 递归遍历子目录（默认开启） |
| `--no-recursive` | 不遍历子目录，仅处理指定目录下的 `.cue` |

---

## 从源码打包为便携 exe

```bash
pip install pyinstaller
pyinstaller --noconfirm --onefile --name cue2flac --windowed ^
  --hidden-import cue2flac --hidden-import cue2flac.cli ^
  --hidden-import cue2flac.cue_parser --hidden-import cue2flac.splitter ^
  --hidden-import cue2flac.batch --hidden-import cue2flac.ffmpeg_provider ^
  --hidden-import tkinter --hidden-import dnd ^
  --add-binary "ffmpeg.exe;ffmpeg" --add-binary "ffprobe.exe;ffmpeg" ^
  main.py
```

> 说明：仓库的 CI 采用**控制台 exe**（双击进入 GUI 时由 `main.py` 自动隐藏控制台黑框），既能获得干净的双击体验，又能在出错时显示真实错误。本地手动打包若想隐藏黑框可保留 `--windowed`，但需配合 `main.py` 的窗口化日志重定向。

项目已包含 GitHub Actions（`/.github/workflows/build.yml`），推送 `main` 分支或手动触发即可在 Windows runner 上自动下载 ffmpeg、打包并发布到 Release 与 Artifacts。

---

## 项目结构

```
cue2flac/
├── cue2flac/            # 核心包
│   ├── cue_parser.py     # CUE 解析
│   ├── ffmpeg_provider.py# ffmpeg / ffprobe 定位（含打包内嵌）
│   ├── splitter.py       # 分轨与校验核心
│   ├── batch.py          # 批量扫描目录 / 汇总结果
│   └── cli.py            # 命令行入口
├── gui.py               # Tkinter 图形界面（含拖放）
├── dnd.py               # Windows 原生拖放支持（COM IDropTarget + ctypes，零依赖）
├── main.py              # 程序入口（有参数走 CLI，否则启动 GUI）
├── main.spec            # PyInstaller 规格（本地打包用）
├── tests/               # 单元测试
├── .github/workflows/   # 自动构建便携 exe
├── README.md
├── LICENSE
└── requirements.txt
```

---

## 无损说明

分轨逻辑为：

```
整轨音频 → ffmpeg 解码为 PCM → 取 [INDEX 01, 下一条 INDEX 01) 区段 → FLAC 编码器重新封装
```

由于 FLAC 是无损编解码器，只要不改变采样率与位深，输出 PCM 与源文件对应区段**完全一致**。
`--verify` 会对源区段与输出分别解码为 s24le 裸 PCM 并比对 MD5，确保分轨结果逐字节无损。

---

## License

[MIT](LICENSE)
