from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
import venv
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parent
SERVER_PATH = PROJECT_ROOT / "server.py"
VENV_DIR = PROJECT_ROOT / ".venv"
IS_WINDOWS = os.name == "nt"
SKILLS = {
    "codextoolSkill": PROJECT_ROOT / ".codex" / "skills" / "codextoolSkill",
    "minecraft-modding-skill": PROJECT_ROOT / ".codex" / "skills" / "minecraft-modding-skill",
}
DEFAULT_PACKAGES = [
    "pillow",
    "matplotlib",
    "playwright",
    "PySide6",
    "pyautogui",
    "rapidocr_onnxruntime",
]
MANAGED_TOML_START = "# >>> CodexTools MCP >>>"
MANAGED_TOML_END = "# <<< CodexTools MCP <<<"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install CodexTools MCP, optional bundled skills, and local client config.",
    )
    parser.add_argument(
        "--skills",
        default="prompt",
        help="Comma-separated skill names, 'all', 'none', or 'prompt' for interactive selection.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Run non-interactively with sensible defaults.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned changes without writing files or installing packages.",
    )
    parser.add_argument(
        "--no-user-config",
        action="store_true",
        help="Do not modify ~/.codex/config.toml.",
    )
    parser.add_argument(
        "--skip-playwright-install",
        action="store_true",
        help="Skip 'python -m playwright install chromium'.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print_banner()
    ensure_python_version()
    venv_python = ensure_virtualenv(args.dry_run)
    install_dependencies(venv_python, args.dry_run)
    if not args.skip_playwright_install:
        install_playwright_browser(venv_python, args.dry_run)
    selected_skills = resolve_skill_selection(args.skills, args.yes)
    install_skills(selected_skills, args.dry_run)
    write_repo_config_files(venv_python, args.dry_run)
    if not args.no_user_config:
        write_user_codex_config(venv_python, args.dry_run)
    print_summary(venv_python, selected_skills, args)
    return 0


def print_banner() -> None:
    print("=" * 72)
    print("CodexTools 一键安装")
    print("=" * 72)
    print(f"项目目录: {PROJECT_ROOT}")
    print()


def ensure_python_version() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit("需要 Python 3.10 或更高版本。")


def ensure_virtualenv(dry_run: bool) -> Path:
    if IS_WINDOWS:
        python_path = VENV_DIR / "Scripts" / "python.exe"
    else:
        python_path = VENV_DIR / "bin" / "python"
    if python_path.exists():
        print(f"[OK] 已发现虚拟环境: {python_path}")
        return python_path
    print(f"[RUN] 创建虚拟环境: {VENV_DIR}")
    if not dry_run:
        builder = venv.EnvBuilder(with_pip=True)
        builder.create(VENV_DIR)
    return python_path


def install_dependencies(python_executable: Path, dry_run: bool) -> None:
    command = [str(python_executable), "-m", "pip", "install", "--upgrade", "pip", *DEFAULT_PACKAGES]
    run_step("安装 Python 依赖", command, dry_run=dry_run)


def install_playwright_browser(python_executable: Path, dry_run: bool) -> None:
    command = [str(python_executable), "-m", "playwright", "install", "chromium"]
    run_step("安装 Playwright Chromium 浏览器", command, dry_run=dry_run, allow_fail=True)


def resolve_skill_selection(raw_value: str, assume_yes: bool) -> list[str]:
    normalized = str(raw_value or "prompt").strip().lower()
    if normalized in {"none", "", "0"}:
        return []
    if normalized == "all":
        return list(SKILLS.keys())
    if normalized != "prompt":
        selected = []
        for item in [part.strip() for part in raw_value.split(",") if part.strip()]:
            if item not in SKILLS:
                raise SystemExit(f"未知 skill: {item}")
            selected.append(item)
        return selected
    if assume_yes:
        return list(SKILLS.keys())
    print("请选择要安装的 skills:")
    print("  1. codextoolSkill")
    print("  2. minecraft-modding-skill")
    print("  A. 全部安装")
    print("  N. 不安装")
    answer = input("输入选项（例如 1,2 或 A）: ").strip().lower()
    if not answer or answer == "a":
        return list(SKILLS.keys())
    if answer == "n":
        return []
    selected: list[str] = []
    mapping = {"1": "codextoolSkill", "2": "minecraft-modding-skill"}
    for token in [part.strip() for part in answer.replace(";", ",").split(",") if part.strip()]:
        skill_name = mapping.get(token, token)
        if skill_name not in SKILLS:
            raise SystemExit(f"未知 skill: {token}")
        if skill_name not in selected:
            selected.append(skill_name)
    return selected


def install_skills(selected_skills: Iterable[str], dry_run: bool) -> None:
    selected = list(selected_skills)
    if not selected:
        print("[SKIP] 未选择任何 skill。")
        return
    codex_dir = Path.home() / ".codex" / "skills"
    agents_dir = Path.home() / ".agents" / "skills"
    for skill_name in selected:
        source = SKILLS[skill_name]
        if not source.exists():
            raise SystemExit(f"skill 不存在: {source}")
        for target_root in (codex_dir, agents_dir):
            target = target_root / skill_name
            print(f"[RUN] 安装 skill {skill_name} -> {target}")
            if dry_run:
                continue
            target_root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target, dirs_exist_ok=True)


def write_repo_config_files(python_executable: Path, dry_run: bool) -> None:
    json_path = PROJECT_ROOT / "mcp.codextools.json"
    toml_path = PROJECT_ROOT / "codex.config.codextools.toml"
    json_content = json.dumps(
        {
            "mcpServers": {
                "CodexTools": {
                    "command": to_posix_path(python_executable),
                    "args": ["-u", "-X", "utf8", to_posix_path(SERVER_PATH)],
                }
            }
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    toml_content = textwrap.dedent(
        f"""
        [mcp_servers.CodexTools]
        command = \"{to_posix_path(python_executable)}\"
        args = [\"-u\", \"-X\", \"utf8\", \"{to_posix_path(SERVER_PATH)}\"]
        enabled = true
        """
    ).lstrip()
    print(f"[RUN] 写入项目配置: {json_path}")
    if not dry_run:
        json_path.write_text(json_content, encoding="utf-8")
    print(f"[RUN] 写入项目配置: {toml_path}")
    if not dry_run:
        toml_path.write_text(toml_content, encoding="utf-8")


def write_user_codex_config(python_executable: Path, dry_run: bool) -> None:
    config_path = Path.home() / ".codex" / "config.toml"
    block = textwrap.dedent(
        f"""
        {MANAGED_TOML_START}
        [mcp_servers.CodexTools]
        command = \"{to_posix_path(python_executable)}\"
        args = [\"-u\", \"-X\", \"utf8\", \"{to_posix_path(SERVER_PATH)}\"]
        enabled = true
        {MANAGED_TOML_END}
        """
    ).strip()
    existing = ""
    if config_path.exists():
        existing = config_path.read_text(encoding="utf-8")
    updated = replace_or_append_block(existing, block)
    print(f"[RUN] 更新 Codex 配置: {config_path}")
    if not dry_run:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(updated, encoding="utf-8")


def replace_or_append_block(existing: str, block: str) -> str:
    start = existing.find(MANAGED_TOML_START)
    end = existing.find(MANAGED_TOML_END)
    if start != -1 and end != -1 and end >= start:
        end += len(MANAGED_TOML_END)
        prefix = existing[:start].rstrip()
        suffix = existing[end:].lstrip()
        pieces = [piece for piece in (prefix, block, suffix) if piece]
        return "\n\n".join(pieces) + "\n"
    if existing.strip():
        return existing.rstrip() + "\n\n" + block + "\n"
    return block + "\n"


def run_step(title: str, command: list[str], *, dry_run: bool, allow_fail: bool = False) -> None:
    print(f"[RUN] {title}")
    print("      " + " ".join(command))
    if dry_run:
        return
    completed = subprocess.run(command, cwd=PROJECT_ROOT)
    if completed.returncode == 0:
        return
    if allow_fail:
        print(f"[WARN] {title} 失败，稍后可以手动重试。")
        return
    raise SystemExit(f"{title} 失败，退出码 {completed.returncode}。")


def to_posix_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/")


def print_summary(python_executable: Path, selected_skills: list[str], args: argparse.Namespace) -> None:
    print()
    print("安装完成。")
    print(f"Python: {python_executable}")
    print(f"MCP 服务入口: {SERVER_PATH}")
    print(f"已安装 skills: {', '.join(selected_skills) if selected_skills else '无'}")
    print()
    print("下一步:")
    print("1. 重启你的 Codex / MCP 客户端。")
    print("2. 确认客户端里已经启用 CodexTools。")
    if args.no_user_config:
        print(f"3. 如需手动导入配置，可使用: {PROJECT_ROOT / 'codex.config.codextools.toml'}")
    else:
        print(f"3. 已自动写入: {Path.home() / '.codex' / 'config.toml'}")
    print(f"4. JSON 配置样例位于: {PROJECT_ROOT / 'mcp.codextools.json'}")


if __name__ == "__main__":
    raise SystemExit(main())
