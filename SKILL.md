---
name: gemini-video-tutor
description: >
  Analyze a video with Gemini and turn it into a structured Markdown document
  (default: a reproducible step-by-step tutorial with keyframe screenshots).
  用 Gemini 分析视频并产出结构化文档，默认把视频整理成带关键帧截图的可复现教程。
  分析方法（提示词）可配置：换 prompts/<名称>.md 即可改成摘要、字幕、复盘等任意方法，改完即时生效。
  整段视频直传 Gemini，长视频自动分段并顺序累积上下文，分段失败可 --resume 续传，支持批量并发与分段并行。
  支持多轮来回深度追问（ask.py）：视频上传一次，对话历史持久化，连续追问无需重新上传/重新分析。
  仅处理本地视频文件。在线 URL（YouTube、B站、抖音等）需先用 video-downloader skill 下载。
  Use when the user wants to analyze/summarize a video, convert it into a tutorial with
  screenshots, or interactively ask follow-up questions about a video.
compatibility: "Requires: Python 3.8+, FFmpeg. Gemini API key via config.yaml or GEMINI_API_KEY. PyYAML optional."
metadata:
  author: teaxus
  version: "3.0"
  openclaw:
    emoji: "🎬"
    requires:
      bins: ["ffmpeg", "ffprobe", "python3"]
      env: ["GEMINI_API_KEY", "GEMINI_BASE_URL", "GEMINI_MODEL"]
    primaryEnv: "GEMINI_API_KEY"
---

# Gemini Video Tutor

用 Gemini 分析视频，产出**结构化、可溯源**的 Markdown 文档。默认方法是把视频整理成"任何人照着就能复现"的操作教程（每步含"看到什么→做什么→变成什么"、关键帧截图、逐字转录），但**分析方法是可配置的**——见下文「分析方法（提示词档案）」。

每条结论都带依据：时间戳 `[MM:SS]` + 关键画面 `[screenshot_MM_SS.jpg]`（脚本据此自动从视频抽帧并嵌入文档）。

## ⚠️ 给 agent 的执行流程（务必按序）

1. **先体检**。运行一次，根据输出决定下一步：
   ```bash
   python3 ./scripts/setup.py
   ```
   - 缺 ffmpeg → 按提示安装（`brew install ffmpeg` / `apt-get install ffmpeg`）。
   - 没有 config.yaml → `python3 ./scripts/setup.py --init-config` 生成，然后引导用户在 `config.yaml` 填 `gemini.api_key`（该文件已被 .gitignore 忽略）。
   - 体检 exit code 为 0 才进入分析。
2. **判断输入类型**：
   - **本地文件** → 直接分析。
   - **在线 URL**（http/https、youtube/bilibili/抖音等）→ 本 skill 不下载。先确认 video-downloader 已安装（setup.py 会报告；未装可 `python3 ./scripts/setup.py --install-downloader` 自动克隆），用它下载到本地，再用返回的本地路径分析。
3. **选模式**（关键分流）：
   - 用户要**完整的结构化文档**（教程/摘要/字幕等）→ `analyze.py`。方法默认 `tutorial`，可用 `--profile <名称>` 或 config `analysis.profile` 换。
   - 用户是**对视频提问/追问/讨论**（"视频里X是什么""第几分钟出现了Y"）→ `ask.py`。同一视频的连续提问自动续接同一会话，**不要**每问一次就重跑 analyze.py。
4. **运行分析**，把产物（`.md` + `_frames/`）位置告诉用户。
5. **多轮来回深度分析**：分析完成后用户继续追问 → 用 `ask.py`，并用 `-c 产物.md` 把已生成的文档带进会话（视频只上传一次，历史自动延续）。要重产出不同形态的完整文档时才回到 `analyze.py` 换 `--profile` 重跑；分段失败用 `--resume`。

## 配置

配置优先级：**命令行参数 > 环境变量 > config.yaml > 内置默认值**。

- 模板见 [example.config.yaml](./example.config.yaml)，包含所有可配置项及注释（base_url、key、模型、重试、超时、温度、分段阈值、并发等）。
- 本地配置写在 `config.yaml`（已被 .gitignore 忽略，密钥不会进 git）。用 `setup.py --init-config` 从模板生成。
- 也可只用环境变量，适合 CI / 无文件场景：
  ```bash
  export GEMINI_API_KEY="your_key"
  export GEMINI_BASE_URL="https://generativelanguage.googleapis.com"  # 或代理地址，不带 /v1beta 后缀
  export GEMINI_MODEL="gemini-2.5-flash"
  ```

**认证方式 `auth`**（关键兼容点）：`auto`（默认）会对官方 `googleapis.com` 用 `x-goog-api-key`，对其它代理用 `Authorization: Bearer`。所以官方端点和中转代理都能用。需要时可用 `--auth api-key|bearer` 或 config 强制。

## 分析方法（提示词档案）

提示词不写死在代码里，而是放在 [prompts/](./prompts/) 下，每个 `.md` 是一种分析方法（"档案"）：

- 内置 `tutorial`（操作教程，默认）、`summary`（内容摘要）。
- 档案格式见 [prompts/tutorial.md](./prompts/tutorial.md)：用 `# @system` / `# @prompt` / `# @continuation` 三段。`@prompt` 必填，其余可选。
- **新增自己的方法**：复制一份改成 `prompts/我的方法.md`，再用 `--profile 我的方法` 或 config 选用。
- **动态生效**：档案每次运行时实时读取，改完立即生效，无需重启 skill 或 agent。
- `analysis.require_evidence: true` 时会自动为没写截图规则的档案追加"必须标注时间戳+截图"的硬性要求。

## 使用方法

`analyze.py` 指 [scripts/analyze.py](./scripts/analyze.py)。

```bash
# 单个视频（默认 tutorial 方法）
python3 ./scripts/analyze.py "/path/to/video.mp4" -o tutorial.md

# 换分析方法：内容摘要
python3 ./scripts/analyze.py "video.mp4" --profile summary -o summary.md

# 指定模型 / 分段时长
python3 ./scripts/analyze.py "lecture.mp4" -m gemini-3-pro-preview --chunk-minutes 30 -o out.md

# 临时自定义提示词（覆盖档案主提示词，不改文件）
python3 ./scripts/analyze.py "video.mp4" --prompt "只提取视频里出现的所有命令行命令" -o cmds.md

# 断点续传：仅重试失败的分段（视频路径/模型/方法都从文档底部元数据自动读取）
python3 ./scripts/analyze.py --resume tutorial.md

# 批量并发：目录或列表文件
python3 ./scripts/analyze.py --batch /path/to/videos/ --output-dir ./output/ --workers 3

# 单个长视频分段并行（更快；代价是段间无上下文，详见 REFERENCE）
python3 ./scripts/analyze.py "long.mp4" --parallel-chunks --workers 3 -o out.md
```

### 多轮来回深度追问（ask.py）

视频**上传一次**（File API 保留 ~48h），之后每次提问复用上传与对话历史——这才是"来回沟通"该用的命令：

```bash
python3 ./scripts/ask.py "video.mp4" "第3分钟用的是什么工具？"
python3 ./scripts/ask.py "video.mp4" "它和上一步是什么关系？"     # 自动继续同一会话
python3 ./scripts/ask.py "video.mp4" -c tutorial.md "这份教程哪里和视频不一致？"  # 携带已有分析
python3 ./scripts/ask.py "video.mp4" "..." -o answer.md          # 保存回答+抽取引用截图
python3 ./scripts/ask.py "video.mp4" --new "重新开始"            # 重置会话
python3 ./scripts/ask.py --list                                  # 查看所有会话
```

回答同样强制带 `[MM:SS]` 时间戳依据。会话存在 `sessions/`（已 gitignore），历史长度由 config `chat.max_history` 控制。

详细参数、断点续传机制、输出格式见 [详细参考文档](./references/REFERENCE.md)。人类向说明见 [README.md](./README.md)。

## 第三方依赖

- **FFmpeg**（`ffmpeg` + `ffprobe`）：必需。分段、抽帧、不支持的容器（.mkv/.ts 等）自动转 mp4。
- **[video-downloader](https://github.com/teaxus/video-downloader)** skill：仅当输入是在线 URL 时需要。setup.py 可探测并 `--install-downloader` 自动克隆。
- **PyYAML**：可选。装了用它解析 config，没装自动降级为内置极简解析器，功能不受影响。

## 安装位置

遵循 [Agent Skills 开放标准](https://agentskills.io/)，可放在以下任一位置：

| 位置 | 适用范围 |
|------|---------|
| `~/.claude/skills/gemini-video-tutor/` | Claude Code 个人级 |
| `~/.codex/skills/gemini-video-tutor/` | Codex 个人级 |
| `~/.agents/skills/gemini-video-tutor/` | 通用个人级（多客户端共享） |
| `~/.copilot/skills/gemini-video-tutor/` | VS Code Copilot 个人级 |
| `.agents/skills/gemini-video-tutor/` · `.github/skills/...` | 项目级（随仓库共享） |
