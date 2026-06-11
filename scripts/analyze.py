#!/usr/bin/env python3
"""
Gemini Video Tutor v2 — Convert a video into a structured, reproducible document.

Core design:
  1. Upload the entire video to Gemini (via File API or inline base64) — let the
     model handle frame sampling and audio extraction natively.
  2. For long videos exceeding model context, split into chunks via ffmpeg and
     process sequentially: chunk N's prompt includes the accumulated document
     from chunks 0..N-1.
  3. Extract the keyframes the model referenced, save them, and embed them as
     image links in the final Markdown.
  4. Failed chunks are marked inline; `--resume` retries only those.

Configuration (CLI > env > config.yaml > defaults) and prompt profiles
(prompts/<name>.md) are handled by skill_config.py — see example.config.yaml.

Supports local video files. Online URLs must be downloaded first (see SKILL.md /
the video-downloader skill).

Python 3.8+.
"""

from __future__ import annotations

import os
import sys
import json
import argparse
import tempfile
import subprocess
import time
import base64
import urllib.request
import urllib.error
import re
import glob
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_config
from skill_config import load_config, load_profile, resolve_prompts_dir, Config, Profile

# ─── Defaults / fallbacks ──────────────────────────────────────────────────────

DEFAULT_MODEL = skill_config.DEFAULTS["model"]
DEFAULT_INLINE_MAX_MB = skill_config.DEFAULTS["inline_max_mb"]
KEYFRAME_INTERVAL_SEC = skill_config.DEFAULTS["keyframe_interval"]

# Used when a prompt profile does not supply its own @continuation section.
DEFAULT_CONTINUATION = """\
这是同一个长视频的第 {chunk_index}/{total_chunks} 段（时间范围 {start_time} - {end_time}）。

⚠️ 重要时间换算：本段视频内部的 00:00 对应原始完整视频的 {start_time}。请在输出中将所有时间戳换算为原始视频的绝对时间。

前面的段落已经整理出以下内容：

<已完成内容>
{previous_doc}
</已完成内容>

请继续整理当前视频段的内容，延续前面的文档：
1. 如有编号（如步骤），接续前文继续编号，不要从头开始
2. 时间戳必须换算为原始视频的绝对时间（本段 00:00 = 原始 {start_time}）
3. 不要重复前面已有的开头部分（目标、准备等）
4. 如果这是最后一段，在末尾补齐覆盖全片的汇总部分
5. 参考画面 screenshot_MM_SS.jpg 中的 MM_SS 也必须是原始视频的绝对时间"""

# Appended to the profile prompt in --parallel-chunks mode, where each chunk is
# analyzed independently (no accumulated context from earlier chunks).
PARALLEL_CHUNK_NOTE = """

# 分段说明（重要）
这是同一个长视频的第 {chunk_index}/{total_chunks} 段，对应原始视频 {start_time} - {end_time}。
本段是独立分析的（看不到其它段），请：
1. 所有时间戳必须换算为原始视频的绝对时间：本段内部 00:00 = 原始视频 {start_time}
2. 参考画面 screenshot_MM_SS.jpg 中的 MM_SS 同样必须是原始视频的绝对时间
3. 只输出本段内容对应的文档片段；不要写覆盖全片的总标题、前言或总结
4. 如有编号（如步骤），用「{start_time} 起的步骤」这类基于时间的小节标题，不要用全局编号"""

# Container -> mime for formats Gemini accepts directly. Others are converted.
GEMINI_VIDEO_MIME = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".webm": "video/webm",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv",
    ".3gp": "video/3gpp",
}


# ─── API Client ───────────────────────────────────────────────────────────────

class GeminiClient:
    """Video-analysis API client. Speaks two wire protocols:

      * Gemini native REST (`/v1beta/models/...:generateContent` + File API) —
        official endpoint and Gemini-style relays.
      * OpenAI-compatible chat completions (`/api/v1/chat/completions` with
        `video_url` content parts) — OpenRouter / one-api style relays.

    `api_style` selects the protocol. "auto" (default): the official Google
    endpoint (googleapis.com) speaks gemini native; every other endpoint is
    assumed to be an OpenAI-style relay (the de-facto standard for one-api /
    new-api / OpenRouter). Set "gemini" explicitly for Gemini-native relays.
    Internally everything stays in Gemini content format and is translated at
    request time.

    Auth style is selected per `auth`:
      * "auto"    -> x-goog-api-key for official googleapis.com, else Bearer
      * "api-key" -> always x-goog-api-key (official Google REST style)
      * "bearer"  -> always Authorization: Bearer (most proxies / relays)
    OpenAI-style endpoints always use Bearer.
    """

    USER_AGENT = "gemini-video-tutor/3.1"  # default urllib UA gets blocked by some CDNs

    def __init__(self, api_key: str, base_url: str, model: str = DEFAULT_MODEL,
                 auth: str = "auto", max_retries: int = 3, retry_delay: int = 15,
                 timeout: int = 600, max_output_tokens: int = 65536,
                 temperature: float = 0.4, inline_max_mb: float = DEFAULT_INLINE_MAX_MB,
                 auto_convert: bool = True, api_style: str = "auto"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.auth = (auth or "auto").lower()
        self.api_style = (api_style or "auto").lower()
        self.max_retries = max(1, int(max_retries))
        self.retry_delay = int(retry_delay)
        self.timeout = int(timeout)
        self.max_output_tokens = int(max_output_tokens)
        self.temperature = float(temperature)
        self.inline_max_mb = float(inline_max_mb)
        self.auto_convert = bool(auto_convert)

    def resolved_style(self) -> str:
        if self.api_style == "auto":
            # OpenAI-compatible is the de-facto relay standard; only Google's
            # own endpoint defaults to native (it has no /v1/chat/completions,
            # and native unlocks the File API for large uploads).
            return "gemini" if "googleapis.com" in self.base_url else "openai"
        return self.api_style

    # ── auth ──
    def _auth_headers(self) -> dict:
        if self.resolved_style() == "openai":
            return {"Authorization": f"Bearer {self.api_key}",
                    "User-Agent": self.USER_AGENT}
        style = self.auth
        if style == "auto":
            style = "api-key" if "googleapis.com" in self.base_url else "bearer"
        if style == "api-key":
            return {"x-goog-api-key": self.api_key, "User-Agent": self.USER_AGENT}
        return {"Authorization": f"Bearer {self.api_key}", "User-Agent": self.USER_AGENT}

    @property
    def generate_endpoint(self) -> str:
        return f"{self.base_url}/v1beta/models/{self.model}:generateContent"

    @property
    def openai_endpoint(self) -> str:
        base = self.base_url
        if base.endswith("/v1") or base.endswith("/api/v1"):
            return f"{base}/chat/completions"  # user already included the prefix
        prefix = "/api/v1" if "openrouter.ai" in base else "/v1"
        return f"{base}{prefix}/chat/completions"

    @property
    def upload_endpoint(self) -> str:
        return f"{self.base_url}/upload/v1beta/files"

    def upload_file(self, file_path: str, mime_type: str = "video/mp4"):
        """Upload a file via Gemini File API. Returns file metadata or None if unsupported."""
        if self.resolved_style() == "openai":
            return None  # no File API on OpenAI-style endpoints -> inline base64
        file_size = os.path.getsize(file_path)
        display_name = Path(file_path).name

        # Step 1: Initiate resumable upload
        metadata = json.dumps({"file": {"displayName": display_name}}).encode("utf-8")
        headers = {
            **self._auth_headers(),
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(file_size),
            "X-Goog-Upload-Header-Content-Type": mime_type,
            "Content-Type": "application/json",
        }

        try:
            req = urllib.request.Request(
                self.upload_endpoint, data=metadata, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                upload_url = resp.headers.get("X-Goog-Upload-URL") or resp.headers.get("x-goog-upload-url")
                if not upload_url:
                    print("  File API: no upload URL in response, falling back to inline.", file=sys.stderr)
                    return None
        except Exception as e:
            print(f"  File API upload initiation failed ({e}), falling back to inline.", file=sys.stderr)
            return None

        # Step 2: Upload file data
        try:
            with open(file_path, "rb") as f:
                file_data = f.read()

            headers2 = {
                **self._auth_headers(),
                "X-Goog-Upload-Command": "upload, finalize",
                "X-Goog-Upload-Offset": "0",
                "Content-Length": str(file_size),
                "Content-Type": mime_type,
            }
            req2 = urllib.request.Request(upload_url, data=file_data, headers=headers2, method="PUT")
            with urllib.request.urlopen(req2, timeout=self.timeout) as resp2:
                result = json.loads(resp2.read().decode("utf-8"))
                file_info = result.get("file", result)
                file_name = file_info.get("name", "")
                print(f"  Uploaded: {file_name} ({file_size / 1024 / 1024:.1f} MB)", file=sys.stderr)

                # Wait for processing
                self._wait_for_file(file_name)
                return file_info
        except Exception as e:
            print(f"  File API upload failed ({e}), falling back to inline.", file=sys.stderr)
            return None

    def _wait_for_file(self, file_name: str, max_wait: int = 300):
        """Poll file status until ACTIVE."""
        get_url = f"{self.base_url}/v1beta/{file_name}"
        headers = self._auth_headers()
        start = time.time()
        while time.time() - start < max_wait:
            try:
                req = urllib.request.Request(get_url, headers=headers, method="GET")
                with urllib.request.urlopen(req, timeout=30) as resp:
                    info = json.loads(resp.read().decode("utf-8"))
                    state = info.get("state", "ACTIVE")
                    if state == "ACTIVE":
                        return
                    elif state == "FAILED":
                        raise RuntimeError(f"File processing failed: {info}")
                    print(f"  File state: {state}, waiting...", file=sys.stderr)
            except urllib.error.HTTPError:
                pass
            time.sleep(5)
        print("  Warning: file processing wait timed out, proceeding anyway.", file=sys.stderr)

    def delete_file(self, file_name: str):
        """Delete an uploaded file."""
        try:
            url = f"{self.base_url}/v1beta/{file_name}"
            req = urllib.request.Request(url, headers=self._auth_headers(), method="DELETE")
            urllib.request.urlopen(req, timeout=15)
        except Exception:
            pass

    def _to_openai_messages(self, contents: list, system_instruction: str) -> list:
        """Translate Gemini-format contents into OpenAI chat messages.

        text -> {"type":"text"}; inlineData -> video_url data URL;
        fileData -> video_url with the original URI.
        """
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        for turn in contents:
            role = "assistant" if turn.get("role") == "model" else "user"
            content = []
            for part in turn.get("parts", []):
                if "text" in part:
                    content.append({"type": "text", "text": part["text"]})
                elif "inlineData" in part:
                    d = part["inlineData"]
                    content.append({"type": "video_url", "video_url": {
                        "url": f"data:{d.get('mimeType', 'video/mp4')};base64,{d['data']}"}})
                elif "fileData" in part:
                    content.append({"type": "video_url", "video_url": {
                        "url": part["fileData"].get("fileUri", "")}})
            if role == "assistant":  # assistant turns are plain text
                text = "\n".join(c["text"] for c in content if c.get("type") == "text")
                messages.append({"role": role, "content": text})
            else:
                messages.append({"role": role, "content": content})
        return messages

    def generate(self, contents: list, system_instruction: str = "",
                 max_output_tokens: int = 0, timeout: int = 0) -> str:
        """Send a generation request (protocol per api_style), return text response."""
        if self.resolved_style() == "openai":
            endpoint = self.openai_endpoint
            payload = {
                "model": self.model,
                "messages": self._to_openai_messages(contents, system_instruction),
                "max_tokens": max_output_tokens or self.max_output_tokens,
                "temperature": self.temperature,
            }
        else:
            endpoint = self.generate_endpoint
            payload = {
                "contents": contents,
                "generationConfig": {
                    "maxOutputTokens": max_output_tokens or self.max_output_tokens,
                    "temperature": self.temperature,
                }
            }
            if system_instruction:
                payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        data = json.dumps(payload).encode("utf-8")
        headers = {**self._auth_headers(), "Content-Type": "application/json"}
        req = urllib.request.Request(
            endpoint, data=data, headers=headers, method="POST"
        )

        for attempt in range(1, self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                    return self._extract_text(result)
            except urllib.error.HTTPError as e:
                error_body = e.read().decode("utf-8") if e.fp else ""
                if e.code in (429, 503) and attempt < self.max_retries:
                    wait = self.retry_delay * attempt
                    print(f"  Attempt {attempt} got {e.code}, retrying in {wait}s...", file=sys.stderr)
                    time.sleep(wait)
                    continue
                print(f"HTTP Error {e.code}: {error_body[:500]}", file=sys.stderr)
                raise
            except Exception as e:
                if attempt < self.max_retries:
                    print(f"  Error: {e}, retrying...", file=sys.stderr)
                    time.sleep(self.retry_delay)
                    continue
                raise

        return ""

    def _extract_text(self, result: dict) -> str:
        if self.resolved_style() == "openai":
            choices = result.get("choices", [])
            if not choices:
                return ""
            content = choices[0].get("message", {}).get("content", "")
            return content if isinstance(content, str) else ""
        candidates = result.get("candidates", [])
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts", [])
        texts = [part["text"] for part in parts if "text" in part]
        return "\n".join(texts)


# ─── Video Utilities ──────────────────────────────────────────────────────────

def get_duration(path: str) -> float:
    """Get video duration in seconds."""
    cmd = ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
           "-of", "csv=p=0", path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def format_time(seconds: float) -> str:
    """Format seconds to MM:SS."""
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"


def format_time_underscore(seconds: float) -> str:
    """Format seconds to MM_SS (for filenames). Supports >99 minutes."""
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}_{s:02d}" if m < 100 else f"{m}_{s:02d}"


def split_video(video_path: str, chunk_minutes: int, work_dir: str):
    """Split video into chunks. Returns list of (start_sec, end_sec, chunk_path)."""
    duration = get_duration(video_path)
    if duration == 0:
        return []

    chunk_seconds = chunk_minutes * 60
    chunks = []
    start = 0.0

    while start < duration:
        end = min(start + chunk_seconds, duration)
        chunk_path = os.path.join(work_dir, f"chunk_{len(chunks):03d}.mp4")
        cmd = ["ffmpeg", "-y", "-ss", str(start), "-i", video_path,
               "-t", str(end - start), "-c", "copy", "-avoid_negative_ts", "1",
               chunk_path]
        subprocess.run(cmd, capture_output=True)
        if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 0:
            chunks.append((start, end, chunk_path))
        start = end

    return chunks


def extract_keyframes(video_path: str, output_dir: str, interval: int = KEYFRAME_INTERVAL_SEC,
                      time_offset: float = 0):
    """Extract keyframes at regular intervals. Returns list of (abs_timestamp, filepath)."""
    duration = get_duration(video_path)
    if duration == 0:
        return []

    os.makedirs(output_dir, exist_ok=True)
    frames = []
    t = 0.0

    while t < duration:
        abs_time = t + time_offset
        filename = f"screenshot_{format_time_underscore(abs_time)}.jpg"
        out_path = os.path.join(output_dir, filename)
        cmd = ["ffmpeg", "-y", "-ss", str(t), "-i", video_path,
               "-frames:v", "1", "-update", "1", "-q:v", "2", out_path]
        subprocess.run(cmd, capture_output=True)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            frames.append((abs_time, out_path))
        t += interval

    return frames


# Pattern to match screenshot_MM_SS.jpg references in Gemini output
_SCREENSHOT_REF_RE = re.compile(r'screenshot_(\d{2,})_(\d{2})\.jpg')


def extract_referenced_keyframes(video_path: str, document: str, output_dir: str):
    """Extract only the keyframes referenced by Gemini in the document.

    Returns list of (timestamp_seconds, saved_filepath).
    """
    matches = _SCREENSHOT_REF_RE.findall(document)
    if not matches:
        return []

    # Deduplicate and convert to seconds
    timestamps = {}
    for mm, ss in matches:
        secs = int(mm) * 60 + int(ss)
        filename = f"screenshot_{mm}_{ss}.jpg"
        timestamps[secs] = filename

    duration = get_duration(video_path)
    os.makedirs(output_dir, exist_ok=True)
    frames = []

    for secs in sorted(timestamps):
        filename = timestamps[secs]
        out_path = os.path.join(output_dir, filename)
        seek_time = min(secs, max(0, duration - 0.5)) if duration > 0 else secs
        cmd = ["ffmpeg", "-y", "-ss", str(seek_time), "-i", video_path,
               "-frames:v", "1", "-update", "1", "-q:v", "2", out_path]
        subprocess.run(cmd, capture_output=True)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            frames.append((secs, out_path))

    return frames


def get_file_size_mb(path: str) -> float:
    return os.path.getsize(path) / (1024 * 1024)


def ensure_supported_container(video_path: str, auto_convert: bool):
    """Ensure the video container is one Gemini accepts.

    Returns (path_to_use, mime_type, is_temp). If the container is unsupported
    (e.g. .mkv / .ts) and auto_convert is on, remux (or re-encode) to mp4 and
    return the temp path with is_temp=True so the caller can clean it up.
    """
    ext = Path(video_path).suffix.lower()
    mime = GEMINI_VIDEO_MIME.get(ext)
    if mime:
        return video_path, mime, False

    if not auto_convert:
        print(f"  Warning: '{ext}' may be unsupported by Gemini and auto_convert is off; "
              f"uploading as-is.", file=sys.stderr)
        return video_path, "video/mp4", False

    out = video_path + ".converted.mp4"
    print(f"  Converting unsupported container '{ext}' -> mp4...", file=sys.stderr)
    remux = subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-c", "copy", "-movflags", "+faststart", out],
        capture_output=True)
    if remux.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        # Remux failed (incompatible codecs for mp4) — re-encode.
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-c:v", "libx264", "-preset", "fast",
             "-c:a", "aac", "-movflags", "+faststart", out],
            capture_output=True)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out, "video/mp4", True
    print("  Conversion failed; uploading original as-is.", file=sys.stderr)
    return video_path, "video/mp4", False


def compress_video(video_path: str, target_mb: float = DEFAULT_INLINE_MAX_MB) -> str:
    """Compress video to fit within inline upload limit (H.264 720p). Returns new path."""
    duration = get_duration(video_path)
    if duration == 0:
        return video_path

    target_bits = target_mb * 1024 * 1024 * 8 * 0.9
    video_bitrate = int(target_bits / duration)
    video_bitrate = max(video_bitrate, 200_000)

    compressed = video_path + ".compressed.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", "scale=-2:720",
        "-c:v", "libx264", "-preset", "fast",
        "-b:v", str(video_bitrate),
        "-c:a", "aac", "-b:a", "64k", "-ac", "1",
        "-movflags", "+faststart",
        compressed,
    ]
    print(f"  Compressing video to ~{target_mb:.0f}MB (bitrate {video_bitrate//1000}kbps)...", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.exists(compressed):
        print(f"  Compression failed: {result.stderr[:300]}", file=sys.stderr)
        return video_path

    new_size = get_file_size_mb(compressed)
    print(f"  Compressed: {get_file_size_mb(video_path):.1f}MB → {new_size:.1f}MB", file=sys.stderr)
    return compressed


# ─── Video Content Building ──────────────────────────────────────────────────

def build_video_parts(client: GeminiClient, video_path: str):
    """Build Gemini content parts for a video file.

    Strategy:
      1. Ensure a Gemini-supported container (convert .mkv/.ts -> mp4 if needed)
      2. Try File API upload (up to 2GB, native video processing)
      3. On failure, compress if over the inline limit, send as inline base64

    Returns: (parts_list, file_name_to_delete_or_None)
    """
    src_path, mime, is_temp = ensure_supported_container(video_path, client.auto_convert)
    try:
        file_info = client.upload_file(src_path, mime)
        if file_info and file_info.get("uri"):
            return [{"fileData": {"mimeType": mime, "fileUri": file_info["uri"]}}], file_info.get("name")

        # Fallback: inline base64, compress if needed
        actual_path = src_path
        actual_mime = mime
        if get_file_size_mb(src_path) > client.inline_max_mb:
            actual_path = compress_video(src_path, client.inline_max_mb)
            actual_mime = "video/mp4"

        print(f"  Using inline base64 ({get_file_size_mb(actual_path):.1f}MB)...", file=sys.stderr)
        with open(actual_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")

        if actual_path != src_path and os.path.exists(actual_path):
            os.remove(actual_path)

        return [{"inlineData": {"mimeType": actual_mime, "data": b64}}], None
    finally:
        if is_temp and os.path.exists(src_path):
            os.remove(src_path)


# ─── Analysis ────────────────────────────────────────────────────────────────

def analyze_single(client: GeminiClient, video_path: str, prompt: str,
                   system_instruction: str = "") -> str:
    """Analyze a single video (or chunk) with Gemini."""
    video_parts, file_name = build_video_parts(client, video_path)

    parts = video_parts + [{"text": prompt}]
    contents = [{"parts": parts}]

    duration = get_duration(video_path)
    print(f"  Sending video ({duration/60:.1f} min) to Gemini...", file=sys.stderr)

    try:
        result = client.generate(contents, system_instruction=system_instruction)
    finally:
        if file_name:
            client.delete_file(file_name)

    return result if result else "ERROR: Empty response from Gemini."


def analyze_video(client: GeminiClient, video_path: str, profile: Profile, cfg: Config,
                  output_dir: str) -> str:
    """Full analysis pipeline: single or chunked with sequential accumulation."""
    duration = get_duration(video_path)
    duration_min = duration / 60
    chunk_minutes = cfg.chunk_minutes
    continuation_tpl = profile.continuation or DEFAULT_CONTINUATION

    if duration_min <= chunk_minutes:
        # ── Single-pass analysis ──
        print(f"Single-pass analysis ({duration_min:.1f} min)...", file=sys.stderr)
        result = analyze_single(client, video_path, profile.prompt, profile.system)

        print("Extracting referenced keyframes...", file=sys.stderr)
        frames = extract_referenced_keyframes(video_path, result, output_dir)
        print(f"  Saved {len(frames)} keyframes to {output_dir}", file=sys.stderr)

        return post_process_document(result, output_dir)

    # ── Chunked analysis with sequential accumulation ──
    print(f"Chunked analysis ({duration_min:.1f} min -> {chunk_minutes} min chunks)...", file=sys.stderr)

    with tempfile.TemporaryDirectory() as work_dir:
        chunks = split_video(video_path, chunk_minutes, work_dir)
        if not chunks:
            raise RuntimeError("Could not split video into chunks.")

        total_chunks = len(chunks)
        print(f"  Split into {total_chunks} chunks", file=sys.stderr)

        if cfg.parallel_chunks and total_chunks > 1:
            accumulated_doc, failed_count = _analyze_chunks_parallel(
                client, chunks, profile, cfg)
            if failed_count > 0:
                print(f"  ⚠️  {failed_count}/{total_chunks} chunks failed. "
                      f"Use --resume to retry.", file=sys.stderr)
            print("Extracting referenced keyframes...", file=sys.stderr)
            frames = extract_referenced_keyframes(video_path, accumulated_doc, output_dir)
            print(f"  Saved {len(frames)} keyframes to {output_dir}", file=sys.stderr)
            return post_process_document(accumulated_doc, output_dir)

        accumulated_doc = ""
        failed_count = 0

        for i, (start, end, chunk_path) in enumerate(chunks):
            chunk_num = i + 1
            start_str = format_time(start)
            end_str = format_time(end)
            print(f"\n  Chunk {chunk_num}/{total_chunks} ({start_str} - {end_str})...", file=sys.stderr)

            if i == 0:
                chunk_prompt = profile.prompt
            else:
                chunk_prompt = continuation_tpl.format(
                    chunk_index=chunk_num,
                    total_chunks=total_chunks,
                    start_time=start_str,
                    end_time=end_str,
                    previous_doc=accumulated_doc,
                )

            try:
                chunk_result = analyze_single(client, chunk_path, chunk_prompt, profile.system)
                if i == 0:
                    accumulated_doc = chunk_result
                else:
                    accumulated_doc = accumulated_doc.rstrip() + "\n\n" + chunk_result
            except Exception as e:
                print(f"  Chunk {chunk_num} failed: {e}", file=sys.stderr)
                accumulated_doc += (
                    f"\n\n<!-- CHUNK_FAILED {chunk_num} {start_str} {end_str} -->\n"
                    f"> ⚠️ **Chunk {chunk_num} ({start_str} - {end_str}) 处理失败：** {e}\n"
                )
                failed_count += 1

        if failed_count > 0:
            print(f"  ⚠️  {failed_count}/{total_chunks} chunks failed. "
                  f"Use --resume to retry.", file=sys.stderr)

        print("Extracting referenced keyframes...", file=sys.stderr)
        frames = extract_referenced_keyframes(video_path, accumulated_doc, output_dir)
        print(f"  Saved {len(frames)} keyframes to {output_dir}", file=sys.stderr)

        return post_process_document(accumulated_doc, output_dir)


def _analyze_chunks_parallel(client: GeminiClient, chunks: list, profile: Profile,
                             cfg: Config):
    """Analyze all chunks concurrently, each with an independent prompt.

    Trades cross-chunk coherence (numbering continuity, dedup of recap sections)
    for wall-clock speed — chunk N never sees chunk N-1's output. Failed chunks
    get the same CHUNK_FAILED marker, so --resume works on the result.
    Returns (merged_doc, failed_count).
    """
    total_chunks = len(chunks)
    print(f"  Parallel mode: {total_chunks} chunks x {cfg.workers} workers "
          f"(independent prompts, no cross-chunk context)", file=sys.stderr)

    results = [None] * total_chunks

    def run_chunk(i: int, start: float, end: float, chunk_path: str) -> str:
        start_str, end_str = format_time(start), format_time(end)
        chunk_prompt = profile.prompt + PARALLEL_CHUNK_NOTE.format(
            chunk_index=i + 1, total_chunks=total_chunks,
            start_time=start_str, end_time=end_str)
        print(f"  Chunk {i + 1}/{total_chunks} ({start_str} - {end_str}) started...",
              file=sys.stderr)
        return analyze_single(client, chunk_path, chunk_prompt, profile.system)

    failed_count = 0
    with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as executor:
        futures = {executor.submit(run_chunk, i, s, e, p): i
                   for i, (s, e, p) in enumerate(chunks)}
        for future in as_completed(futures):
            i = futures[future]
            start_str, end_str = format_time(chunks[i][0]), format_time(chunks[i][1])
            try:
                results[i] = future.result()
                print(f"  ✅ Chunk {i + 1}/{total_chunks} done", file=sys.stderr)
            except Exception as e:
                print(f"  ❌ Chunk {i + 1} failed: {e}", file=sys.stderr)
                results[i] = (
                    f"<!-- CHUNK_FAILED {i + 1} {start_str} {end_str} -->\n"
                    f"> ⚠️ **Chunk {i + 1} ({start_str} - {end_str}) 处理失败：** {e}\n")
                failed_count += 1

    return "\n\n".join(r.rstrip() for r in results), failed_count


# ─── Resume Support ──────────────────────────────────────────────────────────

RESUME_FAIL_PATTERN = re.compile(r'<!-- CHUNK_FAILED (\d+) (\d{2,}:\d{2}) (\d{2,}:\d{2}) -->')
ANALYSIS_META_PATTERN = re.compile(r'<!-- ANALYSIS_META (.+?) -->')


def _parse_time_to_seconds(time_str: str) -> float:
    """Parse MM:SS to seconds."""
    parts = time_str.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def build_analysis_footer(video_path: str, model: str, profile_name: str,
                          chunk_minutes: int, keyframe_interval: int) -> str:
    """Build a metadata footer to append at the end of the document."""
    abs_path = os.path.abspath(video_path)
    meta = json.dumps({
        "video": abs_path, "model": model, "profile": profile_name,
        "chunk_minutes": chunk_minutes, "keyframe_interval": keyframe_interval,
    }, ensure_ascii=False)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"\n\n---\n"
        f"> 📋 **分析信息**\n"
        f"> - 视频文件：`{abs_path}`\n"
        f"> - 模型：`{model}`\n"
        f"> - 分析方法：`{profile_name}`\n"
        f"> - 分段时长：{chunk_minutes} 分钟\n"
        f"> - 截图模式：按需提取（Gemini 标注的关键帧）\n"
        f"> - 分析时间：{ts}\n\n"
        f"<!-- ANALYSIS_META {meta} -->\n"
    )


def resume_failed_chunks(output_file: str, cfg: Config, model_override: str = ""):
    """Resume: parse the existing MD for failed chunk markers and retry them.

    State (video path, model, profile, params) is read from the ANALYSIS_META
    comment in the MD footer. No external state file needed.
    """
    if not os.path.exists(output_file):
        print(f"ERROR: File not found: {output_file}", file=sys.stderr)
        sys.exit(1)

    doc = Path(output_file).read_text(encoding="utf-8")

    meta_match = ANALYSIS_META_PATTERN.search(doc)
    if not meta_match:
        print("ERROR: No ANALYSIS_META found in document. Cannot resume.", file=sys.stderr)
        sys.exit(1)

    meta = json.loads(meta_match.group(1))
    video_path = meta["video"]
    model = model_override or meta.get("model", cfg.model)
    profile_name = meta.get("profile", cfg.profile)
    chunk_minutes = meta.get("chunk_minutes", cfg.chunk_minutes)

    if not os.path.exists(video_path):
        print(f"ERROR: Original video not found: {video_path}", file=sys.stderr)
        sys.exit(1)

    profile = load_profile(profile_name, resolve_prompts_dir(cfg)).with_evidence(cfg.require_evidence)
    continuation_tpl = profile.continuation or DEFAULT_CONTINUATION

    failed_matches = list(RESUME_FAIL_PATTERN.finditer(doc))
    if not failed_matches:
        print("No failed chunks found. Nothing to resume.", file=sys.stderr)
        return

    duration = get_duration(video_path)
    chunk_secs = chunk_minutes * 60
    total_chunks = int(duration // chunk_secs) + (1 if duration % chunk_secs > 0 else 0)

    keyframe_dir = str(Path(output_file).with_suffix("")) + "_frames"

    print(f"Resuming: {len(failed_matches)} failed chunk(s)", file=sys.stderr)
    print(f"Video: {video_path} | Model: {model} | Profile: {profile_name}", file=sys.stderr)

    client = make_client(cfg, model)
    still_failed = 0

    with tempfile.TemporaryDirectory() as work_dir:
        for match in failed_matches:
            idx = int(match.group(1))
            start_str = match.group(2)
            end_str = match.group(3)
            start_sec = _parse_time_to_seconds(start_str)
            end_sec = _parse_time_to_seconds(end_str)

            print(f"\n  Retrying chunk {idx}/{total_chunks} ({start_str} - {end_str})...", file=sys.stderr)

            chunk_path = os.path.join(work_dir, f"retry_{idx:03d}.mp4")
            cmd = ["ffmpeg", "-y", "-ss", str(start_sec), "-i", video_path,
                   "-t", str(end_sec - start_sec), "-c", "copy",
                   "-avoid_negative_ts", "1", chunk_path]
            subprocess.run(cmd, capture_output=True)

            if not os.path.exists(chunk_path) or os.path.getsize(chunk_path) == 0:
                print(f"  Could not extract chunk {idx}", file=sys.stderr)
                still_failed += 1
                continue

            if idx == 1:
                chunk_prompt = profile.prompt
            else:
                context_doc = doc[:match.start()].strip()
                context_doc = re.sub(
                    r'<!-- CHUNK_FAILED \d+ \S+ \S+ -->\n'
                    r'> ⚠️ \*\*Chunk \d+.*处理失败：\*\*[^\n]*\n?',
                    '', context_doc).strip()
                context_doc = RESUME_FAIL_PATTERN.sub('', context_doc).strip()
                footer_pos = context_doc.find('\n---\n> 📋')
                if footer_pos >= 0:
                    context_doc = context_doc[:footer_pos].strip()

                chunk_prompt = continuation_tpl.format(
                    chunk_index=idx,
                    total_chunks=total_chunks,
                    start_time=start_str,
                    end_time=end_str,
                    previous_doc=context_doc,
                )

            try:
                chunk_result = analyze_single(client, chunk_path, chunk_prompt, profile.system)
                fail_block = re.compile(
                    re.escape(f"<!-- CHUNK_FAILED {idx} {start_str} {end_str} -->") +
                    r'\n> ⚠️ \*\*Chunk \d+.*处理失败：\*\*[^\n]*\n?'
                )
                doc, n = fail_block.subn(chunk_result, doc, count=1)
                if n == 0:
                    doc = doc.replace(
                        f"<!-- CHUNK_FAILED {idx} {start_str} {end_str} -->",
                        chunk_result, 1)
                print(f"  ✅ Chunk {idx} recovered", file=sys.stderr)
            except Exception as e:
                print(f"  ❌ Chunk {idx} still failed: {e}", file=sys.stderr)
                still_failed += 1

    print("Extracting referenced keyframes...", file=sys.stderr)
    frames = extract_referenced_keyframes(video_path, doc, keyframe_dir)
    print(f"  Saved {len(frames)} keyframes to {keyframe_dir}", file=sys.stderr)

    doc = post_process_document(doc, keyframe_dir)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(doc)

    if still_failed:
        print(f"\n⚠️  {still_failed} chunk(s) still failed. Use --resume again.", file=sys.stderr)
    else:
        print("\n✅ All chunks recovered.", file=sys.stderr)

    print(f"📄 Updated: {output_file}", file=sys.stderr)


def post_process_document(doc: str, output_dir: str) -> str:
    """Post-process the generated document:
    - Strip leading --- lines that break YAML frontmatter parsing
    - Replace [screenshot_MM_SS.jpg] references with relative Markdown image links
    - Handle backtick-wrapped and model-expanded ![...](...) refs
    - Verify referenced screenshots exist (nearest-match fallback)
    """
    doc = re.sub(r'^\s*---\s*\n', '', doc)

    heading_match = re.search(r'^#\s', doc, re.MULTILINE)
    if heading_match and heading_match.start() > 0:
        doc = doc[heading_match.start():]

    output_dir_name = Path(output_dir).name

    available_frames = {}  # seconds -> filename
    if os.path.isdir(output_dir):
        for f in os.listdir(output_dir):
            m = re.match(r'screenshot_(\d{2,})_(\d{2})\.jpg', f)
            if m:
                secs = int(m.group(1)) * 60 + int(m.group(2))
                available_frames[secs] = f

    def find_nearest_frame(filename: str) -> str:
        m = re.match(r'screenshot_(\d{2,})_(\d{2})\.jpg', filename)
        if not m or not available_frames:
            return filename
        target_secs = int(m.group(1)) * 60 + int(m.group(2))
        nearest = min(available_frames.keys(), key=lambda s: abs(s - target_secs))
        return available_frames[nearest]

    def make_image_link(filename: str) -> str:
        filepath = os.path.join(output_dir, filename)
        if os.path.exists(filepath):
            return f"![{filename}]({output_dir_name}/{filename})"
        nearest = find_nearest_frame(filename)
        if nearest != filename:
            return f"![{nearest}]({output_dir_name}/{nearest})"
        return f"![{filename}]({output_dir_name}/{filename})<!-- file not found -->"

    doc = re.sub(
        r'`?!\[(screenshot_\d{2,}_\d{2}\.jpg)\]\([^)]*\)(?:<!--[^>]*-->)?`?',
        lambda m: make_image_link(m.group(1)),
        doc
    )

    doc = re.sub(
        r'`?\[?(screenshot_\d{2,}_\d{2}\.jpg)\]?`?',
        lambda m: make_image_link(m.group(1)),
        doc
    )

    return doc


# ─── Client factory ───────────────────────────────────────────────────────────

def make_client(cfg: Config, model: str = "") -> GeminiClient:
    return GeminiClient(
        cfg.api_key, cfg.base_url, model or cfg.model,
        auth=cfg.auth, max_retries=cfg.max_retries, retry_delay=cfg.retry_delay_seconds,
        timeout=cfg.request_timeout_seconds, max_output_tokens=cfg.max_output_tokens,
        temperature=cfg.temperature, inline_max_mb=cfg.inline_max_mb,
        auto_convert=cfg.auto_convert, api_style=cfg.api_style,
    )


# ─── Main ─────────────────────────────────────────────────────────────────────

def process_one(input_val: str, output_file: str, keyframe_dir: str,
                cfg: Config, profile: Profile):
    """Process a single video. Returns (input_val, success, message, elapsed_sec)."""
    start = time.time()
    try:
        if not os.path.exists(input_val):
            return (input_val, False, f"File not found: {input_val}", 0.0)

        client = make_client(cfg)
        duration = get_duration(input_val)
        size_mb = get_file_size_mb(input_val)
        print(f"[{Path(input_val).name}] {duration/60:.1f} min | {size_mb:.1f} MB", file=sys.stderr)

        os.makedirs(keyframe_dir, exist_ok=True)
        result = analyze_video(client, input_val, profile, cfg, keyframe_dir)

        footer = build_analysis_footer(input_val, cfg.model, profile.name,
                                       cfg.chunk_minutes, cfg.keyframe_interval)
        parent = Path(output_file).parent
        if str(parent) != ".":
            os.makedirs(parent, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(result + footer)
        return (input_val, True, output_file, time.time() - start)
    except Exception as e:
        return (input_val, False, str(e), time.time() - start)


def is_output_complete(md_path: str) -> bool:
    """A batch output counts as complete when the MD exists with its META footer
    and contains no failed-chunk markers (so re-runs retry partial results)."""
    try:
        doc = Path(md_path).read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(ANALYSIS_META_PATTERN.search(doc)) and not RESUME_FAIL_PATTERN.search(doc)


def write_batch_report(output_dir: str, rows: list, cfg: Config, profile_name: str):
    """Write _batch_report.md summarizing every video's outcome."""
    ok = sum(1 for r in rows if r[2] == "ok")
    fail = sum(1 for r in rows if r[2] == "failed")
    skipped = sum(1 for r in rows if r[2] == "skipped")
    icon = {"ok": "✅", "failed": "❌", "skipped": "⏭️"}
    lines = [
        "# 批量分析报告\n",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 模型：`{cfg.model}` | 方法：`{profile_name}` | 并发：{cfg.workers}",
        f"- 结果：✅ {ok} 成功 / ❌ {fail} 失败 / ⏭️ {skipped} 跳过（共 {len(rows)}）",
        "",
        "| 状态 | 视频 | 产物 / 原因 | 耗时 |",
        "|------|------|------------|------|",
    ]
    for name, input_val, status, elapsed, detail in sorted(rows):
        if status == "ok":
            detail_cell = f"[{name}.md]({name}/{name}.md)"
        elif status == "skipped":
            detail_cell = "已完成，跳过"
        else:
            detail_cell = str(detail).replace("|", "\\|").replace("\n", " ")[:200]
        t = f"{elapsed:.0f}s" if status == "ok" else "-"
        lines.append(f"| {icon[status]} | `{Path(input_val).name}` | {detail_cell} | {t} |")
    if fail:
        lines += ["", "> ❌ 有失败项：重跑同一条命令即可只补失败的（已完成的会自动跳过）。"]
    report = os.path.join(output_dir, "_batch_report.md")
    Path(report).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def resolve_batch_inputs(batch_path: str):
    """Resolve a directory or text file list into a list of local video paths."""
    p = Path(batch_path)
    if p.is_dir():
        videos = []
        for ext in ("*.mp4", "*.mov", "*.avi", "*.mkv", "*.webm"):
            videos.extend(glob.glob(str(p / ext)))
        return sorted(videos)
    elif p.is_file():
        lines = p.read_text(encoding="utf-8").splitlines()
        return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    else:
        print(f"ERROR: --batch path not found: {batch_path}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Gemini Video Tutor v2 — Convert video to a structured document",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s video.mp4 -o tutorial.md
  %(prog)s video.mp4 --profile summary -o summary.md
  %(prog)s --batch ./videos/ --output-dir ./output/
  %(prog)s --batch list.txt --output-dir ./output/ --workers 3
  %(prog)s --resume tutorial.md
""")
    parser.add_argument("input", nargs="?", help="Local video file path")
    parser.add_argument("-o", "--output", help="Output Markdown file path")
    parser.add_argument("--resume", metavar="MD_FILE",
                        help="Resume mode: retry failed chunks for an existing output file")
    parser.add_argument("--batch", metavar="DIR_OR_FILE",
                        help="Batch mode: directory of videos or text file with one path per line")
    parser.add_argument("--output-dir", default=".",
                        help="Output directory for batch mode (default: current dir)")
    parser.add_argument("--force", action="store_true",
                        help="Batch mode: re-analyze videos whose output already exists "
                             "(default skips completed ones, making re-runs resumable)")
    parser.add_argument("--workers", type=int, default=None,
                        help="Parallel workers in batch mode (default: config batch.workers)")
    parser.add_argument("-m", "--model", default=None,
                        help="Model name (overrides env/config)")
    parser.add_argument("--profile", default=None,
                        help="Prompt profile name in prompts/ (overrides config analysis.profile)")
    parser.add_argument("--config", default=None,
                        help="Path to config.yaml (overrides GEMINI_TUTOR_CONFIG / default)")
    parser.add_argument("--auth", default=None, choices=["auto", "bearer", "api-key"],
                        help="Auth style (default: auto — api-key for official, bearer for proxies)")
    parser.add_argument("--api-style", default=None, choices=["auto", "gemini", "openai"],
                        help="Wire protocol: gemini native REST or OpenAI-compatible chat "
                             "completions (default: auto — openai for openrouter.ai)")
    parser.add_argument("--chunk-minutes", type=int, default=None,
                        help="Max minutes per chunk (overrides config)")
    parser.add_argument("--no-chunk", action="store_true",
                        help="Disable auto-chunking (send full video regardless of length)")
    parser.add_argument("--parallel-chunks", action="store_true", default=None,
                        help="Analyze chunks of one long video concurrently (faster, "
                             "but chunks lose cross-chunk context; numbering restarts per chunk)")
    parser.add_argument("--keyframe-dir", default=None,
                        help="Directory to save keyframe screenshots (default: <output>_frames/)")
    parser.add_argument("--keyframe-interval", type=int, default=None,
                        help="Seconds between keyframe captures (overrides config)")
    parser.add_argument("--prompt", default="",
                        help="Raw analysis prompt that replaces the profile's main prompt")
    parser.add_argument("--api-key", default=None, help="GEMINI_API_KEY (or set env/config)")
    parser.add_argument("--base-url", default=None, help="GEMINI_BASE_URL (or set env/config)")

    args = parser.parse_args()

    # ── Resolve config (CLI > env > config.yaml > defaults) ──
    cfg = load_config(args, args.config)
    if args.no_chunk:
        cfg.chunk_minutes = 10 ** 9

    if not cfg.api_key:
        print("ERROR: Gemini API key not set. Use --api-key, env GEMINI_API_KEY, "
              "or config.yaml. Run scripts/setup.py to check setup.", file=sys.stderr)
        sys.exit(1)
    if not cfg.base_url:
        print("ERROR: Gemini base URL not set. Use --base-url, env GEMINI_BASE_URL, "
              "or config.yaml.", file=sys.stderr)
        sys.exit(1)

    print(f"Model: {cfg.model}", file=sys.stderr)
    print(f"Base:  {cfg.base_url}", file=sys.stderr)

    # ── Resume mode (config still needed for profile/auth/params) ──
    if args.resume:
        resume_failed_chunks(args.resume, cfg, args.model or "")
        return

    # ── Load prompt profile ──
    try:
        profile = load_profile(cfg.profile, resolve_prompts_dir(cfg)).with_evidence(cfg.require_evidence)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    if args.prompt:
        profile.prompt = args.prompt  # raw override of the main prompt
    print(f"Profile: {profile.name}", file=sys.stderr)

    # ── Batch mode ──
    if args.batch:
        inputs = resolve_batch_inputs(args.batch)
        if not inputs:
            print("ERROR: No video inputs found.", file=sys.stderr)
            sys.exit(1)

        output_dir = args.output_dir
        os.makedirs(output_dir, exist_ok=True)
        skip = not args.force
        print(f"Batch: {len(inputs)} videos | workers={cfg.workers} | output={output_dir}"
              f"{' | skip-existing' if skip else ' | force'}", file=sys.stderr)

        # One subdirectory per video: <output_dir>/<stem>/{<stem>.md, <stem>_frames/}.
        # Duplicate stems get _2, _3, ... so nothing is silently overwritten.
        used_names = set()

        def assign_output(input_val):
            stem = Path(input_val).stem
            name, n = stem, 1
            while name in used_names:
                n += 1
                name = f"{stem}_{n}"
            used_names.add(name)
            vdir = os.path.join(output_dir, name)
            return (name, os.path.join(vdir, f"{name}.md"),
                    os.path.join(vdir, f"{name}_frames"))

        rows = []  # (name, input_val, status, elapsed_sec, detail)
        futures_map = {}
        with ThreadPoolExecutor(max_workers=cfg.workers) as executor:
            for input_val in inputs:
                name, out_md, out_frames = assign_output(input_val)
                if skip and is_output_complete(out_md):
                    rows.append((name, input_val, "skipped", 0.0, out_md))
                    print(f"  ⏭️  {name}: 已完成，跳过（--force 可强制重跑）", file=sys.stderr)
                    continue
                future = executor.submit(process_one, input_val, out_md, out_frames, cfg, profile)
                futures_map[future] = (name, input_val)

            for future in as_completed(futures_map):
                name, input_val = futures_map[future]
                _, success, msg, elapsed = future.result()
                rows.append((name, input_val, "ok" if success else "failed", elapsed, msg))
                symbol = "✅" if success else "❌"
                print(f"  {symbol} {name} ({elapsed:.0f}s): {msg}", file=sys.stderr)

        ok = sum(1 for r in rows if r[2] == "ok")
        fail = sum(1 for r in rows if r[2] == "failed")
        skipped = sum(1 for r in rows if r[2] == "skipped")
        report = write_batch_report(output_dir, rows, cfg, profile.name)
        print(f"\nBatch complete: {ok} succeeded, {fail} failed, {skipped} skipped.", file=sys.stderr)
        print(f"📋 Report: {report}", file=sys.stderr)
        if fail:
            print("   重跑同一条命令即可只补失败项（已完成的自动跳过）。", file=sys.stderr)
        sys.exit(0 if fail == 0 else 1)

    # ── Single mode ──
    if not args.input:
        parser.print_help()
        sys.exit(1)

    video_path = args.input.strip()
    if not os.path.exists(video_path):
        print(f"ERROR: File not found: {video_path}", file=sys.stderr)
        sys.exit(1)

    client = make_client(cfg)

    output_file = args.output
    if args.keyframe_dir:
        keyframe_dir = args.keyframe_dir
    elif output_file:
        keyframe_dir = str(Path(output_file).with_suffix("")) + "_frames"
    else:
        keyframe_dir = os.path.join(tempfile.gettempdir(), "gemini-tutor-frames")

    os.makedirs(keyframe_dir, exist_ok=True)

    duration = get_duration(video_path)
    size_mb = get_file_size_mb(video_path)
    print(f"Video: {duration/60:.1f} min | {size_mb:.1f} MB | {video_path}", file=sys.stderr)

    result = analyze_video(client, video_path, profile, cfg, keyframe_dir)
    footer = build_analysis_footer(video_path, cfg.model, profile.name,
                                   cfg.chunk_minutes, cfg.keyframe_interval)

    if output_file:
        parent = Path(output_file).parent
        if str(parent) != ".":
            os.makedirs(parent, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(result + footer)
        print(f"\n✅ Saved to: {output_file}", file=sys.stderr)
        print(f"📸 Keyframes saved to: {keyframe_dir}/", file=sys.stderr)
    else:
        print("\n" + result + footer)


if __name__ == "__main__":
    main()
