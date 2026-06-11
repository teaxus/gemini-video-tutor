# 🎬 Gemini Video Tutor

用 **Gemini** 分析视频，产出**结构化、可溯源**的 Markdown 文档。默认把视频整理成"任何人照着就能复现"的操作教程，分析方法可自由配置（摘要、字幕、复盘……）。

- ✅ 整段视频直传 Gemini（File API，最大 ~2GB），原生 1FPS 采样 + 音频分析
- ✅ 每条结论带依据：时间戳 `[MM:SS]` + 关键画面 `[screenshot_MM_SS.jpg]`，自动抽帧嵌图
- ✅ 长视频自动分段，顺序携带前文上下文，保证连贯（赶时间可 `--parallel-chunks` 分段并行）
- ✅ 分段失败 `--resume` 断点续传，文档本身即状态，无需额外状态文件
- ✅ 批量并发处理：每视频一个子目录、自动跳过已完成（断点续传）、生成批量报告
- ✅ 官方端点与 OpenAI 兼容中转（OpenRouter/one-api 等）自动适配协议
- ✅ **多轮来回深度追问**（`ask.py`）：视频上传一次（File API ~48h），对话历史持久化，连续追问不重新上传、不重新分析
- ✅ 分析方法（提示词）外置成文件，改完即时生效，无需重启
- ✅ 跨通用 agent（Claude Code / Codex / openclaw 等）：零强制依赖，PyYAML 可选，认证自动适配官方/代理

> 这是一个 [Agent Skill](https://agentskills.io/)。它通常由 AI agent 调用，你也可以直接命令行运行。

---

## 快速开始

### 1. 依赖

- **Python 3.8+**
- **FFmpeg**（`ffmpeg` + `ffprobe`）：`brew install ffmpeg`（macOS）/ `sudo apt-get install ffmpeg`（Debian/Ubuntu）
- Gemini API key（官方或任意兼容中转）
- PyYAML 可选（`pip install pyyaml`）

### 2. 配置

```bash
python3 scripts/setup.py --init-config   # 从模板生成 config.yaml
```

编辑 `config.yaml` 填入你的 key：

```yaml
gemini:
  api_key: "你的key"
  base_url: "https://generativelanguage.googleapis.com"  # 官方；或你的代理地址
  model: "gemini-2.5-flash"
```

> `config.yaml` 已被 `.gitignore` 忽略，密钥不会进 git。也可改用环境变量 `GEMINI_API_KEY` / `GEMINI_BASE_URL` / `GEMINI_MODEL`。

### 3. 体检 & 运行

```bash
python3 scripts/setup.py                              # 确认一切就绪
python3 scripts/analyze.py "/path/to/video.mp4" -o tutorial.md
```

产物：`tutorial.md` + 同名 `tutorial_frames/` 截图目录。

---

## 常用命令

| 目的 | 命令 |
|------|------|
| 默认教程 | `analyze.py video.mp4 -o out.md` |
| 内容摘要 | `analyze.py video.mp4 --profile summary -o out.md` |
| 指定模型/分段 | `analyze.py v.mp4 -m gemini-3-pro-preview --chunk-minutes 30 -o out.md` |
| 临时提示词 | `analyze.py v.mp4 --prompt "只提取所有命令行命令" -o cmds.md` |
| 断点续传 | `analyze.py --resume out.md` |
| 批量并发 | `analyze.py --batch ./videos/ --output-dir ./out/ --workers 3` |
| 长视频分段并行 | `analyze.py long.mp4 --parallel-chunks --workers 3 -o out.md` |

完整参数见 [references/REFERENCE.md](references/REFERENCE.md)。

---

## 多轮来回深度追问

对视频"聊天"而不是重新生成文档——视频只上传一次，之后随便问：

```bash
python3 scripts/ask.py video.mp4 "第3分钟用的是什么工具？"
python3 scripts/ask.py video.mp4 "它和上一步是什么关系？"      # 自动继续同一会话
python3 scripts/ask.py video.mp4 -c tutorial.md "教程里哪步和视频对不上？"  # 带上已有分析追问
python3 scripts/ask.py video.mp4 "..." -o answer.md            # 保存回答并抽取引用截图
python3 scripts/ask.py video.mp4 --new "重新开始"               # 重置会话
python3 scripts/ask.py --list                                   # 所有会话
```

- 回答强制标注 `[MM:SS]` 时间戳依据，找不到依据会明说。
- 会话状态在 `sessions/`（已 gitignore）；File API 文件 ~48h 过期后自动重传。
- 代理不支持 File API 时自动降级 inline（压缩产物缓存复用），多轮依旧可用。

## 三种并行/交互模式怎么选

| 场景 | 用法 |
|------|------|
| 多个视频各出一份文档 | `--batch --workers N`（多视频并行） |
| 一个长视频尽快出文档 | `--parallel-chunks`（分段并行，牺牲段间连贯性） |
| 对一个视频反复提问、逐步深入 | `ask.py`（会话式，上传一次） |

---

## 自定义分析方法

提示词放在 [`prompts/`](prompts/) 下，每个 `.md` 是一种方法：

```
prompts/
  tutorial.md   # 操作教程（默认）
  summary.md    # 内容摘要
  我的方法.md    # ← 复制一份改成你自己的
```

格式（详见 `prompts/tutorial.md`）：

```markdown
# @system
（角色与总原则，可选）

# @prompt
（主分析提示词，必填）

# @continuation
（长视频分段续写模板，可选；占位符 {chunk_index} {total_chunks} {start_time} {end_time} {previous_doc}）
```

选用：`--profile 我的方法`，或 config 里 `analysis.profile: 我的方法`。**改完立即生效，无需重启。**

---

## 在线视频

本 skill 只处理本地文件。在线 URL 请先用 [video-downloader](https://github.com/teaxus/video-downloader) 下载：

```bash
python3 scripts/setup.py --install-downloader   # 自动克隆到相邻目录
```

下载后用本地路径作为输入。**默认把视频下载进分析输出目录**（video-downloader 第三个参数指定），让 `视频 + 分析.md + 截图` 在同一目录里，方便整体归档/删除，也保证 `--resume` 和 `ask.py` 会话引用的路径长期有效；想集中存放则在 config 设 `video.download_dir: ~/Videos/library` 之类的固定目录：

```bash
python3 <video-downloader>/scripts/video_downloader.py "<URL>" 1080p ~/Downloads/my_analysis/
python3 scripts/analyze.py ~/Downloads/my_analysis/视频.mp4 -o ~/Downloads/my_analysis/分析.md
```

---

## 输出结构

```
tutorial.md          # 文档，底部含源视频路径/模型/方法等元数据（供 --resume 使用）
tutorial_frames/     # 关键帧截图
  screenshot_00_05.jpg
  ...
```

---

## 安全说明

- **不要把真实 key 写进会提交的文件。** key 只放 `config.yaml`（已忽略）或环境变量。
- 视频会上传到你配置的 Gemini 端点（官方或你的代理）。请确认对该端点的信任与合规。
- 若 key 曾经泄露，去对应控制台轮换。
