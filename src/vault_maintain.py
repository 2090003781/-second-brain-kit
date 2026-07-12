"""
vault_maintain.py — Maintenance enhancements for Obsidian vault.

Adds 3 steps to the daily pipeline (建议模式, no auto-execution):
  1. category_scan — flag files without frontmatter category
  2. conflict_scan — FTS5 near-duplicate detection  
  3. stale_report — flag files unmodified for 90 days

Usage:
    python vault_maintain.py all          # Run all 3 steps
    python vault_maintain.py categories   # Step 1 only
    python vault_maintain.py conflicts    # Step 2 only
    python vault_maintain.py stale        # Step 3 only
"""

import argparse, datetime, json, os, re, sqlite3, time
from pathlib import Path
from collections import defaultdict

REVIEW_DIR = Path.home() / ".second-brain" / "review"
REVIEW_DIR.mkdir(parents=True, exist_ok=True)


def get_vault_path():
    env_path = os.environ.get("OBSIDIAN_VAULT")
    if env_path:
        return Path(env_path)
    script_dir = Path(__file__).resolve().parent.parent
    for cfg in [script_dir / "config.toml", Path.home() / ".second-brain" / "config.toml"]:
        if cfg.exists():
            try:
                import configparser
                cp = configparser.ConfigParser()
                cp.read(str(cfg))
                return Path(cp.get("vault", "path"))
            except:
                pass
    return Path("D:/个人数据/辞玖")


def scan_categories(vault_path=None):
    """Scan vault for .md files without frontmatter category. Suggest mode only."""
    vault = vault_path or get_vault_path()
    
    files = sorted(vault.rglob("*.md"))
    skip_dirs = {".obsidian", ".trash", "copilot", "node_modules", ".git"}
    files = [f for f in files if not any(d in f.parts for d in skip_dirs)]
    
    uncategorized = []
    for f in files:
        raw = f.read_text("utf-8", errors="replace")
        has_category = False
        if raw.startswith("---"):
            for line in raw.split("---", 2)[1].split("\n"):
                if line.strip().startswith("category:") or line.strip().startswith("type:"):
                    has_category = True
                    break
        if not has_category:
            rel_path = f.relative_to(vault).as_posix()
            uncategorized.append(rel_path)
    
    # Write to 记忆/待分类候选.md
    vault_path_parent = vault
    candidate_file = vault_path_parent / "记忆" / "待分类候选.md"
    if uncategorized:
        content = f"# 待分类候选\n\n> 由 vault_maintain.py categories 生成 ({datetime.date.today()})\n> 建议模式，确认后手动添加 category 到 frontmatter\n\n"
        for path in uncategorized[:30]:
            content += f"- [ ] {path}\n"
        if len(uncategorized) > 30:
            content += f"\n... 及 {len(uncategorized) - 30} 个其他文件\n"
        candidate_file.write_text(content, "utf-8")
    
    print(f"[maintain] Categories: {len(uncategorized)} files without category -> {candidate_file.name}")
    return uncategorized


def scan_conflicts(vault_path=None):
    """Use FTS5 index to find near-duplicate content. Suggest mode only."""
    vault = vault_path or get_vault_path()
    fts5_db = Path.home() / ".second-brain" / "fts5" / "vault.db"
    if not fts5_db.exists():
        print("[maintain] FTS5 index not found, run vault_fts5.py rebuild first")
        return []
    
    conn = sqlite3.connect(str(fts5_db))
    cursor = conn.execute("SELECT path, content FROM vault_fts")
    rows = cursor.fetchall()
    conn.close()
    
    # Simple candidate detection: same path prefix, similar titles
    conflicts = []
    seen = defaultdict(list)
    for path, content in rows:
        prefix = "/".join(path.split("/")[:2])
        seen[prefix].append((path, content[:200]))
    
    for prefix, items in seen.items():
        if len(items) < 2:
            continue
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                # Quick overlap check
                words_i = set(items[i][1][:100].split())
                words_j = set(items[j][1][:100].split())
                if len(words_i) > 5 and len(words_j) > 5:
                    overlap = len(words_i & words_j) / max(len(words_i), len(words_j))
                    if overlap > 0.85:
                        conflicts.append((items[i][0], items[j][0], round(overlap, 2)))
    
    # Write to 记忆/待仲裁.md
    vault_path_parent = vault
    arb_file = vault_path_parent / "记忆" / "待仲裁.md"
    if conflicts:
        content = f"# 待仲裁\n\n> 由 vault_maintain.py conflicts 生成 ({datetime.date.today()})\n> 建议模式，确认后手动合并\n\n"
        for a, b, score in conflicts[:20]:
            content += f"- [ ] 相似度 {score:.0%}: {a} ↔ {b}\n"
        arb_file.write_text(content, "utf-8")
    
    print(f"[maintain] Conflicts: {len(conflicts)} candidate pairs -> {arb_file.name}")
    return conflicts


def scan_stale(vault_path=None):
    """Find .md files not modified in 90 days. Suggest mode only."""
    vault = vault_path or get_vault_path()
    cutoff = time.time() - 90 * 86400
    
    files = sorted(vault.rglob("*.md"))
    skip_dirs = {".obsidian", ".trash", "copilot", "node_modules", ".git"}
    stale = []
    for f in files:
        if any(d in f.parts for d in skip_dirs):
            continue
        if os.path.getmtime(f) < cutoff and os.path.getsize(f) > 100:
            rel_path = f.relative_to(vault).as_posix()
            days_ago = int((time.time() - os.path.getmtime(f)) / 86400)
            stale.append((rel_path, days_ago))
    
    stale.sort(key=lambda x: -x[1])
    print(f"[maintain] Stale: {len(stale)} files unmodified >90 days")
    for path, days in stale[:10]:
        print(f"  {days}d: {path}")
    if stale:
        print(f"  ... and {len(stale)-10} more" if len(stale) > 10 else "")
    return stale


def all_steps(vault_path=None):
    """Run all 3 maintenance steps."""
    vault = vault_path or get_vault_path()
    print("=" * 50)
    print("vault_maintain.py — All Steps")
    print("=" * 50)
    scan_categories(vault)
    print()
    scan_conflicts(vault)
    print()
    scan_stale(vault)
    print("=" * 50)
    print("Done. Review suggestions at 记忆/待分类候选.md & 记忆/待仲裁.md")


def main():
    parser = argparse.ArgumentParser(description="Vault maintenance enhancements")
    parser.add_argument("action", choices=["all", "categories", "conflicts", "stale"])
    args = parser.parse_args()
    
    if args.action == "all":
        all_steps()
    elif args.action == "categories":
        scan_categories()
    elif args.action == "conflicts":
        scan_conflicts()
    elif args.action == "stale":
        scan_stale()


if __name__ == "__main__":
    main()
