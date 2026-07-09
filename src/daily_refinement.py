"""
daily_refinement.py — Topic-file scanner for structured log format v2.

Reads topic files, extracts structured markers ([DECISION]/[ERROR]/[PREFERENCE]/[PENDING]/[TRIGGER]),
updates error library and habit library, generates daily report.
Usage:
    python src/daily_refinement.py
    python src/daily_refinement.py --dry-run
"""

import argparse, configparser, datetime, json, os, re, sys
from pathlib import Path

def get_vault_path():
    """Read vault path from config.toml, env var, or fallback."""
    env_path = os.environ.get("OBSIDIAN_VAULT")
    if env_path:
        return Path(env_path)
    script_dir = Path(__file__).resolve().parent.parent
    for cfg in [script_dir / "config.toml", Path.home() / ".second-brain" / "config.toml"]:
        if cfg.exists():
            cp = configparser.ConfigParser()
            cp.read(str(cfg))
            try:
                return Path(cp.get("vault", "path"))
            except:
                pass
    return Path("D:/个人数据/辞玖")

def load_config():
    vault = get_vault_path()
    return {
        "vault": vault,
        "errors_file": vault / "记忆" / "错误库.md",
        "habits_file": vault / "记忆" / "习惯库.md",
        "daily_dir": vault / "日报",
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
    """Scan reasonix-raw, supervision log, and JSONL logs for structured markers."""
    import json as _json
    results = {"DECISION": [], "ERROR": [], "PREFERENCE": [], "PENDING": [], "TRIGGER": []}
    
    # 1. Scan reasonix-raw directory (now under 日志/)
    raw_dir = cfg["vault"] / "日志" / "reasonix-raw"
    # Fallback: old location
    if not raw_dir.exists():
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
                    # Also check for raw marker format: 时间 | [DECISION: ...]
                    if "| [" in line:
                        for tag, pat in [("DECISION", DECISION_RE), ("PREFERENCE", PREFERENCE_RE), ("PENDING", PENDING_RE), ("TRIGGER", TRIGGER_RE)]:
                            if "[" + tag in line.upper():
                                m = pat.search(line)
                                if m:
                                    results[tag].append({"type": tag, "groups": m.groups(), "raw": line})
                                    break
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
    
    # 3. Scan supervision log for violations
    sup_log = cfg["vault"] / "日志" / "监督日志.md"
    if not sup_log.exists():
        sup_log = cfg["vault"] / "监督日志.md"
    if sup_log.exists():
        mmdd = ref_date[5:]
        for line in sup_log.read_text("utf-8", errors="replace").split("\n"):
            if mmdd in line and "Tool" in line:
                results["ERROR"].append({"type":"ERROR","groups":(line[:120],None,None,None),"raw":line})
    
    return results


def generate_report(cfg, ref_date, results):
    """Generate clean daily report."""
    report_dir = cfg["daily_dir"]
    report_dir.mkdir(parents=True, exist_ok=True)
    report_file = report_dir / f"{ref_date}.md"
    decisions = results.get("DECISION", [])
    errors = results.get("ERROR", [])
    prefs = results.get("PREFERENCE", [])
    lines = []

    lines.append("# " + ref_date + " \u65e5\u62a5")
    lines.append("")
    lines.append("## \u5de5\u4f5c\u5185\u5bb9")
    has_work = set()
    for d in decisions:
        s = d["groups"][0][:80] if d["groups"] and d["groups"][0] else ""
        if s:
            has_work.add(s)
    for w in sorted(has_work):
        lines.append("- " + w)
    if not has_work:
        lines.append("(\u65e0\u8bb0\u5f55)")
    lines.append("")

    lines.append("## \u8fdb\u5ea6")
    lines.append("- \u5f85\u529e \u2192 [[\u9879\u76ee/\u7b2c\u4e8c\u5927\u8111/\u5f85\u529e\u6e05\u5355.md]]")
    todo_path = cfg["vault"] / "\u9879\u76ee" / "\u7b2c\u4e8c\u5927\u8111" / "\u5f85\u529e\u6e05\u5355.md"
    if todo_path.exists():
        todo_items = []
        for line in todo_path.read_text("utf-8").split("\n"):
            s = line.strip()
            if s.startswith("- [x]") or s.startswith("- [X]"):
                title = s[6:].split("\u2014")[0].strip().strip("*").strip()
                # Extract @start date
                start_m = re.search(r"@start:(\S+)", s)
                start_date = start_m.group(1) if start_m else ""
                todo_items.append(("done", title, start_date, s))
            elif s.startswith("- [ ]") and len(s) > 6:
                title = s[6:].split("\u2014")[0].strip().strip("*").strip()
                start_m = re.search(r"@start:(\S+)", s)
                active_m = re.search(r"@active:(\S+)", s)
                start_date = start_m.group(1) if start_m else ""
                active_date = active_m.group(1) if active_m else ""
                todo_items.append(("pending", title, start_date, active_date, s))
        pending_list = [(t, sd, ad) for item in todo_items if item[0] == "pending" for t, sd, ad in [(item[1], item[2], item[3])]]
        done_list = [(t, sd) for item in todo_items if item[0] == "done" for t, sd in [(item[1], item[2])]]
        # Sort pending by @active desc (most recent first)
        pending_list.sort(key=lambda x: x[2] if x[2] else "", reverse=True)
        for t, sd, ad in pending_list:
            display = "  - [ ] " + t
            if sd:
                display += " (" + sd + ")"
            lines.append(display)
        for t, sd in done_list:
            display = "  - [x] " + t
            lines.append(display)
        if not pending_list and not done_list:
            lines.append("  (\u65e0)")
    lines.append("")

    lines.append("## \u63d0\u70bc")
    if errors:
        lines.append("- \u9519\u8bef: \u63d0\u53d6 " + str(len(errors)) + " \u6761")
    if decisions:
        lines.append("- \u51b3\u7b56: " + str(len(decisions)) + " \u6761")
    if prefs:
        lines.append("- \u4e60\u60ef: \u63d0\u70bc " + str(len(prefs)) + " \u6761")
    if not (errors or decisions or prefs):
        lines.append("(\u65e0\u8bb0\u5f55)")
    lines.append("")

    lines.append("## \u9700\u5ba1\u6279")
    from pathlib import Path as _P
    sup_log = _P.home() / ".reasonix" / "logs" / "supervisor_run.log"
    if sup_log.exists():
        mmdd = ref_date[5:]
        try:
            today_log = [l for l in sup_log.read_text("utf-8", errors="replace").split("\n") if mmdd in l]
        except:
            today_log = []
        downgrades = [l for l in today_log if "downgrading" in l]
        if downgrades:
            seen = set()
            for l in downgrades:
                m = re.search(r"(\S+):\s*fired", l)
                if m and m.group(1) not in seen:
                    seen.add(m.group(1))
                    cnt = sum(1 for x in downgrades if m.group(1) in x)
                    lines.append("- [\u5efa\u8bae] " + m.group(1) + " \u89e6\u53d1\u9891\u7e41(" + mmdd + " \u65e5 " + str(cnt) + " \u6b21)\uff0c\u5747\u5df2\u964d\u7ea7\u672a\u5b9e\u9645\u63a8\u9001")
                    lines.append("  (\u8bc1\u636e: \u89e6\u53d1\u540e\u88ab\u9650\u901f\u964d\u7ea7)")
        if not downgrades:
            lines.append("(\u65e0)")
    else:
        lines.append("(\u65e0)")
    lines.append("")

    content_text = "\n".join(lines) + "\n"
    report_file.write_text(content_text, "utf-8")
    # Write stats JSON
    stats_dir = cfg["vault"] / "\u5468\u62a5" / "stats"
    stats_dir.mkdir(parents=True, exist_ok=True)
    stats = {
        "date": ref_date,
        "errors": len(errors),
        "decisions": len(decisions),
        "preferences": len(prefs),
        "todo_done": sum(1 for s in lines if s.startswith("  - [x]")),
        "todo_pending": sum(1 for s in lines if s.startswith("  - [") and not s.startswith("  - [x")),
        # Read supervisor log for rule stats
        "rules": {}
    }
    sup_log = Path.home() / ".reasonix" / "logs" / "supervisor_run.log"
    if sup_log.exists():
        mmdd = ref_date[5:]
        try:
            today_log = [l for l in sup_log.read_text("utf-8", errors="replace").split("\n") if mmdd in l]
        except:
            today_log = []
        for l in today_log:
            if "downgrading" in l:
                m = re.search(r"(\S+):\s*fired", l)
                if m:
                    r = m.group(1)
                    if r not in stats["rules"]:
                        stats["rules"][r] = 0
                    stats["rules"][r] += 1
    (stats_dir / f"{ref_date}.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), "utf-8")
    return report_file

def update_libraries(cfg, results, dry_run=False):
    """Update error library and habit library from extracted markers."""
    # Error library update disabled: format changed to 方案: instead of 解决:
    return

def scan_bot_logs(cfg, ref_date):
    """Scan bot log files for markers."""
    return {}, 0

def generate_bot_report(cfg, ref_date, bot_results):
    """Generate separate bot report per bot."""
    pass


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

    if not args.dry_run:  # always generate, even with 0 markers
        report = generate_report(cfg, ref_date, results)
        print(f"[daily] Report: {report}")

    update_libraries(cfg, results, args.dry_run)

    # Auto-archive stale error entries (60d no trigger → archived)
    if not args.dry_run:
        archive_stale(cfg, ref_date)
    
    # Update todo list from today's completed decisions
    if not args.dry_run:
        update_todo_list(cfg, results.get("DECISION", []))


def update_todo_list(cfg, decisions):
    """Mark matching todo items as completed from today's decisions."""
    todo_path = cfg["vault"] / "\u9879\u76ee" / "\u7b2c\u4e8c\u5927\u8111" / "\u5f85\u529e\u6e05\u5355.md"
    if not todo_path.exists() or not decisions:
        return
    content = todo_path.read_text("utf-8")
    lines = content.split("\n")
    keywords = ["\u5b8c\u6210", "\u4fee\u590d", "\u89e3\u51b3", "\u90e8\u7f72", "\u5b9e\u73b0"]
    completed = set()
    for d in decisions:
        s = d["groups"][0][:60] if d["groups"] and d["groups"][0] else ""
        if s and any(w in s for w in keywords):
            completed.add(s[:30].lower())
    if not completed:
        return
    modified = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("- [ ]") or stripped.startswith("# [ ]"):
            for c in completed:
                if c in stripped.lower():
                    lines[i] = stripped.replace("[ ]", "[x]", 1)
                    modified = True
                    break
    if modified:
        todo_path.write_text("\n".join(lines), "utf-8")
        print(f"[daily] Todo: updated\n")


def archive_stale(cfg, today):
    """Mark old error entries as archived."""
    pass


if __name__ == "__main__":
    main()
