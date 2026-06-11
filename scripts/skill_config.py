#!/usr/bin/env python3
"""Configuration and prompt-profile loading for Gemini Video Tutor.

Design goals (portability across Claude Code / Codex / openclaw and other
generic agents):

  * Zero hard dependency. PyYAML is used when available (richer/robuster),
    otherwise a tiny scalar-only fallback parser handles the config.yaml
    subset we document in example.config.yaml. Either way no `pip install`
    is required to run the skill.

  * Prompts never live inside config.yaml — they live in prompts/<name>.md
    as raw text. So config.yaml only ever holds simple scalars (keys, URLs,
    numbers, booleans), which keeps the fallback parser safe even though the
    prompts themselves are full of special characters.

  * Resolution priority:  CLI args  >  environment variables  >  config.yaml
    >  built-in defaults.

Python 3.8+ (uses `from __future__ import annotations` so the modern type
hints in this package parse on older interpreters too).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Skill root = parent of this scripts/ directory.
SKILL_DIR = Path(__file__).resolve().parent.parent

# Built-in defaults — mirror example.config.yaml.
DEFAULTS = {
    "base_url": "https://generativelanguage.googleapis.com",
    "model": "gemini-2.5-flash",
    "auth": "auto",                      # auto | bearer | api-key
    "max_retries": 3,
    "retry_delay_seconds": 15,
    "request_timeout_seconds": 600,
    "max_output_tokens": 65536,
    "temperature": 0.4,
    "profile": "tutorial",
    "prompts_dir": "",                   # "" -> SKILL_DIR/prompts
    "require_evidence": True,
    "chunk_minutes": 40,
    "keyframe_interval": 5,
    "auto_convert": True,
    "inline_max_mb": 6,
    "workers": 2,
}

# Field -> python type for casting env / fallback-parsed string values.
_TYPES = {
    "max_retries": int,
    "retry_delay_seconds": int,
    "request_timeout_seconds": int,
    "max_output_tokens": int,
    "temperature": float,
    "require_evidence": bool,
    "chunk_minutes": int,
    "keyframe_interval": int,
    "auto_convert": bool,
    "inline_max_mb": float,
    "workers": int,
}


# ─── YAML loading (hybrid) ──────────────────────────────────────────────────

def _coerce_scalar(v: str):
    """Coerce a bare (already comment/quote-stripped) string to bool/int/float."""
    low = v.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    return v


def _strip_value(raw: str):
    """Strip surrounding quotes and trailing inline comments from a YAML value."""
    v = raw.strip()
    if not v:
        return ""
    if v[0] in ("'", '"'):
        q = v[0]
        end = v.find(q, 1)
        return v[1:end] if end != -1 else v[1:]
    # Bare value: an inline comment must be preceded by whitespace.
    hash_pos = v.find(" #")
    if hash_pos != -1:
        v = v[:hash_pos].strip()
    if v.startswith("#"):
        return ""
    return _coerce_scalar(v)


def _parse_simple_yaml(text: str) -> dict:
    """Fallback parser for the two-level-scalar YAML subset we document.

    Handles: comments, blank lines, `section:` headers, and `  key: value`
    pairs nested one level under a section. Anything fancier (anchors, lists,
    multiline blocks) is intentionally unsupported — config.yaml never needs it.
    """
    data: dict = {}
    section = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[0] not in (" ", "\t"):
            key = raw.split(":", 1)[0].strip()
            data[key] = {}
            section = key
        else:
            if ":" not in raw:
                continue
            k, v = raw.split(":", 1)
            value = _strip_value(v)
            if section is None:
                data[k.strip()] = value
            else:
                data[section][k.strip()] = value
    return data


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        loaded = yaml.safe_load(text)
        return loaded if isinstance(loaded, dict) else {}
    except ImportError:
        return _parse_simple_yaml(text)
    except Exception as e:  # malformed YAML — degrade gracefully, warn once.
        print(f"[skill_config] Warning: failed to parse {path} via PyYAML "
              f"({e}); falling back to minimal parser.", file=sys.stderr)
        return _parse_simple_yaml(text)


def _cast(field: str, value):
    """Cast a string-ish value to the field's declared type."""
    t = _TYPES.get(field)
    if t is None or value is None or isinstance(value, t):
        return value
    s = str(value).strip()
    try:
        if t is bool:
            return s.lower() in ("true", "yes", "on", "1")
        return t(s)
    except (ValueError, TypeError):
        return DEFAULTS.get(field, value)


def _present(v) -> bool:
    """Empty strings / None count as 'not provided' so they don't shadow lower
    priority sources (e.g. api_key: "" in config.yaml must not blank out env)."""
    return v is not None and v != ""


# ─── Config object ──────────────────────────────────────────────────────────

class Config:
    """Resolved configuration. Attribute access for every documented key."""

    def __init__(self, values: dict):
        for k, v in values.items():
            setattr(self, k, v)

    def __repr__(self):
        safe = {k: ("***" if k == "api_key" and v else v)
                for k, v in self.__dict__.items()}
        return f"Config({safe})"


def load_config(cli=None, config_path: str | None = None) -> Config:
    """Resolve configuration from CLI args, env vars, config.yaml, defaults.

    `cli` is an argparse.Namespace (or None). Recognised CLI attributes:
      api_key, base_url, model, auth, profile, chunk_minutes,
      keyframe_interval, workers  (any may be None/absent).
    """
    cli = cli or _Empty()

    path = (config_path
            or os.environ.get("GEMINI_TUTOR_CONFIG")
            or str(SKILL_DIR / "config.yaml"))
    yml = _load_yaml(Path(path))
    g = yml.get("gemini", {}) if isinstance(yml.get("gemini"), dict) else {}
    a = yml.get("analysis", {}) if isinstance(yml.get("analysis"), dict) else {}
    v = yml.get("video", {}) if isinstance(yml.get("video"), dict) else {}
    b = yml.get("batch", {}) if isinstance(yml.get("batch"), dict) else {}

    def resolve(field, cli_attr, env_name, yaml_val):
        cli_val = getattr(cli, cli_attr, None) if cli_attr else None
        env_val = os.environ.get(env_name) if env_name else None
        for candidate in (cli_val, env_val, yaml_val):
            if _present(candidate):
                return _cast(field, candidate)
        return DEFAULTS.get(field)

    values = {
        # ── gemini ──
        "api_key":   resolve("api_key", "api_key", "GEMINI_API_KEY", g.get("api_key")),
        "base_url":  resolve("base_url", "base_url", "GEMINI_BASE_URL", g.get("base_url")),
        "model":     resolve("model", "model", "GEMINI_MODEL", g.get("model")),
        "auth":      resolve("auth", "auth", "GEMINI_AUTH", g.get("auth")),
        "max_retries":            resolve("max_retries", None, "GEMINI_MAX_RETRIES", g.get("max_retries")),
        "retry_delay_seconds":    resolve("retry_delay_seconds", None, None, g.get("retry_delay_seconds")),
        "request_timeout_seconds": resolve("request_timeout_seconds", None, None, g.get("request_timeout_seconds")),
        "max_output_tokens":      resolve("max_output_tokens", None, None, g.get("max_output_tokens")),
        "temperature":            resolve("temperature", None, None, g.get("temperature")),
        # ── analysis ──
        "profile":          resolve("profile", "profile", "GEMINI_TUTOR_PROFILE", a.get("profile")),
        "prompts_dir":      resolve("prompts_dir", None, "GEMINI_TUTOR_PROMPTS_DIR", a.get("prompts_dir")),
        "require_evidence": resolve("require_evidence", None, None, a.get("require_evidence")),
        "chunk_minutes":    resolve("chunk_minutes", "chunk_minutes", None, a.get("chunk_minutes")),
        "keyframe_interval": resolve("keyframe_interval", "keyframe_interval", None, a.get("keyframe_interval")),
        # ── video ──
        "auto_convert":  resolve("auto_convert", None, None, v.get("auto_convert")),
        "inline_max_mb": resolve("inline_max_mb", None, None, v.get("inline_max_mb")),
        # ── batch ──
        "workers": resolve("workers", "workers", None, b.get("workers")),
    }
    values["config_path"] = path
    return Config(values)


class _Empty:
    def __getattr__(self, _):
        return None


def resolve_prompts_dir(cfg: Config) -> Path:
    return Path(cfg.prompts_dir) if getattr(cfg, "prompts_dir", "") else SKILL_DIR / "prompts"


# ─── Prompt profiles ────────────────────────────────────────────────────────

# Matches a section delimiter line:  "# @system" / "## @prompt" / "### @continuation"
_SECTION_RE = re.compile(r'^\s{0,3}#{1,3}\s*@(system|prompt|continuation)\s*$',
                         re.IGNORECASE)

# Appended to a profile's prompt when require_evidence is on AND the profile
# does not already specify screenshot/timestamp rules.
EVIDENCE_RULE = """

# 📌 证据要求（强制）
- 每一条结论 / 步骤都必须标注来源时间戳，格式 [MM:SS]，对应原始视频的绝对时间。
- 关键画面必须标注 [screenshot_MM_SS.jpg]（这些时间点会被自动提取为截图并嵌入文档）。
- 不要编造时间戳；无法在视频中定位时间的内容不要输出。"""


class Profile:
    def __init__(self, name: str, system: str, prompt: str,
                 continuation: str | None = None, description: str = ""):
        self.name = name
        self.system = system
        self.prompt = prompt
        self.continuation = continuation or None
        self.description = description

    def with_evidence(self, require: bool) -> "Profile":
        if require and "screenshot_" not in self.prompt:
            self.prompt = self.prompt.rstrip() + EVIDENCE_RULE
        return self


def available_profiles(prompts_dir: Path) -> list[str]:
    if not prompts_dir.is_dir():
        return []
    return sorted(p.stem for p in prompts_dir.glob("*.md"))


def load_profile(name: str, prompts_dir: Path) -> Profile:
    """Load prompts/<name>.md, parsed into @system / @prompt / @continuation."""
    path = prompts_dir / f"{name}.md"
    if not path.exists():
        avail = available_profiles(prompts_dir)
        raise FileNotFoundError(
            f"提示词档案 '{name}' 不存在: {path}\n"
            f"可用档案: {', '.join(avail) if avail else '(无)'}\n"
            f"新建 {prompts_dir}/{name}.md 即可扩展自定义分析方法。")

    text = path.read_text(encoding="utf-8")
    description = ""
    fm = re.match(r'^\s*---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
    if fm:
        dm = re.search(r'^\s*description\s*:\s*(.+)$', fm.group(1), re.MULTILINE)
        if dm:
            description = dm.group(1).strip().strip('"\'')

    sections = {"system": [], "prompt": [], "continuation": []}
    cur = None
    for line in text.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            cur = m.group(1).lower()
            continue
        if cur:
            sections[cur].append(line)

    system = "\n".join(sections["system"]).strip()
    prompt = "\n".join(sections["prompt"]).strip()
    continuation = "\n".join(sections["continuation"]).strip()

    if not prompt:
        raise ValueError(
            f"提示词档案 {path} 缺少 '@prompt' 段，无法使用。"
            f"档案格式见 prompts/tutorial.md。")

    return Profile(name, system, prompt, continuation or None, description)
