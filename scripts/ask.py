#!/usr/bin/env python3
"""
Gemini Video Tutor — multi-turn deep-dive chat over one video (会话式追问).

The video is uploaded ONCE (Gemini File API keeps it ~48h); every follow-up
question reuses the uploaded file plus the persisted conversation history, so
repeated CLI invocations form one continuous conversation — no re-upload, no
re-analysis. Sessions live in sessions/<id>.json (gitignored), keyed by the
video's absolute path.

  python3 scripts/ask.py video.mp4 "第3分钟用的是什么工具？"
  python3 scripts/ask.py video.mp4 "它和上一步是什么关系？"        # 自动继续同一会话
  python3 scripts/ask.py video.mp4 -c tutorial.md "这份教程里哪些步骤和视频不一致？"
  python3 scripts/ask.py video.mp4 --new "重新开始"                # 重置会话
  python3 scripts/ask.py video.mp4 --show                          # 查看会话历史
  python3 scripts/ask.py --list                                    # 列出所有会话
  python3 scripts/ask.py video.mp4 "..." -o answer.md              # 保存回答并抽取引用截图

If the File API is unavailable (some proxies), falls back to inline base64 —
the (possibly compressed) file is kept next to the session and re-sent each
turn, so multi-turn still works, just with a larger per-request payload.

Python 3.8+.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from skill_config import load_config, resolve_session_dir, Config  # noqa: E402
from analyze import (GeminiClient, make_client, ensure_supported_container,  # noqa: E402
                     compress_video, get_file_size_mb, get_duration, format_time,
                     extract_referenced_keyframes, post_process_document)

# File API files expire after 48h; re-upload proactively a bit earlier.
FILE_TTL_SECONDS = 47 * 3600

CHAT_SYSTEM = """你是一位视频深度分析助手。用户已提供完整视频，你要基于视频内容回答连续多轮追问，支持逐步深入。

硬性规则：
- 每个结论必须标注依据时间戳 [MM:SS]（原始视频的绝对时间），不许编造；视频中找不到依据就明确说"视频中没有出现"。
- 需要展示关键画面时，标注 screenshot_MM_SS.jpg（会被自动提取为截图）。
- 结合之前轮次的对话上下文回答，用户的"它/这个/上面说的"指代要正确解析。
- 直接回答，不要重复问题，不要客套。"""


# ─── Session store ───────────────────────────────────────────────────────────

def session_id(video_path: str) -> str:
    return hashlib.sha1(os.path.abspath(video_path).encode("utf-8")).hexdigest()[:16]


def session_file(cfg: Config, video_path: str) -> Path:
    return resolve_session_dir(cfg) / f"{session_id(video_path)}.json"


def load_session(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print(f"  Warning: corrupt session {path}, starting fresh.", file=sys.stderr)
    return {}


def save_session(path: Path, session: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session, ensure_ascii=False, indent=1), encoding="utf-8")


def reset_session(path: Path):
    inline = load_session(path).get("inline_path", "")
    # Only remove inline copies we created inside the session dir, never user files.
    if inline and Path(inline).parent == path.parent and os.path.exists(inline):
        os.remove(inline)
    if path.exists():
        path.unlink()


# ─── Video attachment (upload once, reuse) ───────────────────────────────────

def _invalidate_upload(session: dict):
    session.pop("file_uri", None)
    session.pop("file_name", None)
    session.pop("uploaded_at", None)


def ensure_video_parts(client: GeminiClient, session: dict, video_path: str,
                       sess_path: Path) -> list:
    """Return Gemini parts referencing the video, uploading only when needed.

    Priority: persisted inline copy > still-valid File API upload > new upload
    > inline fallback (persisted next to the session for reuse).
    """
    inline = session.get("inline_path", "")
    if inline and os.path.exists(inline):
        import base64
        with open(inline, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        return [{"inlineData": {"mimeType": session.get("mime", "video/mp4"), "data": b64}}]

    if session.get("file_uri") and \
            time.time() - session.get("uploaded_at", 0) < FILE_TTL_SECONDS:
        return [{"fileData": {"mimeType": session.get("mime", "video/mp4"),
                              "fileUri": session["file_uri"]}}]

    # (Re)upload.
    src, mime, is_temp = ensure_supported_container(video_path, client.auto_convert)
    try:
        info = client.upload_file(src, mime)
        if info and info.get("uri"):
            session.update({"file_uri": info["uri"], "file_name": info.get("name", ""),
                            "mime": mime, "uploaded_at": time.time()})
            session.pop("inline_path", None)
            save_session(sess_path, session)
            return [{"fileData": {"mimeType": mime, "fileUri": info["uri"]}}]

        # Inline fallback — persist the payload file so later turns reuse it.
        payload = src
        if get_file_size_mb(src) > client.inline_max_mb:
            payload = compress_video(src, client.inline_max_mb)
            mime = "video/mp4"
        if payload != video_path:
            persisted = sess_path.parent / f"{sess_path.stem}_inline{Path(payload).suffix}"
            sess_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(payload, persisted)
            payload = str(persisted)
        _invalidate_upload(session)
        session.update({"inline_path": payload, "mime": mime})
        save_session(sess_path, session)

        import base64
        with open(payload, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        print(f"  Using inline base64 ({get_file_size_mb(payload):.1f}MB, "
              f"kept for reuse: {payload})", file=sys.stderr)
        return [{"inlineData": {"mimeType": mime, "data": b64}}]
    finally:
        if is_temp and src != video_path and os.path.exists(src) \
                and src != session.get("inline_path"):
            os.remove(src)


# ─── Conversation building ───────────────────────────────────────────────────

def build_contents(history: list, question: str, video_parts: list) -> list:
    """Replay history as role turns; the video rides on the earliest user turn."""
    contents = []
    attached = False
    for h in history:
        parts = [{"text": h["text"]}]
        if not attached and h["role"] == "user":
            parts = list(video_parts) + parts
            attached = True
        contents.append({"role": h["role"], "parts": parts})
    q_parts = [{"text": question}]
    if not attached:
        q_parts = list(video_parts) + q_parts
    contents.append({"role": "user", "parts": q_parts})
    return contents


def trim_history(history: list, max_pairs: int) -> list:
    """Keep at most the last `max_pairs` Q/A rounds (history grows unbounded otherwise)."""
    limit = max(1, max_pairs) * 2
    if len(history) <= limit:
        return history
    trimmed = history[-limit:]
    if trimmed and trimmed[0]["role"] == "model":  # don't start replay mid-pair
        trimmed = trimmed[1:]
    return trimmed


# ─── Commands ────────────────────────────────────────────────────────────────

def cmd_list(cfg: Config) -> int:
    sdir = resolve_session_dir(cfg)
    files = sorted(sdir.glob("*.json")) if sdir.is_dir() else []
    if not files:
        print("(没有会话)")
        return 0
    for f in files:
        s = load_session(f)
        turns = len(s.get("history", [])) // 2
        age_h = (time.time() - s.get("uploaded_at", s.get("created_at", time.time()))) / 3600
        mode = "inline" if s.get("inline_path") else ("file-api" if s.get("file_uri") else "?")
        print(f"  {f.stem}  {turns} 轮  [{mode}, 上传于 {age_h:.1f}h 前]  {s.get('video', '?')}")
    return 0


def cmd_show(sess_path: Path) -> int:
    s = load_session(sess_path)
    history = s.get("history", [])
    if not history:
        print("(会话为空)")
        return 0
    print(f"会话视频: {s.get('video', '?')}  共 {len(history) // 2} 轮\n")
    for h in history:
        tag = "🙋" if h["role"] == "user" else "🤖"
        text = h["text"]
        print(f"{tag} {text if len(text) <= 500 else text[:500] + ' …[截断]'}\n")
    return 0


def cmd_ask(cfg: Config, video_path: str, question: str,
            context_file: str = "", output: str = "") -> int:
    sess_path = session_file(cfg, video_path)
    session = load_session(sess_path)
    if not session:
        session = {"video": os.path.abspath(video_path),
                   "created_at": time.time(), "history": []}

    client = make_client(cfg)
    history = trim_history(session.get("history", []), cfg.max_history)

    q_text = question
    if context_file:
        doc = Path(context_file).read_text(encoding="utf-8")
        q_text = (f"以下是此前对这个视频生成的分析文档，作为追问的背景资料：\n"
                  f"<已有分析>\n{doc}\n</已有分析>\n\n{question}")

    if not history:
        duration = get_duration(video_path)
        print(f"新会话: {Path(video_path).name} ({duration / 60:.1f} min) "
              f"| model={cfg.model}", file=sys.stderr)
        if duration > 55 * 60:
            print("  ⚠️ 视频超过 ~55 分钟，单次会话可能超出模型上下文；"
                  "建议先用 analyze.py 分段分析，再用 -c 把结果带进会话追问。", file=sys.stderr)
    else:
        print(f"继续会话（已有 {len(history) // 2} 轮）", file=sys.stderr)

    video_parts = ensure_video_parts(client, session, video_path, sess_path)
    contents = build_contents(history, q_text, video_parts)

    try:
        answer = client.generate(contents, system_instruction=CHAT_SYSTEM)
    except urllib.error.HTTPError as e:
        if session.get("file_uri") and e.code in (400, 403, 404):
            # Uploaded file likely expired/unknown to this endpoint — re-upload once.
            print(f"  Upload reference rejected ({e.code}); re-uploading...", file=sys.stderr)
            _invalidate_upload(session)
            save_session(sess_path, session)
            video_parts = ensure_video_parts(client, session, video_path, sess_path)
            contents = build_contents(history, q_text, video_parts)
            answer = client.generate(contents, system_instruction=CHAT_SYSTEM)
        else:
            raise

    if not answer:
        print("ERROR: Gemini 返回了空回复。", file=sys.stderr)
        return 1

    history = history + [
        {"role": "user", "text": q_text, "ts": time.strftime("%Y-%m-%d %H:%M:%S")},
        {"role": "model", "text": answer, "ts": time.strftime("%Y-%m-%d %H:%M:%S")},
    ]
    session["history"] = trim_history(history, cfg.max_history)
    save_session(sess_path, session)

    if output:
        frames_dir = str(Path(output).with_suffix("")) + "_frames"
        frames = extract_referenced_keyframes(video_path, answer, frames_dir)
        rendered = post_process_document(answer, frames_dir)
        parent = Path(output).parent
        if str(parent) != ".":
            os.makedirs(parent, exist_ok=True)
        with open(output, "w", encoding="utf-8") as f:
            f.write(f"> 🙋 **{question}**\n\n{rendered}\n")
        print(f"\n📄 回答已保存: {output}"
              + (f"（{len(frames)} 张截图 → {frames_dir}/）" if frames else ""), file=sys.stderr)

    print("\n" + answer)
    return 0


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Gemini Video Tutor — 对一个视频进行多轮来回深度追问（上传一次，持续对话）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s video.mp4 "第3分钟用的是什么工具？"
  %(prog)s video.mp4 "它和上一步什么关系？"          # 自动继续同一会话
  %(prog)s video.mp4 -c tutorial.md "教程里哪步和视频不一致？"
  %(prog)s video.mp4 --new "重新开始"
  %(prog)s video.mp4 --show
  %(prog)s --list
""")
    parser.add_argument("video", nargs="?", help="Local video file path")
    parser.add_argument("question", nargs="?", help="Question about the video")
    parser.add_argument("-c", "--context", default="",
                        help="Markdown file (e.g. a previous analyze.py output) injected "
                             "as background for this question")
    parser.add_argument("-o", "--output", default="",
                        help="Also save the answer (with extracted screenshots) to this .md")
    parser.add_argument("--new", action="store_true",
                        help="Reset the session for this video before asking")
    parser.add_argument("--show", action="store_true", help="Print session history and exit")
    parser.add_argument("--list", action="store_true", dest="list_sessions",
                        help="List all sessions and exit")
    parser.add_argument("-m", "--model", default=None, help="Model name (overrides env/config)")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--auth", default=None, choices=["auto", "bearer", "api-key"])
    parser.add_argument("--api-style", default=None, choices=["auto", "gemini", "openai"])
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--base-url", default=None)
    args = parser.parse_args()

    cfg = load_config(args, args.config)

    if args.list_sessions:
        sys.exit(cmd_list(cfg))

    if not args.video:
        parser.print_help()
        sys.exit(1)
    if not os.path.exists(args.video):
        print(f"ERROR: File not found: {args.video}", file=sys.stderr)
        sys.exit(1)

    sess_path = session_file(cfg, args.video)
    if args.new:
        reset_session(sess_path)
        print("会话已重置。", file=sys.stderr)
    if args.show:
        sys.exit(cmd_show(sess_path))
    if not args.question:
        print("ERROR: 缺少问题。用法: ask.py <video> \"问题\"", file=sys.stderr)
        sys.exit(1)
    if args.context and not os.path.exists(args.context):
        print(f"ERROR: --context 文件不存在: {args.context}", file=sys.stderr)
        sys.exit(1)

    if not cfg.api_key:
        print("ERROR: Gemini API key not set. Use --api-key, env GEMINI_API_KEY, "
              "or config.yaml. Run scripts/setup.py to check setup.", file=sys.stderr)
        sys.exit(1)

    sys.exit(cmd_ask(cfg, args.video, args.question, args.context, args.output))


if __name__ == "__main__":
    main()
