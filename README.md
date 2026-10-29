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

1. 点击「浏览…」选择 `.cue` 文件。
2. 程序会自动填入音频文件与默认输出目录（CUE 同目录下的 `<专辑名>` 文件夹）。如有必要可手动修改。
3. 在曲目列表中确认曲目、起点、终点无误。
4. 点击「开始分轨」，等待完成。

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
| `cue` | CUE 文件路径（位置参数，必填） |
| `-o, --out` | 输出目录（默认：CUE 同目录下的 `<专辑名>` 文件夹） |
| `-a, --audio` | 指定整轨音频文件（覆盖 CUE 中 `FILE` 指向） |
| `--no-cover` | 不复制封面 |
| `--verify` | 分轨后校验每条音轨 PCM 无损 |

---

## 从源码打包为便携 exe

```bash
pip install pyinstaller
pyinstaller --noconfirm --onefile --name cue2flac --windowed ^
  --hidden-import cue2flac --hidden-import cue2flac.cli ^
  --hidden-import cue2flac.cue_parser --hidden-import cue2flac.splitter ^
  --hidden-import cue2flac.ffmpeg_provider --hidden-import tkinter ^
  --add-binary "ffmpeg.exe;ffmpeg" --add-binary "ffprobe.exe;ffmpeg" ^
  main.py
```

项目已包含 GitHub Actions（`/.github/workflows/build.yml`），推送 `main` 分支或手动触发即可在 Windows runner 上自动下载 ffmpeg、打包并发布到 Release 与 Artifacts。

---

## 项目结构

```
cue2flac/
├── cue2flac/            # 核心包
│   ├── cue_parser.py     # CUE 解析
│   ├── ffmpeg_provider.py# ffmpeg / ffprobe 定位（含打包内嵌）
│   ├── splitter.py       # 分轨与校验核心
│   └── cli.py            # 命令行入口
├── gui.py               # Tkinter 图形界面
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
