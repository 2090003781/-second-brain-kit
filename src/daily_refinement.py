"""
daily_refinement.py — Daily topic-file scan and knowledge refinement.

Scans yesterday's topic files in the vault, extracts inline markers,
compares with known error/rule files, updates them, and generates a
daily refinement report under 日报/.

Configuration:
  - vault path from config.toml (via config.py)
  - memory dirs from config.toml (memory.dirs)

Usage:
    python src/daily_refinement.py          # normal run
    python src/daily_refinement.py --dry-run  # preview only, no writes
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path
from typing import Any
try:
    from dedup import is_duplicate, content_hash
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from dedup import is_duplicate, content_hash

# ---------------------------------------------------------------------------
# Local config loader — same pattern as existing scripts
# ---------------------------------------------------------------------------
try:
    from config import load_config, vault_path
except ImportError:
    # Allow running from src/ directly
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config import load_config, vault_path  # type: ignore[import-not-found]


# ---------------------------------------------------------------------------
# Constants — marker patterns
# ---------------------------------------------------------------------------

# Line markers we look for in topic files
MARKERS = {
    "❌ 错误": re.compile(r"→ ❌ 错误:\s*(.+)"),
    "💡 经验": re.compile(r"→ 💡 经验:\s*(.+)"),
    "♻️ 可复用": re.compile(r"→ ♻️ 可复用:\s*(.+)"),
    "📌 记录": re.compile(r"→ 📌 记录:\s*(.+)"),
}

# Memory file names (relative to each domain dir)
MEMORY_ERROR_FILE = "高频错误.md"
MEMORY_RULE_FILE = "规则.md"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def yesterday_str() -> str:
    """Return 'YYYY-MM-DD' for yesterday."""
    return (datetime.date.today() - datetime.timedelta(days=1)).isoformat()


def today_str() -> str:
    return datetime.date.today().isoformat()


def find_topic_files(vault: Path, ref_date: str | None = None) -> list[Path]:
    """Return topic .md files modified on *ref_date* (default: yesterday)."""
    date = (datetime.date.today() - datetime.timedelta(days=1)) if ref_date is None else datetime.date.fromisoformat(ref_date)
    topic_dir = vault / "话题"
    if not topic_dir.is_dir():
        return []
    results: list[Path] = []
    for f in topic_dir.iterdir():
        if f.suffix.lower() != ".md":
            continue
        if not f.is_file():
            continue
        # Check file modification date, not filename
        mtime = datetime.datetime.fromtimestamp(f.stat().st_mtime).date()
        if mtime == date:
            results.append(f)
    return results


def extract_markers(content: str) -> dict[str, list[str]]:
    """Scan *content* for marker lines and group by type."""
    found: dict[str, list[str]] = {k: [] for k in MARKERS}
    for line in content.splitlines():
        for label, pattern in MARKERS.items():
            m = pattern.search(line)
            if m:
                found[label].append(m.group(1).strip())
    return found


def load_memory_dirs(cfg: dict[str, Any]) -> list[Path]:
    """Return resolved Paths for each memory domain directory."""
    vault = _resolve_vault(cfg)
    raw_dirs: list[str] = cfg.get("memory", {}).get("dirs", ["记忆/全局"])
    return [vault / d for d in raw_dirs]


def _resolve_vault(cfg: dict[str, Any]) -> Path:
    """Get the vault path from config or environment."""
    vp = vault_path()
    if vp and vp.is_dir():
        return vp
    # fallback: try config["vault"]["path"] directly
    raw = cfg.get("vault", {}).get("path", "")
    if raw:
        return Path(raw).expanduser().resolve()
    print("[daily_refinement] WARNING: vault path not found; using CWD", file=sys.stderr)
    return Path.cwd()


# ---------------------------------------------------------------------------
# Memory file operations
# ---------------------------------------------------------------------------


def _parse_error_count(line: str) -> int | None:
    """Parse '- **次数：** N' from a line, return N or None."""
    m = re.search(r"\*\*次数：\*\*\s*(\d+)", line)
    return int(m.group(1)) if m else None


def _update_error_count(file_path: Path, error_keyword: str) -> str | None:
    """
    Increment the count for an existing error matching *error_keyword*.
    Returns None if no match found, or a summary string if updated.
    """
    if not file_path.is_file():
        return None
    content = file_path.read_text("utf-8")
    lines = content.splitlines()

    # Try to find a section that contains the keyword
    section_start: int | None = None
    count_line_idx: int | None = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and error_keyword.lower() in line.lower():
            section_start = i
        if section_start is not None and i > section_start:
            cnt = _parse_error_count(line)
            if cnt is not None:
                count_line_idx = i
                break

    if count_line_idx is None:
        return None

    new_count = _parse_error_count(lines[count_line_idx])
    if new_count is None:
        return None
    lines[count_line_idx] = re.sub(
        r"(\*\*次数：\*\*\s*)\d+",
        rf"\g<1>{new_count + 1}",
        lines[count_line_idx],
    )
    file_path.write_text("\n".join(lines) + "\n", "utf-8")
    return f"已知错误，次数+1 → 累计{new_count + 1}次"


def _append_new_error(file_path: Path, domain: str, content_text: str) -> str:
    """Append a new error entry to the error file."""
    # Determine max error number
    if file_path.is_file():
        text = file_path.read_text("utf-8")
        nums = [int(m) for m in re.findall(r"^## #(\d+)", text, re.MULTILINE)]
        next_num = max(nums) + 1 if nums else 1
    else:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        next_num = 1
        text = f"# High-Frequency Errors — {domain} Domain\n\n---\n"

    entry = (
        f"\n## #{next_num} — {content_text[:60]}\n\n"
        f"- **次数：** 1\n"
        f"- **现象：** {content_text}\n"
        f"- **解决：** (pending refinement)\n\n---\n"
    )

    with file_path.open("a", encoding="utf-8") as f:
        f.write(entry)
    return f"新增1条"


def _find_similar_rule(file_path: Path, content_text: str) -> str | None:
    """Check if a rule file already has a rule similar to *content_text*."""
    if not file_path.is_file():
        return None
    text = file_path.read_text("utf-8")
    # Simple keyword overlap check
    keywords = set(re.findall(r"\S+", content_text))
    for line in text.splitlines():
        line_keywords = set(re.findall(r"\S+", line))
        overlap = keywords & line_keywords
        if len(overlap) >= 3:  # at least 3 words in common
            return line.strip()
    return None


def _append_new_rule(file_path: Path, domain: str, content_text: str) -> str:
    """Append a new rule derived from experience."""
    if file_path.is_file():
        text = file_path.read_text("utf-8")
        nums = [int(m) for m in re.findall(r"^## Rule (\d+)", text, re.MULTILINE)]
        next_num = max(nums) + 1 if nums else 1
    else:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        next_num = 1
        text = f"# Rules — {domain} Domain\n\n"

    rule_entry = (
        f"\n## Rule {next_num} — {content_text[:50]}\n\n"
        f"1. **Derived from experience**: {content_text}\n"
    )

    with file_path.open("a", encoding="utf-8") as f:
        f.write(rule_entry)
    return f"写入 {domain}/规则.md"


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def process_vault(cfg: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    """
    Main pipeline.

    Returns a report dict structured for the daily markdown output.
    """
    vault = _resolve_vault(cfg)
    ref_date = yesterday_str()
    topic_files = find_topic_files(vault, ref_date)
    memory_dirs = load_memory_dirs(cfg)

    report: dict[str, Any] = {
        "date": ref_date,
        "audit_records": [],  # list of {type, content, action}
        "updates": [],        # list of "更新了 ..."
        "pending": [],        # list of pending-review items
        "errors": [],
    }

    if not topic_files:
        report["errors"].append(f"没有找到 {ref_date} 的话题文件")
        return report

    # Collect all markers from topic files
    all_markers: dict[str, list[str]] = {k: [] for k in MARKERS}
    for tf in topic_files:
        content = tf.read_text("utf-8", errors="replace")
        extracted = extract_markers(content)
        for k in all_markers:
            all_markers[k].extend(extracted[k])

    # Process each marker type
    for label, items in all_markers.items():
        for item in items:
            action = _classify_and_update(item, label, memory_dirs, dry_run)
            record = {"type": label, "content": item, "action": action}
            report["audit_records"].append(record)
            if action.startswith("待分类"):
                report["pending"].append(f"「{item}」→ 可能是{label}？待周自检确认")
            elif action.startswith("更新了"):
                report["updates"].append(action)

    return report


def _classify_and_update(
    item: str, label: str, memory_dirs: list[Path], dry_run: bool
) -> str:
    """
    Determine what to do with a marker item.

    Returns a human-readable action string.
    """
    # ---- Errors go to 高频错误.md ----
    if label == "❌ 错误":
        for d in memory_dirs:
            err_file = d / MEMORY_ERROR_FILE
            if dry_run and err_file.is_file():
                # Check if it would match
                text = err_file.read_text("utf-8")
                if item.lower() in text.lower():
                    return f"[DRY-RUN] 已知错误（匹配 {d.name}），次数+1"
            if not dry_run:
                result = _update_error_count(err_file, item)
                if result:
                    return f"已知错误，{result} → {d.name}"
        # Not found in any domain — append to default domain
        default_domain = memory_dirs[0] if memory_dirs else Path()
        err_file = default_domain / MEMORY_ERROR_FILE
        if dry_run:
            return f"[DRY-RUN] 新错误 → 将写入 {default_domain.name}/高频错误.md"
        summary = _append_new_error(err_file, default_domain.name, item)
        return f"新错误 → {summary}（{default_domain.name}）"

    # ---- Experience → 规则.md ----
    if label == "💡 经验":
        for d in memory_dirs:
            rule_file = d / MEMORY_RULE_FILE
            similar = _find_similar_rule(rule_file, item) if rule_file.is_file() else None
            if similar:
                if dry_run:
                    return f"[DRY-RUN] 已有类似规则（{d.name}），将合并"
                return f"已有类似规则（{d.name}），已合并"
        # New experience — write to default domain
        default_domain = memory_dirs[0] if memory_dirs else Path()
        rule_file = default_domain / MEMORY_RULE_FILE
        if dry_run:
            return f"[DRY-RUN] 新经验 → 将写入 {default_domain.name}/规则.md"
        summary = _append_new_rule(rule_file, default_domain.name, item)
        return f"新经验 → {summary}"

    # ---- Reusable process → 规则.md (as a process note) ----
    if label == "♻️ 可复用":
        default_domain = memory_dirs[0] if memory_dirs else Path()
        rule_file = default_domain / MEMORY_RULE_FILE
        similar = _find_similar_rule(rule_file, item) if rule_file.is_file() else None
        if similar:
            if dry_run:
                return f"[DRY-RUN] 已有类似流程（{default_domain.name}），将合并"
            return f"已有类似流程（{default_domain.name}），已合并"
        if dry_run:
            return f"[DRY-RUN] 新流程 → 将写入 {default_domain.name}/规则.md"
        summary = _append_new_rule(rule_file, default_domain.name, f"[流程] {item}")
        return f"新流程 → {summary}"

    # ---- Uncategorised record → pending review ----
    if label == "📌 记录":
        return "待分类 → 转入周自检"

    return "未处理"


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def generate_report_md(report: dict[str, Any]) -> str:
    """Render the report dict as a Markdown string."""
    lines: list[str] = []
    lines.append(f"# {report['date']} 日提炼报告")
    lines.append("")

    # Audit records table
    lines.append("## 审核记录")
    lines.append("| 类型 | 内容 | 处理 |")
    lines.append("|------|------|------|")
    for rec in report["audit_records"]:
        # Escape pipes in content
        content = rec["content"].replace("|", "\\|")
        action = rec["action"].replace("|", "\\|")
        lines.append(f"| {rec['type']} | {content} | {action} |")

    lines.append("")

    # Associated updates
    lines.append("## 关联更新")
    if report["updates"]:
        for u in report["updates"]:
            lines.append(f"- {u}")
    else:
        lines.append("- 无更新")
    lines.append("")

    # Pending review
    lines.append("## 待审")
    if report["pending"]:
        for p in report["pending"]:
            lines.append(f"- {p}")
    else:
        lines.append("- 无待审项")
    lines.append("")

    # Errors / warnings
    if report["errors"]:
        lines.append("## 备注")
        for e in report["errors"]:
            lines.append(f"- ⚠️ {e}")
        lines.append("")

    return "\n".join(lines)


def write_report(report: dict[str, Any], vault: Path, dry_run: bool) -> None:
    """Write the daily report to 日报/<date>.提炼报告.md."""
    date = report["date"]
    report_dir = vault / "日报"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{date}.提炼报告.md"

    md = generate_report_md(report)
    if dry_run:
        print(f"[DRY-RUN] Would write to: {report_path}")
        print(md)
    else:
        report_path.write_text(md, "utf-8")
        print(f"[daily_refinement] Report written: {report_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Daily refinement — scan topic files and update memory."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only, do not modify any files.",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Target date YYYY-MM-DD (default: yesterday).",
    )
    args = parser.parse_args()

    cfg = load_config()
    vault = _resolve_vault(cfg)

    print(f"[daily_refinement] Vault: {vault}")
    print(f"[daily_refinement] Dry-run: {args.dry_run}")
    print(f"[daily_refinement] Target date: {args.date or yesterday_str()}")

    report = process_vault(cfg, dry_run=args.dry_run)
    write_report(report, vault, dry_run=args.dry_run)

    if report["audit_records"]:
        print(f"[daily_refinement] Processed {len(report['audit_records'])} markers.")
    if report["pending"]:
        print(f"[daily_refinement] {len(report['pending'])} items pending review.")
    if report["errors"]:
        for e in report["errors"]:
            print(f"[daily_refinement] WARNING: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()

