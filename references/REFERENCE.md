# Gemini Video Tutor — 详细参考

## 命令行参数（analyze.py）

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `input` | 本地视频文件路径（位置参数） | - |
| `-o FILE` | 输出 Markdown 文件 | stdout |
| `--profile NAME` | 分析方法档案（`prompts/<NAME>.md`） | config `analysis.profile`（tutorial） |
| `--config PATH` | 指定 config.yaml 路径 | env `GEMINI_TUTOR_CONFIG` 或 skill 目录 |
| `-m MODEL` | 模型（覆盖 env/config） | config `gemini.model` |
| `--auth STYLE` | 认证：`auto`/`bearer`/`api-key` | config `gemini.auth`（auto） |
| `--chunk-minutes N` | 长视频分段时长上限 | config `analysis.chunk_minutes`（40） |
| `--no-chunk` | 禁用自动分段 | - |
| `--parallel-chunks` | 单视频分段并发分析（见下「并行模型」） | config `analysis.parallel_chunks`（false） |
| `--keyframe-dir DIR` | 截图保存目录 | `<output>_frames/` |
| `--keyframe-interval N` | 截图间隔（秒，备用） | config（5） |
| `--prompt TEXT` | 临时主提示词，覆盖档案的 `@prompt` | - |
| `--api-key KEY` | 覆盖 env/config 的 key | - |
| `--base-url URL` | 覆盖 env/config 的 base_url | - |
| `--resume MD_FILE` | 断点续传：重试失败分段 | - |
| `--batch DIR_OR_FILE` | 批量模式：目录或列表文件 | - |
| `--output-dir DIR` | 批量输出目录 | 当前目录 |
| `--workers N` | 批量/分段并发数 | config `batch.workers`（2） |

## 命令行参数（ask.py — 多轮追问会话）

| 参数 | 说明 |
|------|------|
| `video question` | 位置参数：视频路径 + 问题。同一视频自动续接同一会话 |
| `-c FILE` | 把已有分析文档（如 analyze.py 的产物）注入为本次追问的背景资料 |
| `-o FILE` | 把回答存为 Markdown 并抽取其引用的截图到 `<FILE>_frames/` |
| `--new` | 重置该视频的会话后再提问 |
| `--show` | 打印该视频的会话历史 |
| `--list` | 列出所有会话 |
| `-m / --config / --auth / --api-key / --base-url` | 同 analyze.py |

## 并行模型

三个互不冲突的层级：

1. **多视频并行**：`analyze.py --batch ... --workers N`，N 个视频同时各自完整分析。
2. **单视频分段并行**：`--parallel-chunks`（或 config `analysis.parallel_chunks: true`），长视频的各分段同时分析,速度约提升 workers 倍。代价：各段相互看不到（无累积上下文），全局编号退化为按时间的小节标题，可能有少量重复表述。**默认关（顺序模式质量最好）**，赶时间再开。失败段同样打 `CHUNK_FAILED` 标记，`--resume` 兼容。
3. **多轮会话**（ask.py）：与上述正交，单视频上传一次后反复追问。

## 多轮追问会话机制（ask.py）

- 视频经 File API **上传一次**，引用（fileUri）与对话历史持久化在 `sessions/<sha1(视频绝对路径)>.json`（已 gitignore）。
- 每次提问：回放历史（角色 user/model）+ 新问题；视频引用挂在最早的 user 轮上。
- File API 文件 ~48h 过期：超过 47h 主动重传；请求遭 400/403/404 拒绝时自动重传一次并重试。
- 代理无 File API 时降级 inline base64：压缩产物持久化在 sessions/ 旁复用，多轮依旧可用（每轮 payload 较大）。
- 历史长度由 `chat.max_history`（默认 20 轮）限制，超出丢最早轮次。
- 回答被系统提示词强制要求标注 `[MM:SS]` 时间戳依据；`-o` 时引用截图会被抽帧嵌入。
- 超过 ~55 分钟的视频建议先 `analyze.py` 分段分析，再用 `-c 分析.md` 携带结果进会话。

## 配置解析优先级

**命令行参数 > 环境变量 > config.yaml > 内置默认值**。

- config.yaml 全部可配置项见 [example.config.yaml](../example.config.yaml)。
- 环境变量：`GEMINI_API_KEY`、`GEMINI_BASE_URL`、`GEMINI_MODEL`、`GEMINI_AUTH`、`GEMINI_TUTOR_PROFILE`、`GEMINI_TUTOR_CONFIG`、`GEMINI_TUTOR_PROMPTS_DIR`、`GEMINI_TUTOR_SESSION_DIR`、`GEMINI_MAX_RETRIES`。
- 空字符串（如模板里的 `api_key: ""`）视为"未设置"，不会覆盖更低优先级来源。
- YAML 解析：装了 PyYAML 用之，否则用内置极简解析器（仅支持两级嵌套+标量，足够 config.yaml）。

## 认证兼容性

`analyze.py` 据 `auth` 选择请求头：

- `auto`（默认）：base_url 含 `googleapis.com` → `x-goog-api-key`；否则 → `Authorization: Bearer`。
- `api-key`：强制 `x-goog-api-key`（Google 官方 REST 风格）。
- `bearer`：强制 `Authorization: Bearer`（多数中转/代理）。

这让**官方 Gemini 端点和第三方代理都能用**同一套脚本。

## 提示词档案（profiles）

`prompts/<name>.md`，分节标记 `# @system` / `# @prompt` / `# @continuation`：

- `@prompt` 必填；`@system`、`@continuation` 可选。
- `@continuation` 缺省时使用内置通用续写模板。占位符：`{chunk_index}` `{total_chunks}` `{start_time}` `{end_time}` `{previous_doc}`。
- 分节标记前的内容（注释、frontmatter）被忽略。
- `analysis.require_evidence: true` 且档案未含 `screenshot_` 规则时，自动追加时间戳+截图硬性要求。
- 每次运行实时读取，**改完即时生效**。

## 容器自动转换

Gemini 直接支持 mp4/mov/avi/webm/mpeg/flv/wmv/3gp。其它容器（.mkv/.ts 等）在 `video.auto_convert: true` 时自动 `ffmpeg` 转 mp4（先尝试 `-c copy` 快速重封装，失败再重编码）。

## 断点续传机制

长视频分段时若某些段超时/失败：

1. 成功段照常写入，失败段标记 `> ⚠️ **Chunk N (MM:SS - MM:SS) 处理失败：** 错误信息`。
2. 文档底部记录源视频路径、模型、**分析方法**、分段参数（`<!-- ANALYSIS_META ... -->`）。
3. `--resume tutorial.md` 从文档解析失败标记与参数，加载同一档案，只重试失败段。
4. 全部恢复后标记被替换；仍有失败可再次 `--resume`。

无需额外状态文件，MD 文档本身即全部状态。

## 输出结构

```
tutorial.md              # 文档（底部含源视频路径、模型、方法、分析参数）
tutorial_frames/         # 关键帧截图
  screenshot_00_00.jpg
  screenshot_00_05.jpg
  ...
```

`tutorial` 档案文档结构：教程目标 / 事前准备 / 操作步骤（含界面状态·操作内容·结果·注意·参考截图）/ 关键时间点 / 完整语音转录。其它档案结构由各自 `@prompt` 决定。

## 支持的模型

`gemini-3-flash-preview` / `gemini-3-pro-preview` / `gemini-2.5-flash` / `gemini-2.5-pro` 等（以你的端点可用为准）。
