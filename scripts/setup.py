#!/usr/bin/env python3
"""
Gemini Video Tutor — initialization / doctor.

Run this first (or whenever something breaks). It checks every prerequisite,
tells you exactly what to fix, and can bootstrap the local config and the
companion video-downloader skill.

  python3 scripts/setup.py                 # check everything, print a report
  python3 scripts/setup.py --init-config   # create config.yaml from the template
  python3 scripts/setup.py --install-downloader   # git clone the video-downloader skill

Exit code 0 = ready to analyze local videos; non-zero = blocking issue found.
(A missing video-downloader is only a warning — it is just needed for URL inputs.)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_config
from skill_config import (SKILL_DIR, load_config, resolve_prompts_dir,
                          available_profiles)

DOWNLOADER_REPO = "https://github.com/teaxus/video-downloader"
DOWNLOADER_NAME = "video-downloader"

OK, WARN, BAD = "✅", "⚠️ ", "❌"


def _print(symbol: str, msg: str):
    print(f"{symbol} {msg}")


# ─── Individual checks ──────────────────────────────────────────────────────

def check_python() -> bool:
    v = sys.version_info
    if v >= (3, 8):
        _print(OK, f"Python {v.major}.{v.minor}.{v.micro}")
        return True
    _print(BAD, f"Python {v.major}.{v.minor} 太旧，需要 3.8+")
    return False


def check_ffmpeg() -> bool:
    ok = True
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        if path:
            _print(OK, f"{tool}: {path}")
        else:
            ok = False
            _print(BAD, f"{tool} 未安装")
    if not ok:
        if sys.platform == "darwin":
            _print(WARN, "安装：brew install ffmpeg")
        elif sys.platform.startswith("linux"):
            _print(WARN, "安装：sudo apt-get install ffmpeg  (或对应包管理器)")
        else:
            _print(WARN, "请从 https://ffmpeg.org/download.html 安装 ffmpeg")
    return ok


def check_yaml():
    try:
        import yaml  # noqa: F401
        _print(OK, "PyYAML 可用（配置解析更稳健）")
    except ImportError:
        _print(WARN, "PyYAML 未安装——已自动降级为内置极简解析器（功能正常，无需处理）")


def find_downloader() -> Path | None:
    """Look for the video-downloader skill in common agent skill locations."""
    candidates = [
        SKILL_DIR.parent / DOWNLOADER_NAME,                 # sibling of this skill
        Path.home() / ".claude" / "skills" / DOWNLOADER_NAME,
        Path.home() / ".agents" / "skills" / DOWNLOADER_NAME,
        Path.home() / ".copilot" / "skills" / DOWNLOADER_NAME,
        Path.home() / ".codex" / "skills" / DOWNLOADER_NAME,
        Path.cwd() / ".github" / "skills" / DOWNLOADER_NAME,
        Path.cwd() / ".agents" / "skills" / DOWNLOADER_NAME,
    ]
    for c in candidates:
        if c.is_dir() and (c / "SKILL.md").exists():
            return c
    return None


def check_downloader() -> bool:
    found = find_downloader()
    if found:
        _print(OK, f"video-downloader 已安装：{found}")
        return True
    _print(WARN, "video-downloader 未安装（仅在线 URL 输入需要它；本地视频不需要）")
    _print(WARN, f"自动安装：python3 scripts/setup.py --install-downloader")
    _print(WARN, f"或手动：git clone {DOWNLOADER_REPO} '{SKILL_DIR.parent / DOWNLOADER_NAME}'")
    return False


def check_config():
    cfg = load_config(None)
    cfg_file = Path(cfg.config_path)
    if cfg_file.exists():
        _print(OK, f"配置文件：{cfg_file}")
    else:
        _print(WARN, f"未发现 config.yaml（可仅用环境变量）；生成模板："
                     f" python3 scripts/setup.py --init-config")

    if cfg.api_key:
        masked = cfg.api_key[:6] + "…" if len(cfg.api_key) > 6 else "***"
        _print(OK, f"API key 已配置（{masked}）")
        key_ok = True
    else:
        _print(BAD, "API key 未配置（config.yaml 的 gemini.api_key 或环境变量 GEMINI_API_KEY）")
        key_ok = False

    if cfg.base_url:
        _print(OK, f"base_url：{cfg.base_url}")
    else:
        _print(BAD, "base_url 未配置")
        key_ok = False

    _print(OK, f"模型：{cfg.model} | 认证：{cfg.auth} | 分析方法：{cfg.profile}")
    return key_ok, cfg


def check_profiles(cfg) -> bool:
    pdir = resolve_prompts_dir(cfg)
    profiles = available_profiles(pdir)
    if profiles:
        _print(OK, f"提示词档案（{pdir}）：{', '.join(profiles)}")
        if cfg.profile not in profiles:
            _print(WARN, f"当前选定档案 '{cfg.profile}' 不在列表中，运行时会报错")
        return True
    _print(BAD, f"未发现任何提示词档案：{pdir}")
    return False


# ─── Actions ────────────────────────────────────────────────────────────────

def init_config() -> int:
    template = SKILL_DIR / "example.config.yaml"
    target = SKILL_DIR / "config.yaml"
    if not template.exists():
        _print(BAD, f"模板缺失：{template}")
        return 1
    if target.exists():
        _print(WARN, f"config.yaml 已存在，未覆盖：{target}")
        return 0
    shutil.copyfile(template, target)
    _print(OK, f"已从模板生成：{target}")
    _print(WARN, "请编辑 config.yaml 填入 gemini.api_key（该文件已被 .gitignore 忽略，不会提交）")
    return 0


def install_downloader() -> int:
    existing = find_downloader()
    if existing:
        _print(OK, f"video-downloader 已存在：{existing}")
        return 0
    if not shutil.which("git"):
        _print(BAD, "git 未安装，无法自动克隆。请手动下载 video-downloader。")
        return 1
    target = SKILL_DIR.parent / DOWNLOADER_NAME
    _print(WARN, f"git clone {DOWNLOADER_REPO} -> {target}")
    r = subprocess.run(["git", "clone", "--depth", "1", DOWNLOADER_REPO, str(target)])
    if r.returncode == 0 and (target / "SKILL.md").exists():
        _print(OK, f"video-downloader 安装完成：{target}")
        return 0
    _print(BAD, "克隆失败（检查网络/权限），可手动安装。")
    return 1


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Gemini Video Tutor setup / doctor")
    ap.add_argument("--init-config", action="store_true",
                    help="Create config.yaml from example.config.yaml")
    ap.add_argument("--install-downloader", action="store_true",
                    help="git clone the companion video-downloader skill")
    args = ap.parse_args()

    if args.init_config:
        sys.exit(init_config())
    if args.install_downloader:
        sys.exit(install_downloader())

    print("── Gemini Video Tutor: 环境检查 ──\n")
    py = check_python()
    ff = check_ffmpeg()
    check_yaml()
    print()
    key_ok, cfg = check_config()
    prof_ok = check_profiles(cfg)
    print()
    check_downloader()
    print()

    blocking = py and ff and key_ok and prof_ok
    if blocking:
        _print(OK, "就绪：可以分析本地视频了。")
        print('   例：python3 scripts/analyze.py "/path/to/video.mp4" -o tutorial.md')
        sys.exit(0)
    else:
        _print(BAD, "尚未就绪：请先解决上面标 ❌ 的项。")
        sys.exit(1)


if __name__ == "__main__":
    main()
