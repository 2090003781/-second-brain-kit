#!/usr/bin/env python3
"""
Obsidian Vault Onboarding Assistant
====================================
Scans an existing Obsidian vault, analyses its current structure, and suggests
how to organise content into the second-brain-kit directory layout.

Features:
  - Detects existing directories and note distribution
  - Suggests a target structure based on tags, folder names, and content
  - Optionally re-organises notes (copy mode by default; --move to relocate)
  - Generates a migration report (Markdown)

Usage:
    python obsidian-onboard.py --vault /path/to/your/vault
    python obsidian-onboard.py --vault /path/to/your/vault --dry-run
    python obsidian-onboard.py --vault /path/to/your/vault --move
"""

import argparse
import json
import re
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

# ===========================================================================
# Target structure recommended by second-brain-kit
# ===========================================================================
TARGET_STRUCTURE = {
    "记忆": {
        "全局": {"规则.md", "高频错误.md"},
        "编程": {"规则.md", "高频错误.md"},
        "写作": {"规则.md", "高频错误.md"},
        "QQ Bot": {"规则.md", "高频错误.md"},
        "游戏开发": {"规则.md", "高频错误.md"},
    },
    "知识库": {},
    "话题": {},
    "项目": {},
    "日报": {},
    "流程库": {},
    "待办清单.md": None,
}

PLAN_HEADING = "## Suggested Directory Structure"

# ---------------------------------------------------------------------------
# Heuristic classifiers
# ---------------------------------------------------------------------------

_PROGRAMMING_KEYWORDS = frozenset({
    "code", "programming", "python", "go", "golang", "java", "javascript",
    "typescript", "rust", "cpp", "c++", "algorithm", "data structure",
    "api", "sdk", "library", "framework", "coding", "program", "script",
    "debug", "compiler", "function", "class", "module", "import", "git",
    "github", "ci/cd", "testing", "unit test", "deployment",
})

_WRITING_KEYWORDS = frozenset({
    "writing", "article", "blog", "essay", "draft", "copy", "content",
    "prose", "story", "narrative", "documentation", "doc", "manual",
    "readme", "changelog", "guide", "tutorial",
})

_QQ_BOT_KEYWORDS = frozenset({
    "qq", "bot", "chatbot", "qq bot", "qqbot", "mirai", "go-cqhttp",
    "onebot", "message", "group message", "friend message",
})

_GAME_DEV_KEYWORDS = frozenset({
    "game", "gamedev", "unity", "unreal", "godot", "blender", "3d",
    "2d", "sprite", "texture", "shader", "level design", "gameplay",
    "godot engine", "ue5",
})

_DAILY_KEYWORDS = frozenset({
    "daily", "journal", "diary", "log", "202", "daily note",
})

_PROJECT_KEYWORDS = frozenset({
    "project", "sprint", "milestone", "roadmap", "task", "todo",
    "kanban", "backlog",
})

_KNOWLEDGE_KEYWORDS = frozenset({
    "knowledge", "wiki", "reference", "learn", "study", "note",
    "concept", "theory", "principle", "fundamental",
})


def classify_folder(name: str) -> str | None:
    """Suggest a target top-level folder based on folder name."""
    lower = name.lower()
    if lower in ("记忆", "memory"):
        return "记忆"
    if lower in ("知识库", "知识", "knowledge", "knowledge base"):
        return "知识库"
    if lower in ("话题", "topics", "topic"):
        return "话题"
    if lower in ("项目", "projects", "project"):
        return "项目"
    if lower in ("日报", "daily", "journal", "日记"):
        return "日报"
    if lower in ("流程库", "流程", "process", "workflow"):
        return "流程库"
    if lower in ("待办", "todo", "待办清单"):
        return "待办清单.md"
    return None


def classify_note(path: Path) -> str | None:
    """Suggest a target subdirectory based on note content and tags."""
    try:
        text = path.read_text("utf-8", errors="replace")
    except Exception:
        return None

    lower_text = text.lower()
    score_map: dict[str, int] = {}

    # Check frontmatter tags first
    fm_tags = _extract_frontmatter_tags(text)
    for tag in fm_tags:
        if tag in ("programming", "coding", "编程"):
            score_map["记忆/编程"] = score_map.get("记忆/编程", 0) + 3
        if tag in ("writing", "写作"):
            score_map["记忆/写作"] = score_map.get("记忆/写作", 0) + 3
        if tag in ("qq", "qq-bot", "qqbot"):
            score_map["记忆/QQ Bot"] = score_map.get("记忆/QQ Bot", 0) + 3
        if tag in ("game-dev", "gamedev", "游戏开发"):
            score_map["记忆/游戏开发"] = score_map.get("记忆/游戏开发", 0) + 3

    # Check keywords in text
    for kw in _PROGRAMMING_KEYWORDS:
        if kw in lower_text:
            score_map["记忆/编程"] = score_map.get("记忆/编程", 0) + 1
    for kw in _WRITING_KEYWORDS:
        if kw in lower_text:
            score_map["记忆/写作"] = score_map.get("记忆/写作", 0) + 1
    for kw in _QQ_BOT_KEYWORDS:
        if kw in lower_text:
            score_map["记忆/QQ Bot"] = score_map.get("记忆/QQ Bot", 0) + 1
    for kw in _GAME_DEV_KEYWORDS:
        if kw in lower_text:
            score_map["记忆/游戏开发"] = score_map.get("记忆/游戏开发", 0) + 1
    for kw in _DAILY_KEYWORDS:
        if kw in lower_text:
            score_map["日报"] = score_map.get("日报", 0) + 1
    for kw in _PROJECT_KEYWORDS:
        if kw in lower_text:
            score_map["项目"] = score_map.get("项目", 0) + 1
    for kw in _KNOWLEDGE_KEYWORDS:
        if kw in lower_text:
            score_map["知识库"] = score_map.get("知识库", 0) + 1

    if not score_map:
        return None

    # Return highest-scoring category
    best = max(score_map, key=score_map.get)
    if score_map[best] < 2:
        return None
    return best


def _extract_frontmatter_tags(text: str) -> list[str]:
    """Extract tags from YAML frontmatter."""
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return []
    fm = m.group(1)
    # Match `tags: [...]` or `tag: [...]` or `tags: foo`
    tags = []
    for line in fm.split("\n"):
        line = line.strip()
        if line.startswith("tags:") or line.startswith("tag:"):
            val = line.split(":", 1)[1].strip()
            # List syntax
            if val.startswith("["):
                raw = val.strip("[]").split(",")
                for r in raw:
                    t = r.strip().strip('"').strip("'")
                    if t:
                        tags.append(t.lower())
            else:
                tags.append(val.lower())
    return tags


# ===========================================================================
# Scanner
# ===========================================================================

def scan_vault(vault_path: Path) -> dict[str, Any]:
    """Scan the vault and return analysis data."""
    existing_dirs = set()
    existing_files = []
    file_count_by_ext = Counter()

    for entry in vault_path.rglob("*"):
        if entry.is_dir():
            # Skip hidden dirs and reasonix-managed dirs
            if entry.name.startswith(".") or entry.name in ("reasonix-raw",):
                continue
            rel = entry.relative_to(vault_path)
            existing_dirs.add(str(rel))
        elif entry.is_file() and entry.suffix.lower() == ".md":
            rel = str(entry.relative_to(vault_path))
            existing_files.append(entry)
            file_count_by_ext[entry.suffix.lower()] += 1

    # Classify existing notes
    note_classification: dict[str, list[Path]] = {}
    uncategorised = []
    for f in existing_files:
        cat = classify_note(f)
        key = cat if cat else "_uncategorised"
        note_classification.setdefault(key, []).append(f)
        if not cat:
            uncategorised.append(f)

    # Detect existing structure overlaps with target
    structure_intersection = set()
    structure_missing = set()
    for target_dir in TARGET_STRUCTURE:
        target_path = vault_path / target_dir
        if target_path.exists():
            structure_intersection.add(target_dir)
        else:
            structure_missing.add(target_dir)

    return {
        "vault_path": vault_path,
        "existing_dirs": existing_dirs,
        "total_md_files": len(existing_files),
        "file_count_by_ext": file_count_by_ext,
        "note_classification": note_classification,
        "uncategorised_count": len(uncategorised),
        "uncategorised_top10": [str(p.relative_to(vault_path)) for p in uncategorised[:10]],
        "structure_intersection": structure_intersection,
        "structure_missing": structure_missing,
    }


# ===========================================================================
# Migration planner
# ===========================================================================

def build_migration_plan(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a list of proposed actions."""
    actions = []
    vault_path = analysis["vault_path"]

    # Create missing target directories
    for missing in analysis["structure_missing"]:
        if missing.endswith(".md"):
            continue  # files, not dirs
        actions.append({
            "type": "create_dir",
            "target": missing,
            "path": str(vault_path / missing),
            "reason": "Required by second-brain-kit structure",
        })

    # Move classified notes
    for cat, files in analysis["note_classification"].items():
        if cat == "_uncategorised":
            continue
        target_dir = vault_path / cat
        actions.append({
            "type": "create_dir_if_missing",
            "target": cat,
            "path": str(target_dir),
            "reason": f"Target for {len(files)} classified notes",
        })
        for f in files:
            rel = f.relative_to(vault_path)
            target_path = target_dir / f.name
            if f.parent == target_dir:
                continue  # already there
            actions.append({
                "type": "copy",
                "source": str(rel),
                "target": str(target_path.relative_to(vault_path)),
                "reason": f"Classified as {cat}",
            })

    return actions


# ===========================================================================
# Reporter
# ===========================================================================

def generate_report(analysis: dict[str, Any], actions: list[dict[str, Any]],
                    dry_run: bool = True) -> str:
    """Produce a Markdown migration report."""
    lines = [
        f"# Vault Migration Report",
        f"",
        f"**Vault:** {analysis['vault_path']}",
        f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"**Total .md files:** {analysis['total_md_files']}",
        f"**Uncategorised:** {analysis['uncategorised_count']}",
        f"**Mode:** {'Dry run (no changes)' if dry_run else 'Live (files will be moved/copied)'}",
        f"",
        f"---",
        f"",
    ]

    # Existing structure vs target
    lines.append("## Current Structure vs. Target\n")
    lines.append("| Existing | Required by Kit | Status |")
    lines.append("|----------|----------------|--------|")
    for target, _ in sorted(TARGET_STRUCTURE.items(), key=lambda x: x[0]):
        exists = target in analysis["structure_intersection"]
        status = "✅ Exists" if exists else "❌ Missing"
        lines.append(f"| – | `{target}` | {status} |")
    lines.append("")

    # Note classification summary
    lines.append("## Note Classification\n")
    lines.append("| Category | Count |")
    lines.append("|----------|-------|")
    total_classified = 0
    for cat, files in sorted(analysis["note_classification"].items(), key=lambda x: -len(x[1])):
        label = cat if cat != "_uncategorised" else "*Uncategorised*"
        lines.append(f"| {label} | {len(files)} |")
        if cat != "_uncategorised":
            total_classified += len(files)
    lines.append(f"| **Total classified** | **{total_classified}** |")
    lines.append("")

    if analysis["uncategorised_top10"]:
        lines.append("### Top Uncategorised Files\n")
        for p in analysis["uncategorised_top10"]:
            lines.append(f"- `{p}`")
        lines.append("")

    # Migration plan summary
    dir_creates = [a for a in actions if a["type"] == "create_dir"]
    moves = [a for a in actions if a["type"] == "move"]
    copies = [a for a in actions if a["type"] == "copy"]

    lines.append("## Proposed Actions\n")
    lines.append(f"- **Directories to create:** {len(dir_creates)}")
    lines.append(f"- **Files to move:** {len(moves)}")
    lines.append(f"- **Files to copy:** {len(copies)}")
    lines.append("")

    if dir_creates:
        lines.append("### New Directories\n")
        for a in dir_creates:
            lines.append(f"- `{a['target']}` — {a['reason']}")
        lines.append("")

    if moves:
        lines.append("### File Moves (classified notes)\n")
        lines.append("| Source | Target |")
        lines.append("|--------|--------|")
        for a in moves[:30]:  # cap display
            lines.append(f"| `{a['source']}` | `{a['target']}` |")
        if len(moves) > 30:
            lines.append(f"| … and {len(moves) - 30} more | |")
        lines.append("")

    lines.append("---\n")
    lines.append(f"*Generated by obsidian-onboard.py — re-run with `--no-dry-run` to apply.*\n")

    return "\n".join(lines)


# ===========================================================================
# Migration executor
# ===========================================================================

def execute_actions(actions: list[dict[str, Any]], dry_run: bool, vault_path: Path):
    """Perform (or simulate) the migration actions."""
    for action in actions:
        atype = action["type"]
        if atype == "create_dir":
            p = Path(action["path"])
            if dry_run:
                print(f"  [DRY-RUN] mkdir -p {p}")
            else:
                p.mkdir(parents=True, exist_ok=True)
                print(f"  [OK] Created {p}")
        elif atype == "create_dir_if_missing":
            p = Path(action["path"])
            if not p.exists():
                if dry_run:
                    print(f"  [DRY-RUN] mkdir -p {p}")
                else:
                    p.mkdir(parents=True, exist_ok=True)
                    print(f"  [OK] Created {p}")
        elif atype == "copy":
            src = vault_path / action["source"]
            dst = vault_path / action["target"]
            if dry_run:
                print(f"  [DRY-RUN] cp {action['source']} → {action['target']}")
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                print(f"  [OK] Copied {action['source']} → {action['target']}")
        elif atype == "move":
            src = vault_path / action["source"]
            dst = vault_path / action["target"]
            if dry_run:
                print(f"  [DRY-RUN] mv {action['source']} → {action['target']}")
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                print(f"  [OK] Moved {action['source']} → {action['target']}")


# ===========================================================================
# CLI
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Obsidian Vault Onboarding Assistant — scan, classify, and migrate your vault to the second-brain-kit structure."
    )
    parser.add_argument("--vault", "-v", required=True,
                        help="Path to your Obsidian vault")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Preview changes without applying them (default)")
    parser.add_argument("--move", action="store_true", default=False,
                        help="Move files (default is copy — originals are preserved)")
    parser.add_argument("--output", "-o", default=None,
                        help="Save report to a file (default: print to stdout)")
    parser.add_argument("--classify-only", action="store_true", default=False,
                        help="Only classify notes, do not build or execute a migration plan")

    args = parser.parse_args()

    vault_path = Path(args.vault).expanduser().resolve()
    if not vault_path.exists() or not vault_path.is_dir():
        print(f"[ERROR] Vault path does not exist: {vault_path}")
        sys.exit(1)

    print(f"[*] Scanning vault: {vault_path}")
    analysis = scan_vault(vault_path)

    print(f"[*] Found {analysis['total_md_files']} Markdown files")
    print(f"[*] Uncategorised: {analysis['uncategorised_count']}")

    if args.classify_only:
        print("\n=== Classification Summary ===")
        for cat, files in sorted(analysis["note_classification"].items(),
                                  key=lambda x: -len(x[1])):
            label = cat if cat != "_uncategorised" else "Uncategorised"
            print(f"  {label}: {len(files)} files")
        return

    # Build migration plan
    actions = build_migration_plan(analysis)

    # Optionally switch from copy to move
    if args.move:
        for a in actions:
            if a["type"] == "copy":
                a["type"] = "move"

    # Generate report
    report = generate_report(analysis, actions, dry_run=not args.move or args.dry_run)

    if args.output:
        out_path = Path(args.output)
        out_path.write_text(report, encoding="utf-8")
        print(f"[*] Report saved to {out_path}")
    else:
        print("\n" + report)

    # Execute if not dry-run
    if not args.dry_run or args.move:
        if args.dry_run:
            print("\n[*] DRY RUN — no files will be changed\n")
        else:
            print("\n[*] Executing migration plan...\n")

        execute_actions(actions, dry_run=args.dry_run, vault_path=vault_path)

        if not args.dry_run:
            print("\n[*] Migration complete. Open your vault in Obsidian to see the new structure.")
    else:
        print("\n[*] Use `--no-dry-run` or `--move` to apply changes.")


if __name__ == "__main__":
    import sys
    main()

