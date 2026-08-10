# 音频批量截取工具

音频处理工具，支持截取、淡入淡出、10 段均衡器、多格式转换。

## 功能

- **裁剪** — 时长模式 (截取前 N 秒) / 范围模式 (指定起止点)
- **淡入淡出** — 淡入淡出独立调节，对数/线性曲线
- **均衡器** — 10 段图形 EQ (31Hz-16kHz)，±12dB
- **多格式** — 支持 MP3 / AAC / WAV / OGG / FLAC 输出
- **预设** — 保存/加载处理参数
- **预览** — 15 秒片段即时试听
- **批量处理** — 文件夹一键处理
- **GUI + CLI** — 图形界面和命令行双模式

## 下载

前往 [Releases](https://github.com/yyyyylw/audio-trimmer/releases) 下载最新版本。

下载 `音频处理工具_vX.X.X.zip`，解压后双击 `setup.bat` 安装，或直接运行 `音频处理工具.exe`。

## 命令行用法

```bash
# 单文件
python mp3_tool.py input.mp3 -o output.mp3

# 批量
python mp3_tool.py "文件夹" --batch

# 更多参数
python mp3_tool.py input.mp3 --start 30 --end 90 --format wav --fade-in 2 --fade-out 5
```

## 依赖

- Python 3.10+
- ffmpeg (程序会自动在常见位置查找)
- tkinterdnd2 (可选，用于拖拽支持)

## 构建

```bash
pip install pyinstaller tkinterdnd2
pyinstaller --onefile --windowed --name "音频处理工具" --hidden-import tkinterdnd2 --icon app_icon.ico --version-file version_info.txt mp3_tool.py
```

## 作者

烟岚余雨
