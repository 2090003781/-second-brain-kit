"""
dedup.py — 内容哈希去重工具
用于知识库、记忆、经验文件的重复检测。
存储哈希索引到 .dedup_cache.json，避免重复写入。
"""

import hashlib
import json
from pathlib import Path
from typing import Optional

CACHE_FILE = ".dedup_cache.json"

def content_hash(text: str) -> str:
    """SHA-256 of normalized text (strip whitespace, lowercase)."""
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

def load_cache(vault_root: Path) -> dict:
    """Load dedup cache from vault root."""
    cache_path = vault_root / CACHE_FILE
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text("utf-8"))
        except:
            return {}
    return {}

def save_cache(vault_root: Path, cache: dict) -> None:
    """Save dedup cache."""
    cache_path = vault_root / CACHE_FILE
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), "utf-8")

def is_duplicate(vault_root: Path, text: str, source: str = "") -> Optional[str]:
    """
    Check if text is a known duplicate.
    Returns existing file path if duplicate, None if new.
    """
    cache = load_cache(vault_root)
    h = content_hash(text)
    
    if h in cache:
        return cache[h]["file"]
    
    # Not duplicate - register it
    cache[h] = {
        "file": source,
        "added": __import__("datetime").datetime.now().isoformat()[:10]
    }
    save_cache(vault_root, cache)
    return None

def bulk_index(vault_root: Path, directory: str, glob_pattern: str = "**/*.md") -> int:
    """
    Index all knowledge/memory files for dedup.
    Returns number of entries indexed.
    """
    cache = load_cache(vault_root)
    count = 0
    target = vault_root / directory
    
    for f in sorted(target.glob(glob_pattern)):
        if not f.is_file():
            continue
        content = f.read_text("utf-8", errors="replace")
        # Index each section/heading as separate entry
        sections = content.split("\n## ")
        for section in sections[:20]:  # Limit per file
            h = content_hash(section[:200])  # First 200 chars
            if h not in cache:
                rel = f.relative_to(vault_root)
                cache[h] = {
                    "file": str(rel),
                    "added": __import__("datetime").datetime.now().isoformat()[:10]
                }
                count += 1
    
    save_cache(vault_root, cache)
    return count

if __name__ == "__main__":
    import sys
    vault = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    count = bulk_index(vault, ".")
    print(f"Indexed {count} new entries")
