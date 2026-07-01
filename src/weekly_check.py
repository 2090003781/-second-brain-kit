"""
weekly_check.py — Weekly self-inspection of the vault.

Checks performed:
  1. Rule conflict detection — compare newly added/modified rules against existing ones.
  2. Link validity — grep [[wikilinks]] in new files, verify target exists.
  3. Error ranking adjustment — demote errors absent for 3 weeks, promote new ones.
  4. Orphan files — files not linked by any other file.
  5. Weekly report — written to 日报/<date>.周自检.md.

Configuration from config.toml (via config.py).

Usage:
    python src/weekly_check.py           # normal run
    python src/weekly_check.py --dry-run  # preview only
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Local config loader
# ---------------------------------------------------------------------------
try:
    from config import load_config, vault_path
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config import load_config, vault_path  # type: ignore[import-not-found]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MEMORY_ERROR_FILE = "高频错误.md"
MEMORY_RULE_FILE = "规则.md"

# How many consecutive weeks of absence before demoting an error
DEMOTE_WEEKS = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def today_str() -> str:
    return datetime.date.today().isoformat()


def last_week_str() -> str:
    """Return YYYY-MM-DD for 7 days ago."""
    return (datetime.date.today() - datetime.timedelta(days=7)).isoformat()


def _resolve_vault(cfg: dict[str, Any]) -> Path:
    vp = vault_path()
    if vp and vp.is_dir():
        return vp
    raw = cfg.get("vault", {}).get("path", "")
    if raw:
        return Path(raw).expanduser().resolve()
    print("[weekly_check] WARNING: vault path not found; using CWD", file=sys.stderr)
    return Path.cwd()


def load_memory_dirs(cfg: dict[str, Any]) -> list[Path]:
    vault = _resolve_vault(cfg)
    raw_dirs: list[str] = cfg.get("memory", {}).get("dirs", ["记忆/全局"])
    return [vault / d for d in raw_dirs]


def load_memory_files(cfg: dict[str, Any]) -> dict[str, Path]:
    """Return {label: path} for all known memory files."""
    files: dict[str, Path] = {}
    for d in load_memory_dirs(cfg):
        err = d / MEMORY_ERROR_FILE
        rule = d / MEMORY_RULE_FILE
        if err.is_file():
            files[f"{d.name}:error"] = err
        if rule.is_file():
            files[f"{d.name}:rule"] = rule
    return files


def all_vault_files(vault: Path) -> list[Path]:
    """Return all .md files under the vault."""
    return sorted(vault.rglob("*.md"))


def files_modified_since(vault: Path, since: str) -> list[Path]:
    """Return .md files modified on or after *since* (YYYY-MM-DD)."""
    try:
        since_date = datetime.date.fromisoformat(since)
    except ValueError:
        return []
    result: list[Path] = []
    for f in vault.rglob("*.md"):
        try:
            mtime = datetime.date.fromtimestamp(f.stat().st_mtime)
        except OSError:
            continue
        if mtime >= since_date:
            result.append(f)
    return sorted(result)


def get_file_content(path: Path) -> str:
    """Read a file safely; return empty string on error."""
    try:
        return path.read_text("utf-8", errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Check 1 — Rule conflicts
# ---------------------------------------------------------------------------


def check_rule_conflicts(cfg: dict[str, Any], dry_run: bool) -> list[dict[str, str]]:
    """
    Compare rules modified in the last week against all existing rules
    and flag contradictions.
    """
    vault = _resolve_vault(cfg)
    since = last_week_str()
    recent = files_modified_since(vault, since)
    memory_files = load_memory_files(cfg)

    # Collect all existing rules: {domain: [rule_line, ...]}
    existing_rules: dict[str, list[str]] = defaultdict(list)
    for label, path in memory_files.items():
        if ":rule" in label:
            domain = label.split(":")[0]
            existing_rules[domain].extend(
                get_file_content(path).splitlines()
            )

    # Build keyword index for existing rules
    conflict_candidates: list[dict[str, str]] = []
    for f in recent:
        if not f.is_file():
            continue
        content = get_file_content(f)
        # Skip if it's not a rule file
        if "规则" not in f.stem and "规则" not in content[:200]:
            continue
        lines = content.splitlines()
        for line in lines:
            if not line.startswith("1.") and not line.startswith("- "):
                continue
            # Check keyword negation patterns
            negations = re.findall(r"(不要|禁止|避免|never|do not|avoid|must not)", line, re.IGNORECASE)
            if not negations:
                continue

            # Look for positive statements about the same topic in existing rules
            keywords = set(re.findall(r"\w{3,}", line))
            for domain, rule_lines in existing_rules.items():
                for rl in rule_lines:
                    rl_keywords = set(re.findall(r"\w{3,}", rl))
                    overlap = keywords & rl_keywords
                    if len(overlap) >= 2:
                        # Check if existing rule says the opposite
                        existing_pos = re.findall(r"(应该|必须|always|must|should|do)", rl, re.IGNORECASE)
                        if existing_pos:
                            conflict_candidates.append({
                                "file": str(f.relative_to(vault)),
                                "line": line.strip(),
                                "conflicts_with": rl.strip(),
                                "domain": domain,
                            })
                            break

    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for c in conflict_candidates:
        key = (c["line"], c["conflicts_with"])
        if key not in seen:
            seen.add(key)
            result.append(c)
    return result


# ---------------------------------------------------------------------------
# Check 2 — Wikilink validity
# ---------------------------------------------------------------------------


def check_links(vault: Path, dry_run: bool) -> list[dict[str, str]]:
    """Find [[wikilinks]] in files modified this week and verify targets."""
    since = last_week_str()
    recent = files_modified_since(vault, since)
    broken: list[dict[str, str]] = []

    wikilink_re = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")

    for f in recent:
        content = get_file_content(f)
        for m in wikilink_re.finditer(content):
            target_name = m.group(1).strip()
            # A wikilink target can be a path relative to vault root or absolute
            # Normalise: remove .md extension if present, try both with and without
            candidates = [
                vault / (target_name + ".md"),
                vault / target_name,
            ]
            # Also try subdirectory patterns
            for p in vault.rglob(f"{target_name}.md"):
                candidates.append(p)
            for p in vault.rglob(target_name):
                candidates.append(p)

            found = any(p.is_file() for p in candidates)
            if not found:
                broken.append({
                    "file": str(f.relative_to(vault)),
                    "link": target_name,
                })
    return broken


# ---------------------------------------------------------------------------
# Check 3 — Error ranking adjustment
# ---------------------------------------------------------------------------


def check_error_rankings(cfg: dict[str, Any], dry_run: bool) -> list[dict[str, str]]:
    """
    Scan 高频错误.md files. Demote errors whose topic hasn't appeared
    in any topic file for DEMOTE_WEEKS weeks; promote newly added errors.
    """
    vault = _resolve_vault(cfg)
    changes: list[dict[str, str]] = []

    # Collect all topic files
    topic_dir = vault / "话题"
    topic_texts: list[str] = []
    if topic_dir.is_dir():
        for f in topic_dir.iterdir():
            if f.suffix.lower() == ".md" and f.is_file():
                topic_texts.append(get_file_content(f))

    combined_topic_text = "\n".join(topic_texts).lower()

    for d in load_memory_dirs(cfg):
        err_file = d / MEMORY_ERROR_FILE
        if not err_file.is_file():
            continue
        content = get_file_content(err_file)
        # Parse error sections
        sections = re.split(r"\n## ", content)
        for section in sections[1:]:  # skip header
            title_line = section.splitlines()[0] if section.splitlines() else ""
            title = title_line.strip()
            # Extract the short description after "#N — "
            m = re.match(r"#\d+\s*[—\-–]\s*(.*)", title)
            desc = m.group(1).strip().lower() if m else title.lower()

            # Count occurrences in recent topic files (last 3 weeks)
            three_weeks_ago = (datetime.date.today() - datetime.timedelta(weeks=3)).isoformat()
            recent_topics = files_modified_since(vault, three_weeks_ago)
            recent_text = ""
            for tf in recent_topics:
                if "话题" in str(tf):
                    recent_text += get_file_content(tf).lower()

            count_in_topic = recent_text.count(desc)
            if count_in_topic == 0:
                # Check if this error was already low-ranked
                if "🔻" not in section:
                    changes.append({
                        "file": str(err_file.relative_to(vault)),
                        "error": title,
                        "action": f"连续3周未出现 → 建议降级",
                    })
            else:
                # Error appeared recently — promote if it had a down-arrow
                if "🔻" in section:
                    changes.append({
                        "file": str(err_file.relative_to(vault)),
                        "error": title,
                        "action": "最近出现 → 建议升级",
                    })

    return changes


# ---------------------------------------------------------------------------
# Check 4 — Orphan files
# ---------------------------------------------------------------------------


def check_orphans(vault: Path, dry_run: bool) -> list[Path]:
    """
    Files that are NOT linked by any other file via [[wikilinks]].
    Excludes common auto-generated dirs.
    """
    all_files = all_vault_files(vault)
    # Build set of all linked targets (without .md extension)
    linked_targets: set[str] = set()
    wikilink_re = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
    for f in all_files:
        content = get_file_content(f)
        for m in wikilink_re.finditer(content):
            linked_targets.add(m.group(1).strip().lower())

    # Also exclude trivial self-references: a file linking to itself
    orphan_candidates: list[Path] = []
    for f in all_files:
        rel = f.relative_to(vault)
        stem_lower = f.stem.lower()
        # Exclude generated directories
        parts = rel.parts
        if any(p in ("reasonix-raw", "日报", ".obsidian", ".trash", "__pycache__") for p in parts):
            continue
        # Check if stem is in linked targets
        if stem_lower not in linked_targets:
            orphan_candidates.append(f)

    return orphan_candidates


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def generate_weekly_report(
    conflicts: list[dict[str, str]],
    broken_links: list[dict[str, str]],
    ranking_changes: list[dict[str, str]],
    orphans: list[Path],
    vault: Path,
    dry_run: bool,
) -> str:
    """Render the weekly check report as Markdown."""
    lines: list[str] = []
    date = today_str()
    lines.append(f"# {date} 周自检报告")
    lines.append("")

    # 1. Rule conflicts
    lines.append("## 1. 规则冲突检测")
    if not conflicts:
        lines.append("- ✅ 未发现规则冲突")
    else:
        for c in conflicts:
            lines.append(
                f"- ⚠️ `{c['file']}` 中 `{c['line']}`\n"
                f"  与 {c['domain']} 的规则 `{c['conflicts_with']}` 可能存在矛盾"
            )
    lines.append("")

    # 2. Broken links
    lines.append("## 2. 链接有效性")
    if not broken_links:
        lines.append("- ✅ 所有 wikilinks 均有效")
    else:
        for b in broken_links:
            lines.append(f"- 🔗 `{b['file']}` → `[[{b['link']}]]` 目标文件不存在")
    lines.append("")

    # 3. Error ranking
    lines.append("## 3. 错误排名调整")
    if not ranking_changes:
        lines.append("- ✅ 无需调整")
    else:
        for r in ranking_changes:
            lines.append(f"- {r['error']} — {r['action']}（{r['file']}）")
    lines.append("")

    # 4. Orphan files
    lines.append("## 4. 孤立文件")
    if not orphans:
        lines.append("- ✅ 无孤立文件")
    else:
        lines.append(f"- 发现 {len(orphans)} 个文件未被任何链接引用：")
        for o in orphans[:20]:  # cap display
            lines.append(f"  - `{o.relative_to(vault)}`")
        if len(orphans) > 20:
            lines.append(f"  - ...及其他 {len(orphans) - 20} 个")
    lines.append("")

    # Summary
    lines.append("## 5. 本周摘要")
    total_warnings = (
        len(conflicts) + len(broken_links) + len(ranking_changes) + len(orphans)
    )
    if total_warnings == 0:
        lines.append("- 一切正常！🎉")
    else:
        lines.append(f"- 共发现 {total_warnings} 个需要注意的项目")
        lines.append("- 请根据以上信息酌情处理")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Weekly self-inspection of the vault."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only, do not modify any files.",
    )
    args = parser.parse_args()

    cfg = load_config()
    vault = _resolve_vault(cfg)

    print(f"[weekly_check] Vault: {vault}")
    print(f"[weekly_check] Dry-run: {args.dry_run}")

    # Run all checks
    conflicts = check_rule_conflicts(cfg, dry_run=args.dry_run)
    broken_links = check_links(vault, dry_run=args.dry_run)
    ranking_changes = check_error_rankings(cfg, dry_run=args.dry_run)
    orphans = check_orphans(vault, dry_run=args.dry_run)

    # Generate report
    md = generate_weekly_report(
        conflicts, broken_links, ranking_changes, orphans, vault, args.dry_run
    )

    # Write report
    report_dir = vault / "日报"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{today_str()}.周自检.md"

    if args.dry_run:
        print(f"[DRY-RUN] Would write to: {report_path}")
        print(md)
    else:
        report_path.write_text(md, "utf-8")
        print(f"[weekly_check] Report written: {report_path}")

    # Summary
    print(f"\n[weekly_check] Results:")
    print(f"  Rule conflicts:    {len(conflicts)}")
    print(f"  Broken links:      {len(broken_links)}")
    print(f"  Ranking changes:   {len(ranking_changes)}")
    print(f"  Orphan files:      {len(orphans)}")


if __name__ == "__main__":
    main()
