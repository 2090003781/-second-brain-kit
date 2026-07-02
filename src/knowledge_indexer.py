#!/usr/bin/env python3
"""Build knowledge index from Vault — v2: better content extraction."""

import json, os, re
from datetime import datetime
from pathlib import Path

VAULT = Path("D:/个人数据/辞玖")
KB_DIR = VAULT / "知识库"
INDEX_PATH = Path.home() / ".reasonix" / "knowledge_index.json"


def strip_bom(text):
    return text[1:] if text.startswith("\ufeff") else text


def extract_frontmatter(text):
    fm = {}
    clean = strip_bom(text)
    if clean.startswith("---"):
        end = clean.find("---", 3)
        if end > 0:
            for line in clean[3:end].strip().split("\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip().lower()] = v.strip()
    return fm


def extract_heading(text):
    clean = strip_bom(text)
    # Skip frontmatter
    start = 0
    if clean.startswith("---"):
        end = clean.find("---", 3)
        if end > 0:
            start = end + 3
    best = ""
    for line in clean[start:].split("\n"):
        line = line.strip()
        if line.startswith("# "):
            return line.lstrip("#").strip()
        if line.startswith("## ") and not best:
            best = line[3:].strip()
        if not line.startswith(">") and not line.startswith("#") and line and not best:
            best = line[:60]
    return best


def extract_tags(fm, text):
    tags = []
    # Obsidian frontmatter: tags: [tag1, tag2] or tags: tag1
    if "tags" in fm:
        raw = fm["tags"].strip()
        # Remove brackets, split by comma
        raw = raw.strip("[]")
        tags.extend(t.strip().strip("\"' ") for t in raw.split(",") if t.strip())
    # Also check for inline #tags in content
    clean = strip_bom(text)
    for m in re.finditer(r'#([\w\u4e00-\u9fff]+)', clean[:500]):
        t = m.group(1)
        if t not in tags and len(t) > 1:
            tags.append(t)
    return list(set(tags))[:15]


def extract_summary(text):
    """Extract meaningful content, skipping frontmatter and metadata lines."""
    clean = strip_bom(text)
    # Skip frontmatter
    start = 0
    if clean.startswith("---"):
        end = clean.find("---", 3)
        if end > 0:
            start = end + 3

    body = clean[start:].strip()
    lines = []
    for line in body.split("\n"):
        line = line.strip()
        # Skip headings, quotes, metadata, empty
        if line.startswith("#"):
            continue
        if line.startswith(">"):
            continue
        if line.startswith("- **"):
            continue
        if not line:
            continue
        lines.append(line)
        if len(" ".join(lines)) > 150:
            break

    summary = " ".join(lines)[:150].strip()
    # Clean markdown artifacts
    summary = re.sub(r'\[\[([^\]]+)\]\]', r'\1', summary)
    summary = re.sub(r'[*_`#]', '', summary)
    return summary if summary else "(无摘要)"


def build_index():
    if not KB_DIR.exists():
        return

    entries = {}
    for md_file in sorted(KB_DIR.rglob("*.md")):
        try:
            text = md_file.read_text("utf-8", errors="replace")
        except Exception:
            continue

        fm = extract_frontmatter(text)
        title = extract_heading(text) or md_file.stem
        tags = extract_tags(fm, text)
        summary = extract_summary(text)
        rel_path = str(md_file.relative_to(VAULT))

        entries[md_file.stem] = {
            "title": title,
            "summary": summary,
            "tags": tags,
            "path": rel_path,
            "project": fm.get("project", "全局"),
        }

    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(
        json.dumps({"updated": datetime.now().isoformat(), "entries": entries},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Indexed {len(entries)} entries -> {INDEX_PATH}")


def search(query, top_k=5):
    """Keyword search with title/summary/tag scoring."""
    if not INDEX_PATH.exists():
        return []
    data = json.loads(INDEX_PATH.read_text("utf-8"))
    entries = data.get("entries", {})

    q_words = query.lower().split()
    scored = []
    for key, e in entries.items():
        title_lower = e["title"].lower()
        summary_lower = e.get("summary", "").lower()
        tags_lower = [t.lower() for t in e.get("tags", [])]

        score = 0
        for w in q_words:
            if w in title_lower:
                score += 10
            if w in summary_lower:
                score += 3
            for t in tags_lower:
                if w in t:
                    score += 5
        if score > 0:
            e["_score"] = score
            scored.append(e)

    scored.sort(key=lambda x: x["_score"], reverse=True)
    return scored[:top_k]


def context_inject(prompt="", top_k=3, min_score=5):
    """
    Active injection: only inject if there's a meaningful match.
    Returns (injection_string, matched_count).
    """
    results = search(prompt, top_k=top_k)
    results = [r for r in results if r.get("_score", 0) >= min_score]

    if not results:
        return "", 0

    lines = ["## 相关知识与经验"]
    for r in results:
        s = r.get("summary", "")[:100]
        t = r.get("title", "?")
        lines.append(f"- [{t}]: {s}")

    return "\n".join(lines), len(results)


if __name__ == "__main__":
    build_index()
    # Quick test
    for q in ["python 编码", "bot 微信", "obsidian 记忆", "supervisor 监督"]:
        ctx, n = context_inject(q, top_k=3, min_score=3)
        if ctx:
            print(f"\n🔍 \"{q}\" ({n} matches, ~{len(ctx)//4} tokens):")
            print(ctx[:200])

