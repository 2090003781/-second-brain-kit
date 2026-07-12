"""
vault_fts5.py — Chinese full-text search index for Obsidian vault.

Usage:
    python vault_fts5.py rebuild         # Full rebuild
    python vault_fts5.py rebuild_inc     # Incremental (since last build)
    python vault_fts5.py query <keyword> # Search and rank results
    python vault_fts5.py stats           # Index stats

Dependencies: jieba (pip install jieba)
Storage: ~/.second-brain/fts5/vault.db (derived cache, can be rebuilt)
"""

import argparse, glob, hashlib, json, os, re, sqlite3, time
from pathlib import Path

FTS5_DIR = Path.home() / ".second-brain" / "fts5"
FTS5_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = FTS5_DIR / "vault.db"

# Stopwords (common Chinese characters that add no search value)
STOPWORDS = set("的了在是我有和就不人都一个上也很到说要去会着没有看看好自己这出来它她它们那")  

def get_vault_path():
    """Read vault path from config.toml, env var, or fallback."""
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


def init_db():
    """Create FTS5 tables if they don't exist."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS vault_fts USING fts5(
            title, content, category, path,
            tokenize='unicode61'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_meta (
            path TEXT PRIMARY KEY,
            mtime REAL,
            hash TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS index_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    return conn


def extract_text(filepath, vault_base=None):
    """Read .md file, separate frontmatter from body, extract title."""
    if vault_base is None:
        vault_base = VAULT
    raw = filepath.read_text("utf-8", errors="replace")
    fm = {}
    body = raw
    title = filepath.stem
    
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().split("\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
            body = parts[2]
    
    if "title" in fm:
        title = fm["title"]
    
    # Clean markdown syntax
    body = re.sub(r'\[\[([^\]]+)\]\]', r'\1', body)
    body = re.sub(r'[#*`>|\[\]()]', ' ', body)
    body = re.sub(r'\s+', ' ', body).strip()
    
    category = ""
    rel_path = filepath.relative_to(vault_base).as_posix()
    parts = rel_path.split("/")
    if len(parts) > 1:
        category = parts[0]
    
    return title, body, category, rel_path


def index_file(conn, filepath, vault_base=None):
    """Index a single .md file into FTS5."""
    title, body, category, rel_path = extract_text(filepath, vault_base)
    if not body.strip():
        return False
    
    # Get file hash for change detection
    mtime = os.path.getmtime(filepath)
    file_hash = hashlib.md5(body.encode()).hexdigest()[:16]
    
    # Check if unchanged
    row = conn.execute("SELECT hash FROM file_meta WHERE path=?", (rel_path,)).fetchone()
    if row and row[0] == file_hash:
        return False
    
    # Remove old entry
    conn.execute("DELETE FROM vault_fts WHERE path=?", (rel_path,))
    
    # Insert new entry (FTS5 handles tokenization)
    conn.execute(
        "INSERT INTO vault_fts(title, content, category, path) VALUES (?, ?, ?, ?)",
        (title, body[:10000], category, rel_path)
    )
    
    # Update meta
    conn.execute(
        "INSERT OR REPLACE INTO file_meta(path, mtime, hash) VALUES (?, ?, ?)",
        (rel_path, mtime, file_hash)
    )
    return True


def rebuild(full=True, vault_path=None):
    """Full or incremental rebuild of the FTS5 index."""
    vault = vault_path or get_vault_path()
    conn = init_db()
    
    if full:
        conn.execute("DELETE FROM vault_fts")
        conn.execute("DELETE FROM file_meta")
    
    files = sorted(vault.rglob("*.md"))
    # Skip certain directories
    skip_dirs = {".obsidian", ".trash", "copilot", "node_modules", ".git"}
    files = [f for f in files if not any(d in f.parts for d in skip_dirs)]
    
    count = 0
    start = time.time()
    for f in files:
        if index_file(conn, f, vault):
            count += 1
    
    conn.execute("INSERT OR REPLACE INTO index_meta(key, value) VALUES (?, ?)",
                 ("last_build", str(time.time())))
    conn.commit()
    conn.close()
    
    elapsed = time.time() - start
    print(f"[fts5] {'Full' if full else 'Incremental'} rebuild: {count} new/updated files in {elapsed:.1f}s")


def query(keyword, limit=10, vault_path=None):
    """Search the FTS5 index and return ranked results."""
    vault = vault_path or get_vault_path()
    conn = init_db()
    
    # Check if index exists and has data
    row = conn.execute("SELECT COUNT(*) FROM vault_fts").fetchone()
    if not row or row[0] == 0:
        conn.close()
        print("[fts5] Index empty, run rebuild first")
        return []
    
    # Use jieba for Chinese word segmentation
    import jieba
    words = jieba.lcut(keyword)
    # Remove stopwords
    words = [w for w in words if w.strip() and w not in STOPWORDS and len(w) > 1]
    
    if not words:
        words = [keyword]
    
    # Build FTS5 query (prefix matching for each word)
    fts_query = " AND ".join(f'"{w}"' for w in words[:5])
    
    try:
        cursor = conn.execute(
            f"SELECT title, content, category, path, rank FROM vault_fts WHERE vault_fts MATCH ? ORDER BY rank LIMIT ?",
            (fts_query, limit)
        )
        results = cursor.fetchall()
    except sqlite3.OperationalError:
        # FTS5 query syntax error, fall back to LIKE
        like_query = "%" + keyword.replace("%", "%%") + "%"
        cursor = conn.execute(
            "SELECT title, content, category, path, 0.0 FROM vault_fts WHERE content LIKE ? OR title LIKE ? LIMIT ?",
            (like_query, like_query, limit)
        )
        results = cursor.fetchall()
    
    conn.close()
    return results


def query_cli(keyword, limit=10):
    """CLI wrapper for query."""
    results = query(keyword, limit)
    if not results:
        print("[fts5] No results")
        return
    
    print(f"[fts5] {len(results)} result(s) for '{keyword}':\n")
    for title, content, category, path, rank in results:
        preview = content[:120].replace("\n", " ")
        print(f"  [{category}] {title}")
        print(f"  → {preview}...")
        safe_path = path.encode("ascii", errors="replace").decode()
        print(f"  -> {safe_path} (score: {rank:.2f})\n")


def stats():
    """Print index statistics."""
    vault = get_vault_path()
    conn = init_db()
    total = conn.execute("SELECT COUNT(*) FROM vault_fts").fetchone()[0]
    meta = conn.execute("SELECT value FROM index_meta WHERE key='last_build'").fetchone()
    last_build = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(meta[0]))) if meta else "never"
    files_in_vault = len(list(vault.rglob("*.md")))
    conn.close()
    print(f"[fts5] FTS5 Index Status")
    print(f"  Indexed files: {total}")
    print(f"  Vault .md files: {files_in_vault}")
    print(f"  Coverage: {total/files_in_vault*100:.0f}%")
    print(f"  Last rebuild: {last_build}")
    print(f"  DB size: {DB_PATH.stat().st_size / 1024:.0f} KB")


def main():
    parser = argparse.ArgumentParser(description="Vault Chinese FTS5 search")
    parser.add_argument("action", choices=["rebuild", "rebuild_inc", "query", "stats"])
    parser.add_argument("keyword", nargs="?", default="")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    
    if args.action == "rebuild":
        rebuild(full=True)
    elif args.action == "rebuild_inc":
        rebuild(full=False)
    elif args.action == "query":
        if not args.keyword:
            print("[fts5] Please provide a keyword to search")
            return
        query_cli(args.keyword, args.limit)
    elif args.action == "stats":
        stats()


if __name__ == "__main__":
    main()
