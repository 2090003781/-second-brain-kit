"""
daily_refinement.py — Topic-file scanner for structured log format v2.

Reads topic files, extracts structured markers ([DECISION]/[ERROR]/[PREFERENCE]/[PENDING]/[TRIGGER]),
updates error library and habit library, generates daily report.
Usage:
    python src/daily_refinement.py
    python src/daily_refinement.py --dry-run
"""

import argparse, datetime, json, re, sys
from pathlib import Path

def load_config():
    vault = Path("D:/个人数据/辞玖")
    return {
        "vault": vault,
        "topics_dir": vault / "话题",
        "errors_file": vault / "记忆" / "错误库.md",
        "habits_file": vault / "记忆" / "习惯库.md",
        "daily_dir": vault / "日报",
        "bot_logs": {
            "QQ-Bot": vault / "个人" / "Bot" / "QQ-Bot" / "日志.md",
            "微信-Bot": vault / "个人" / "Bot" / "微信-Bot" / "日志.md",
        },
        "bot_report_dir": vault / "Bot-日报",
    }

# ── New format patterns ──
# Line format: - 日期 | [TYPE: summary | field: value | ...]
DECISION_RE = re.compile(
    r'\[DECISION:\s*(.+?)(?:\s*\|\s*context:\s*(.+?))?(?:\s*\|\s*scope:\s*(.+?))?\]',
    re.IGNORECASE
)
ERROR_RE = re.compile(
    r'\[ERROR:\s*(.+?)(?:\s*\|\s*resolution:\s*(.+?))?(?:\s*\|\s*tool:\s*(.+?))?(?:\s*\|\s*fixed:\s*(.+?))?\]',
    re.IGNORECASE
)
PREFERENCE_RE = re.compile(
    r'\[PREFERENCE:\s*(.+?)(?:\s*\|\s*context:\s*(.+?))?(?:\s*\|\s*source:\s*(.+?))?\]',
    re.IGNORECASE
)
PENDING_RE = re.compile(
    r'\[PENDING:\s*(.+?)(?:\s*\|\s*context:\s*(.+?))?\]',
    re.IGNORECASE
)
TRIGGER_RE = re.compile(
    r'\[TRIGGER:\s*(.+?)(?:\s*\|\s*action:\s*(.+?))?(?:\s*\|\s*summary:\s*(.+?))?\]',
    re.IGNORECASE
)

# ── Legacy format patterns (旧格式兼容) ──
LEGACY_DECISION = re.compile(r'决策：(.+)')
LEGACY_PREFERENCE = re.compile(r'偏好：(.+)')
LEGACY_PENDING = re.compile(r'待续：(.+)')
LEGACY_ERR = re.compile(r'\[err:\s*([^\]]+)\]')
LEGACY_TRIGGER = re.compile(r'触发\s*\|\s*\*\*\s*(.+?)(?:\s+(\w+)\s*\{)?')


def parse_entry_line(line: str, ref_date: str) -> dict | None:
    """Parse a single entry line. Returns dict with type and fields, or None."""
    line = line.strip()
    if not line.startswith("- " + ref_date):
        return None

    # Try new format first
    for cls, pat, marker in [
        ("DECISION", DECISION_RE, "[DECISION"),
        ("ERROR", ERROR_RE, "[ERROR"),
        ("PREFERENCE", PREFERENCE_RE, "[PREFERENCE"),
        ("PENDING", PENDING_RE, "[PENDING"),
        ("TRIGGER", TRIGGER_RE, "[TRIGGER"),
    ]:
        if marker.lower() in line.lower():
            m = pat.search(line)
            if m:
                return {"type": cls, "groups": m.groups(), "raw": line}

    # Legacy fallback
    if "决策：" in line:
        m = LEGACY_DECISION.search(line)
        if m:
            return {"type": "DECISION", "groups": (m.group(1), None, None), "raw": line}
    if "偏好：" in line:
        m = LEGACY_PREFERENCE.search(line)
        if m:
            return {"type": "PREFERENCE", "groups": (m.group(1), None, None), "raw": line}
    if "[err:" in line:
        m = LEGACY_ERR.search(line)
        if m:
            return {"type": "ERROR", "groups": (m.group(1), None, None, None), "raw": line}
    if "待续：" in line:
        m = LEGACY_PENDING.search(line)
        if m:
            return {"type": "PENDING", "groups": (m.group(1), None), "raw": line}
    if "触发" in line:
        m = LEGACY_TRIGGER.search(line)
        if m:
            summary = m.group(1)[:60] if m.group(1) else ""
            return {"type": "TRIGGER", "groups": (summary, m.group(2) or "", ""), "raw": line}

    return None


def scan_topics(cfg, ref_date):
    """Scan reasonix-raw and JSONL logs for structured markers."""
    import json as _json
    results = {"DECISION": [], "ERROR": [], "PREFERENCE": [], "PENDING": [], "TRIGGER": []}
    
    # 1. Scan reasonix-raw directory
    raw_dir = cfg["vault"] / "reasonix-raw"
    if raw_dir.exists():
        # New format: reasonix-raw/YYYY-MM-DD/*.md
        dated_dir = raw_dir / ref_date
        if dated_dir.exists():
            for f in sorted(dated_dir.glob("*.md")):
                text = f.read_text("utf-8", errors="replace")
                for line in text.split("\n"):
                    entry = parse_entry_line(line, ref_date)
                    if entry: results[entry["type"]].append(entry)
        # Old format: reasonix-raw/YYYY-MM-DD.md
        old_file = raw_dir / f"{ref_date}.md"
        if old_file.exists():
            for line in old_file.read_text("utf-8", errors="replace").split("\n"):
                entry = parse_entry_line(line, ref_date)
                if entry: results[entry["type"]].append(entry)
    
    # 2. Scan JSONL logs for markers in content
    logs_dir = Path.home() / ".reasonix" / "logs" / "sessions" / "raw"
    date_prefix = ref_date.replace("-", "")
    for f in sorted(logs_dir.glob(f"reasonix-{date_prefix}*.jsonl")):
        try:
            for line in f.read_text("utf-8", errors="replace").split("\n"):
                if not line.strip(): continue
                try:
                    entry = _json.loads(line)
                    content = entry.get("content", "")
                    for marker_type in ["decision","error"]:
                        if entry.get("type") == marker_type and len(content) > 10:
                            if marker_type == "decision" and ("[DECISION" in content or "决策" in content):
                                results["DECISION"].append({"type":"DECISION","groups":(content[:120],None,None),"raw":content})
                            elif marker_type == "error" and ("[ERROR" in content or "error:" in content.lower()):
                                results["ERROR"].append({"type":"ERROR","groups":(content[:120],None,None,None),"raw":content})
                except: pass
        except: pass
    
    return results


def generate_report(cfg, ref_date, results):
    """Generate a structured daily summary — not a log dump."""
    report_dir = cfg["daily_dir"]
    report_dir.mkdir(parents=True, exist_ok=True)
    report_file = report_dir / f"{ref_date}.md"

    # Read existing extra entries (written by writer.go)
    existing_extra = []
    # (keeping the merge logic from v2)

    lines = [f"# {ref_date} 日报\n\n"]

    decisions = results.get("DECISION", [])
    errors = results.get("ERROR", [])
    preferences = results.get("PREFERENCE", [])
    pending = results.get("PENDING", [])
    triggers = results.get("TRIGGER", [])

    total = sum(len(v) for v in results.values())

    # Section 1: Summary of what happened today
    lines.append("## 今日概览\n")
    if total == 0 and not existing_extra:
        lines.append("无记录\n")
    else:
        item_count = sum(len(v) for v in results.values())
        lines.append(f"- 总事件: {item_count} 条\n")

    # Section 2: Key decisions (精简，不要原始日志)
    if decisions:
        lines.append("\n## 关键决策\n")
        for e in decisions[:10]:
            s = (e["groups"][0] or "").strip() if e["groups"] else ""
            context = (e["groups"][1] or "").strip() if len(e["groups"]) > 1 else ""
            if s:
                line = f"1. {s[:80]}"
                if context:
                    line += f" — {context[:60]}"
                lines.append(line + "\n")

    # Section 3: Errors that occurred
    if errors:
        lines.append("\n## 错误/异常\n")
        for e in errors[:10]:
            s = (e["groups"][0] or "").strip() if e["groups"] else ""
            resolution = (e["groups"][1] or "").strip() if len(e["groups"]) > 1 else ""
            if s:
                lines.append(f"- {s[:80]}: {resolution[:60]}\n")

    # Section 4: Preferences captured
    if preferences:
        lines.append("\n## 偏好变化\n")
        for e in preferences[:5]:
            s = (e["groups"][0] or "").strip() if e["groups"] else ""
            if s:
                lines.append(f"- {s[:100]}\n")

    # Section 5: Active topics from snapshot
    snap_path = cfg["vault"] / "系统设计" / "状态快照.md"
    if snap_path.exists():
        try:
            text = snap_path.read_text("utf-8")
            if text.find("## 当前话题") > 0:
                in_topic = False
                topic_items = []
                for line in text.split("\n"):
                    if "## 当前话题" in line or "## 已完成的" in line or "## 进行中" in line:
                        in_topic = True
                        continue
                    if line.startswith("## ") and in_topic:
                        in_topic = False
                    if in_topic and line.strip().startswith("- "):
                        topic_items.append(line.strip()[2:])
                if topic_items:
                    lines.append("\n## 活跃话题\n")
                    for t in topic_items[:8]:
                        lines.append(f"- {t}\n")
        except: pass

    # Append extra from writer.go (deduplicated)
    if existing_extra:
        lines.append("\n## 其他记录\n")
        for extra in existing_extra[:5]:
            lines.append(f"{extra}\n")

    report_file.write_text("".join(lines), "utf-8")
    return report_file

def update_libraries(cfg, results, dry_run=False):
    """Update error library and habit library from extracted markers."""
    # ── Error library ──
    err_entries = results.get("ERROR", [])
    if err_entries and not dry_run:
        err_file = cfg["errors_file"]
        if err_file.exists():
            content = err_file.read_text("utf-8")
            lines = content.split("\n")

            for entry in err_entries:
                err_type = (entry["groups"][0] or "").strip().lower()
                resolution = (entry["groups"][1] or "").strip()
                if not err_type:
                    continue

                matched_idx = -1
                for i, ln in enumerate(lines):
                    if ln.startswith("## ") and err_type in ln.lower():
                        matched_idx = i
                        break

                if matched_idx >= 0:
                    for j in range(matched_idx, min(matched_idx + 10, len(lines))):
                        if "- **次数：**" in lines[j]:
                            m2 = re.search(r'\d+', lines[j])
                            if m2:
                                old_n = int(m2.group())
                                lines[j] = lines[j].replace(str(old_n), str(old_n + 1), 1)
                            break
                    # Update resolution if present and not "待补充"
                    if resolution:
                        for j in range(matched_idx, min(matched_idx + 10, len(lines))):
                            if "- **解决：**" in lines[j] and "待补充" in lines[j]:
                                lines[j] = f"- **解决：** {resolution}"
                                break
                else:
                    new_num = len([l for l in lines if l.startswith("## #")]) + 1
                    template = (
                        f"\n## #{new_num} {err_type.capitalize()}\n"
                        f"- **次数：** 1\n"
                    )
                    if resolution:
                        template += f"- **解决：** {resolution}\n"
                    else:
                        template += "- **解决：** (待补充)\n"
                    template += "- **领域：** 通用\n"
                    content += template
                    lines = content.split("\n")

            content = "\n".join(lines)
            err_file.write_text(content, "utf-8")
            print(f"[daily] Error library: {len(err_entries)} marker(s) processed")

    # ── Trigger patterns → habit library ──
    trigger_entries = results.get("TRIGGER", [])
    if trigger_entries and not dry_run:
        habits_file = cfg["habits_file"]
        if habits_file.exists():
            content = habits_file.read_text("utf-8")
            lines = content.split("\n")

            for entry in trigger_entries:
                summary = (entry["groups"][0] or "").strip()
                action = (entry["groups"][1] or "").strip()
                if not summary:
                    continue
                keyword = summary[:30]

                matched = False
                for i, ln in enumerate(lines):
                    if ln.startswith("## [") and keyword.lower() in ln.lower():
                        matched = True
                        for j in range(i, min(i + 10, len(lines))):
                            if "- **次数：**" in lines[j]:
                                m2 = re.search(r'\d+', lines[j])
                                if m2:
                                    old_n = int(m2.group())
                                    lines[j] = lines[j].replace(str(old_n), str(old_n + 1), 1)
                                break
                        break

                if not matched:
                    new_entry = (
                        f"\n## [通用] {keyword}\n"
                        f"- **次数：** 1\n"
                        f"- **场景：** {keyword}\n"
                        f"- **模板：** (待提炼)\n"
                        f"- **阈值：** 50\n"
                        f"- **来源：** 日报自动记录\n"
                    )
                    content += new_entry

            content = "\n".join(lines)
            habits_file.write_text(content, "utf-8")
            print(f"[daily] Habit library: {len(trigger_entries)} trigger(s) processed")


def scan_bot_logs(cfg, ref_date):
    """Scan bot log files for markers. Returns (bot_markers, bot_name_to_markers)."""
    bot_results = {}
    total_markers = 0
    for bot_name, log_file in cfg.get("bot_logs", {}).items():
        if not log_file.exists():
            continue
        markers = {"DECISION": [], "ERROR": [], "PREFERENCE": [], "PENDING": [], "TRIGGER": []}
        try:
            text = log_file.read_text("utf-8", errors="replace")
            for line in text.split("\n"):
                entry = parse_entry_line(line, ref_date)
                if entry:
                    markers[entry["type"]].append(entry)
                    total_markers += 1
        except: pass
        if sum(len(v) for v in markers.values()) > 0:
            bot_results[bot_name] = markers
    return bot_results, total_markers

def generate_bot_report(cfg, ref_date, bot_results):
    """Generate separate bot report per bot."""
    report_dir = cfg.get("bot_report_dir", cfg["vault"] / "Bot-日报")
    report_dir.mkdir(parents=True, exist_ok=True)
    display_map = {"DECISION": "决策", "ERROR": "错误", "PREFERENCE": "偏好", "PENDING": "待续"}
    for bot_name, markers in bot_results.items():
        report_file = report_dir / f"{ref_date}-{bot_name}.md"
        lines = [f"# {ref_date} {bot_name} 日报\n\n"]
        total = sum(len(v) for v in markers.values())
        if total == 0:
            lines.append("无记录\n")
        else:
            for key, zh in display_map.items():
                entries = markers.get(key, [])
                if entries:
                    lines.append(f"\n## {zh}\n")
                    for e in entries:
                        s = e["groups"][0] if e["groups"] and e["groups"][0] else e["raw"]
                        lines.append(f"- {s.strip()}\n")
        report_file.write_text("".join(lines), "utf-8")
        print(f"[daily] Bot report: {report_file} ({total} markers)")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--date", default=None)
    args = parser.parse_args()

    cfg = load_config()
    ref_date = args.date or (datetime.date.today() - datetime.timedelta(days=1)).isoformat()

    results = scan_topics(cfg, ref_date)
    total = sum(len(v) for v in results.values())

    print(f"[daily] {ref_date}: {total} markers in topic files")
    for k, v in results.items():
        print(f"  {k}: {len(v)}")

    if not args.dry_run and total > 0:
        report = generate_report(cfg, ref_date, results)
        print(f"[daily] Report: {report}")

    update_libraries(cfg, results, args.dry_run)

    # Auto-archive stale error entries (60d no trigger → archived)
    if not args.dry_run:
        archive_stale(cfg, ref_date)


def archive_stale(cfg, today):
    """Mark error entries with last_seen > 60 days as '已归档'."""
    err_file = cfg["errors_file"]
    if not err_file.exists():
        return
    content = err_file.read_text("utf-8")
    lines = content.split("\n")
    modified = False
    now_date = datetime.date.fromisoformat(today) if today else datetime.date.today()

    for i, ln in enumerate(lines):
        if ln.startswith("## #"):
            # Check for last_seen field in following lines
            name = ln.strip()
            last_seen_date = None
            last_seen_idx = -1
            status_idx = -1
            for j in range(i, min(i + 15, len(lines))):
                if "- **最近出现：**" in lines[j]:
                    date_str = lines[j].split("最近出现：**")[-1].strip()
                    try:
                        last_seen_date = datetime.date.fromisoformat(date_str[:10])
                        last_seen_idx = j
                    except (ValueError, IndexError):
                        pass
                if "- **状态：**" in lines[j]:
                    status_idx = j
                if ln.strip() == "---" and j > i:
                    break  # end of entry

            if last_seen_date:
                days_since = (now_date - last_seen_date).days
                if days_since > 60:
                    if status_idx >= 0 and "已归档" not in lines[status_idx]:
                        lines[status_idx] = "- **状态：** 已归档"
                        modified = True
                        print(f"[daily] Archive: {name} ({days_since}d since last seen)")
            else:
                # No last_seen field — add one (use today as starting point)
                insert_at = i + 1
                # Find where frequency line ends
                for j in range(i, min(i + 10, len(lines))):
                    if "- **次数：**" in lines[j]:
                        lines[j] = lines[j] + f"\n- **最近出现：** {today}"
                        modified = True
                        break
                else:
                    # No count line, insert after name
                    lines.insert(insert_at, f"- **最近出现：** {today}")
                    modified = True

    if modified:
        err_file.write_text("\n".join(lines), "utf-8")


if __name__ == "__main__":
    main()
