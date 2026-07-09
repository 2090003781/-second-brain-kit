"""
weekly_report.py — Weekly summary generator.

Generates weekly reports from daily reports, supervisor logs, error library, and todo list.
Usage:
    python src/weekly_report.py                    # Last week
    python src/weekly_report.py --week 2026-W28    # Specific week
"""

import datetime, json, re, sys
from pathlib import Path

def _get_vault():
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

VAULT = _get_vault()
DAILY_DIR = VAULT / "日报"
WEEKLY_DIR = VAULT / "周报"

def get_week_range(week_str):
    """Convert '2026-W28' to (start_date, end_date)."""
    year = int(week_str[:4])
    week = int(week_str[6:])
    # ISO week: week 1 starts on the Monday of the first Thursday
    jan4 = datetime.date(year, 1, 4)
    start_of_week1 = jan4 - datetime.timedelta(days=jan4.isoweekday() - 1)
    monday = start_of_week1 + datetime.timedelta(weeks=week - 1)
    sunday = monday + datetime.timedelta(days=6)
    return monday, sunday

def collect_daily_reports(monday, sunday):
    """Collect all daily reports in the week range."""
    data = []
    d = monday
    while d <= sunday:
        date_str = d.isoformat()
        report = DAILY_DIR / f"{date_str}.md"
        if report.exists():
            data.append({"date": date_str, "content": report.read_text("utf-8", errors="replace")})
        d += datetime.timedelta(days=1)
    return data

def count_errors_from_logs(monday, sunday):
    """Count rule trigger stats from supervisor run log."""
    sup_log = Path.home() / ".reasonix" / "logs" / "supervisor_run.log"
    if not sup_log.exists():
        return {}, 0
    
    days = []
    d = monday
    while d <= sunday:
        days.append(d.strftime("%m-%d"))
        d += datetime.timedelta(days=1)
    
    # Rules we track
    rule_stats = {}  # rule_name -> {fired, effective}
    try:
        for line in sup_log.read_text("utf-8", errors="replace").split("\n"):
            if not any(day in line for day in days):
                continue
            
            # Count downgrades (fired but rate-limited = ineffective)
            if "downgrading" in line:
                m = re.search(r"(\S+):\s*fired", line)
                if m:
                    rule = m.group(1)
                    if rule not in rule_stats:
                        rule_stats[rule] = {"fired": 0, "effective": 0}
                    rule_stats[rule]["fired"] += 1
            
            # Count succeeded (cancelled after self-correct = AI listened)
            if "succeeded, cancelling" in line:
                m = re.search(r"delayed:\s*(\S+)", line)
                if m:
                    rule = m.group(1)
                    if rule not in rule_stats:
                        rule_stats[rule] = {"fired": 0, "effective": 0}
                    rule_stats[rule]["effective"] += 1
    except:
        pass
    
    return rule_stats, sum(s["fired"] for s in rule_stats.values())

def count_error_trends():
    """Read error library for current counts."""
    err_file = VAULT / "记忆" / "错误库.md"
    if not err_file.exists():
        return []
    
    errors = []
    current = None
    for line in err_file.read_text("utf-8", errors="replace").split("\n"):
        s = line.strip()
        if s.startswith("## #"):
            if current and current["count"] > 0:
                errors.append(current)
            current = {"name": s[5:].strip(), "count": 0, "solution": ""}
        elif current and s.startswith("- **关键词："):
            current["name"] = s.split("：")[-1].split(",")[0].strip().strip("**") if "：" in s else current["name"]
        elif current and s.startswith("- **方案："):
            current["solution"] = s.split("方案：")[-1].strip().strip("**")[:60] if "方案：" in s else ""
    if current and current["count"] > 0:
        errors.append(current)
    return errors[:5]

def get_todo_summary():
    """Get todo stats and items."""
    todo_file = VAULT / "项目" / "第二大脑" / "待办清单.md"
    if not todo_file.exists():
        return {"done": 0, "pending": 0, "items": []}
    
    done = 0
    pending = 0
    items = []
    for line in todo_file.read_text("utf-8", errors="replace").split("\n"):
        s = line.strip()
        if s.startswith("- [x]") or s.startswith("- [X]"):
            done += 1
            # Extract clean title
            title = re.sub(r"\s*@\w+:\S+", "", s[6:]).strip().rstrip("*").strip()
            items.append(("done", title))
        elif s.startswith("- [ ]") and len(s) > 6:
            pending += 1
            title = re.sub(r"\s*@\w+:\S+", "", s[6:]).strip().rstrip("*").strip()
            title = title.split("\u2014")[0].strip()
            items.append(("pending", title))
    
    return {"done": done, "pending": pending, "items": items}

def generate_weekly_report(monday, sunday, week_str):
    """Generate the weekly report."""
    WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
    report_file = WEEKLY_DIR / f"{week_str}.md"
    
    lines = []
    lines.append(f"# {week_str} \u5468\u62a5\uff08{monday}\uff0d{sunday}\uff09")
    lines.append("")
    
    # 1. Work summary from daily reports
    lines.append("## \u672c\u5468\u5de5\u4f5c")
    reports = collect_daily_reports(monday, sunday)
    if reports:
        # Extract work items from daily reports
        for r in reports:
            for line in r["content"].split("\n"):
                s = line.strip()
                if s.startswith("- [ ]"):
                    lines.append(f"- {s[5:]} \u2192 [[\u7cfb\u7edf\u8bbe\u8ba1/\u5f85\u529e\u6e05\u5355.md]]")
    else:
        lines.append("(\u65e0\u8bb0\u5f55)")
    lines.append("")
    
    # 2. Error trends
    lines.append("## \u9519\u8bef\u8d8b\u52bf")
    errors = count_error_trends()
    if errors:
        lines.append("| \u9519\u8bef | \u7d2f\u8ba1 |")
        lines.append("|------|:----:|")
        for e in errors:
            lines.append(f"| {e['name'][:40]} | {e['count']} |")
    else:
        lines.append("(\u65e0)")
    lines.append("")
    
    # 3. Rule stats
    lines.append("## \u89c4\u5219\u8fd0\u884c")
    rule_stats, total = count_errors_from_logs(monday, sunday)
    if rule_stats:
        lines.append("| \u89c4\u5219 | \u89e6\u53d1 |")
        lines.append("|------|:----:|")
        for rule, stats in sorted(rule_stats.items(), key=lambda x: -x[1]["fired"]):
            lines.append(f"| {rule} | {stats['fired']} |")
    else:
        lines.append("(\u65e0\u89e6\u53d1)")
    lines.append("")
    
    # 4. Todo progress
    lines.append("## \u5f85\u529e\u8fdb\u5ea6")
    todo = get_todo_summary()
    lines.append(f"- \u603b\u4f53\uff1a\u5df2\u5b8c\u6210 {todo['done']} \u9879\uff0c\u5269\u4f59 {todo['pending']} \u9879 \u2192 [[\u7cfb\u7edf\u8bbe\u8ba1/\u5f85\u529e\u6e05\u5355.md]]")
    for status, title in todo["items"]:
        if status == "done":
            lines.append(f"  - \u2714 {title}")
    if not todo["items"]:
        lines.append("  (\u65e0)")
    lines.append("")
    

    # 5b. Supervision log violation counts
    lines.append("## \u76d1\u7763\u7edf\u8ba1")
    sup_log = VAULT / "\u65e5\u5fd7" / "\u76d1\u7763\u65e5\u5fd7.md"
    if sup_log.exists():
        v_count = 0
        v_tools = set()
        for line in sup_log.read_text("utf-8", errors="replace").split("\n"):
            for d in range((sunday - monday).days + 1):
                day = (monday + datetime.timedelta(days=d)).strftime("%m-%d")
                if day in line and "Tool" in line:
                    v_count += 1
                    m = re.search(r"Tool: `(.+?)`", line)
                    if m: v_tools.add(m.group(1))
        if v_count > 0:
            lines.append(f"- \u672c\u5468\u8fdd\u89c4: {v_count} \u6b21")
            if v_tools: lines.append(f"- \u6d89\u53ca\u5de5\u5177: {', '.join(sorted(v_tools))}")
        else:
            lines.append("(\u65e0\u8fdd\u89c4)")
    lines.append("")
    
        # 5. Knowledge integration
    lines.append("## \u77e5\u8bc6\u6574\u5408")
    # Count daily stats from JSON
    stats_dir = VAULT / "\u5468\u62a5" / "stats"
    total_new = 0
    if stats_dir.exists():
        for f in sorted(stats_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text("utf-8"))
                date_str = data.get("date", "")
                if monday.isoformat() <= date_str <= sunday.isoformat():
                    total_new += 1
            except:
                pass
    lines.append(f"- \u65b0\u589e: {total_new} \u6761")
    lines.append("- \u5df2\u5ba1\u6838\u94fe\u63a5: (\u5f85\u5ba1\u6838)")
    lines.append("")
    
    # 6. Next week plan
    lines.append("## \u4e0b\u5468\u8ba1\u5212")
    lines.append("(\u7531 AI \u6839\u636e\u5f53\u524d\u8fdb\u5ea6\u81ea\u52a8\u751f\u6210)")
    lines.append("")
    
    report_file.write_text("\n".join(lines), "utf-8")
    return report_file

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--week", default=None)
    args = parser.parse_args()
    
    today = datetime.date.today()
    if args.week:
        week_str = args.week
    else:
        # Last ISO week
        if today.weekday() == 0:
            last_monday = today - datetime.timedelta(days=7)
        else:
            last_monday = today - datetime.timedelta(days=today.weekday() + 7)
        iso_year, iso_week, _ = last_monday.isocalendar()
        week_str = f"{iso_year}-W{iso_week:02d}"
    
    monday, sunday = get_week_range(week_str)
    report = generate_weekly_report(monday, sunday, week_str)
    print(f"[weekly] Report: {report}")

if __name__ == "__main__":
    main()
