#!/usr/bin/env python3
"""
ingest.py — 知识抓取 & 格式化工具
==================================
接受 URL 或搜索关键词，提取正文并格式化为标准知识库 Markdown。

Usage:
    python ingest.py <url>                          # 抓取单个 URL
    python ingest.py "搜索关键词"                     # 搜索并抓取
    python ingest.py <url> --to "私密/知识库/xxx"     # 写入指定目录
    python ingest.py <url> --dry-run                 # 预览不写入
    echo "some text" | python ingest.py --stdin      # 管道模式

输出格式：YAML frontmatter + Markdown 正文 + ^block-id
"""

import argparse
import datetime
import json
import os
import re
import sys
import textwrap
import urllib.parse
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Tag

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

HEADERS = {"User-Agent": USER_AGENT}

DEFAULT_VAULT = Path("D:/个人数据/辞玖/私密/知识库")

# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


def extract_readability(soup: BeautifulSoup) -> tuple[str, str, str]:
    """
    Simple readability-style extraction.
    Returns (title, summary, body_text).
    """
    # Title
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    if not title:
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            title = og_title["content"]
    if not title:
        title = soup.title.get_text(strip=True) if soup.title else ""

    # Summary / description
    summary = ""
    meta_desc = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", property="og:description"
    )
    if meta_desc and meta_desc.get("content"):
        summary = meta_desc["content"].strip()

    # Try <article> first, then <main>, then <body>
    content_tag: Tag | None = soup.find("article")
    if not content_tag:
        content_tag = soup.find("main")
    if not content_tag:
        content_tag = soup.find("body")

    if not content_tag:
        return title, summary, ""

    # Remove unwanted elements
    for tag in content_tag.find_all(["script", "style", "nav", "footer", "aside", "header"]):
        tag.decompose()

    # Extract text
    parts = []
    for elem in content_tag.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre", "blockquote"]):
        text = elem.get_text(strip=True)
        if not text:
            continue
        if elem.name.startswith("h") and elem.name != "h1":
            level = int(elem.name[1])
            parts.append(f"{'#' * level} {text}\n")
        elif elem.name == "li":
            parts.append(f"- {text}")
        elif elem.name == "pre":
            parts.append(f"```\n{text}\n```\n")
        elif elem.name == "blockquote":
            parts.append(f"> {text}\n")
        else:
            parts.append(text)

    body = "\n\n".join(parts)
    # Collapse excessive blank lines
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    return title, summary, body


def fetch_url(url: str) -> tuple[str, str, str, str]:
    """Fetch a URL, return (url, title, summary, body_text)."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    # Try to detect encoding
    if resp.encoding and resp.encoding.lower() != "utf-8":
        resp.encoding = resp.apparent_encoding or "utf-8"
    html = resp.text
    soup = BeautifulSoup(html, "lxml")
    title, summary, body = extract_readability(soup)
    return resp.url, title, summary, body


# ---------------------------------------------------------------------------
# DuckDuckGo search (lite HTML version, no API key needed)
# ---------------------------------------------------------------------------

DDG_URL = "https://html.duckduckgo.com/html/"


def search_duckduckgo(query: str, max_results: int = 5) -> list[dict]:
    """Search DuckDuckGo and return list of {title, url, snippet}."""
    params = {"q": query}
    try:
        resp = requests.post(DDG_URL, data=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"[ingest] DuckDuckGo search failed: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    results = []
    for a in soup.select("a.result__a"):
        title = a.get_text(strip=True)
        href = a.get("href", "")
        # DuckDuckGo wraps redirect URLs
        if "//duckduckgo.com/l/?uddg=" in href:
            parsed = urllib.parse.urlparse(href)
            qs = urllib.parse.parse_qs(parsed.query)
            href = qs.get("uddg", [""])[0]
        if not href.startswith("http"):
            continue
        # Find sibling snippet
        snippet = ""
        parent = a.find_parent()
        if parent:
            snip_tag = parent.select_one(".result__snippet")
            if snip_tag:
                snippet = snip_tag.get_text(strip=True)
        results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= max_results:
            break

    return results


# ---------------------------------------------------------------------------
# Markdown formatting
# ---------------------------------------------------------------------------


def _safe_filename(text: str) -> str:
    """Turn text into a safe filename fragment."""
    text = re.sub(r'[\\/:*?"<>|]', "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        text = "untitled"
    if len(text) > 80:
        text = text[:80]
    return text


def _block_id() -> str:
    """Generate a short unique block-id like ^abc12345."""
    import secrets
    return "^" + secrets.token_hex(4)


def _wikilink(path: str) -> str:
    """Convert file path to Obsidian wikilink."""
    # Remove extension
    path = re.sub(r"\.md$", "", path)
    return f"[[{path}]]"


def format_entry(
    title: str,
    body: str,
    source_url: str,
    summary: str = "",
    tags: list[str] | None = None,
    subdir: str = "",
) -> str:
    """
    Format extracted content into a full Markdown note with YAML frontmatter.

    Returns the Markdown string.
    """
    now = datetime.datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    datetime_str = now.strftime("%Y-%m-%d %H:%M")

    safe_title = title or "Untitled"
    safe_title_short = _safe_filename(safe_title)

    # Tags
    all_tags = tags or []
    all_tags.append("ingested")
    if subdir:
        all_tags.append(subdir.replace("/", "/"))
    all_tags = list(dict.fromkeys(all_tags))  # dedup, preserve order

    block_id = _block_id()

    lines = []
    # Frontmatter
    lines.append("---")
    lines.append(f"created: {date_str}")
    lines.append(f"source: {source_url}")
    lines.append(f"title: {safe_title}")
    if summary:
        lines.append(f"description: >")
        # Wrap long summary
        wrapped = textwrap.fill(summary, width=72, subsequent_indent="  ")
        lines.append(f"  {wrapped}")
    lines.append(f"tags: {json.dumps(all_tags, ensure_ascii=False)}")
    lines.append("---")
    lines.append("")

    # Title
    lines.append(f"# {safe_title}")
    if source_url:
        lines.append(f"")
        lines.append(f"> **来源**: [{source_url}]({source_url})")
    if summary:
        lines.append(f"> {summary}")
    lines.append("")
    lines.append(f"*抓取时间: {datetime_str}*")
    lines.append("")

    # Body
    if body:
        lines.append(body)
        lines.append("")

    # Block ID
    lines.append("")
    lines.append(block_id)
    lines.append("")

    # Related wikilinks (based on tags/dir)
    lines.append("---")
    lines.append("")
    lines.append("## 关联")
    lines.append("")
    if subdir:
        linked = _wikilink(subdir.replace("\\", "/").lstrip("/"))
        lines.append(f"- 所属目录: {linked}")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# File output
# ---------------------------------------------------------------------------


def write_entry(
    content: str,
    title: str,
    output_dir: Path,
    dry_run: bool = False,
) -> Path | None:
    """Write the entry to a file under output_dir. Returns the file path or None."""
    safe = _safe_filename(title)
    if not safe:
        safe = "untitled"
    filename = f"{safe}.md"
    filepath = output_dir / filename

    # Deduplicate: if file exists, append a number
    counter = 1
    while filepath.exists() and not dry_run:
        stem = filepath.stem
        # Remove existing counter suffix if any
        parent = filepath.parent
        filepath = parent / f"{stem}_{counter}.md"
        counter += 1

    if dry_run:
        print(f"[ingest] DRY-RUN: would write to {filepath}")
        print("─" * 50)
        print(content)
        print("─" * 50)
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    filepath.write_text(content, encoding="utf-8")
    print(f"[ingest] ✓ Written: {filepath}", file=sys.stderr)
    return filepath


# ---------------------------------------------------------------------------
# Stdin / pipe mode
# ---------------------------------------------------------------------------


def process_stdin(text: str, output_dir: Path, dry_run: bool = False):
    """Format raw text from stdin as a knowledge entry."""
    lines = text.strip().splitlines()
    # First line as title
    title = lines[0].strip().strip("#").strip() if lines else "Pipe Input"
    body = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
    content = format_entry(
        title=title,
        body=body,
        source_url="stdin",
        summary="",
        tags=["stdin", "ingested"],
        subdir="",
    )
    write_entry(content, title, output_dir, dry_run)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="ingest — 知识抓取 & 格式化工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            示例:
              python ingest.py https://example.com/article
              python ingest.py "Ren'Py 视觉小说开发" --to "成人游戏开发"
              python ingest.py https://example.com --dry-run
              echo "一些笔记内容" | python ingest.py --stdin
        """),
    )
    parser.add_argument("input", nargs="?", help="URL 或搜索关键词")
    parser.add_argument("--to", "-t", default="", help="写入的子目录（如 '成人游戏开发'）")
    parser.add_argument("--dry-run", "-n", action="store_true", help="预览模式，不写入文件")
    parser.add_argument("--stdin", action="store_true", help="从 stdin 读取内容")
    parser.add_argument("--vault", default=str(DEFAULT_VAULT), help="Obsidian vault 路径")
    parser.add_argument("--max-results", type=int, default=5, help="搜索返回最大结果数 (默认 5)")
    parser.add_argument("--tag", action="append", default=[], help="额外标签（可多次）")

    args = parser.parse_args()

    # Determine output directory
    vault = Path(args.vault)
    subdir = args.to.strip().strip("/").replace("\\", "/")
    output_dir = vault / subdir if subdir else vault

    # Stdin mode
    if args.stdin or (args.input is None and not sys.stdin.isatty()):
        text = sys.stdin.read()
        if text.strip():
            process_stdin(text, output_dir, args.dry_run)
        else:
            print("[ingest] No stdin input received.", file=sys.stderr)
        return

    if not args.input:
        parser.print_help()
        sys.exit(1)

    input_text = args.input.strip()

    # Detect if input is a URL
    is_url = input_text.startswith(("http://", "https://"))

    if is_url:
        # Fetch single URL
        print(f"[ingest] Fetching URL: {input_text}", file=sys.stderr)
        try:
            url, title, summary, body = fetch_url(input_text)
            content = format_entry(
                title=title or "Untitled",
                body=body,
                source_url=url,
                summary=summary,
                tags=args.tag,
                subdir=subdir,
            )
            write_entry(content, title or "article", output_dir, args.dry_run)
        except Exception as e:
            print(f"[ingest] ✗ Error fetching URL: {e}", file=sys.stderr)
            sys.exit(1)

    else:
        # Search mode
        print(f"[ingest] Searching for: {input_text}", file=sys.stderr)
        results = search_duckduckgo(input_text, args.max_results)
        if not results:
            print("[ingest] No search results found.", file=sys.stderr)
            # Fallback: create an entry with the keyword as title
            content = format_entry(
                title=input_text,
                body=f"*搜索关键词，未找到结果*\n\n> 尝试手动搜索此主题。",
                source_url="",
                summary="",
                tags=args.tag + ["search"],
                subdir=subdir,
            )
            write_entry(content, input_text, output_dir, args.dry_run)
            return

        # Fetch top result fully (others as links)
        first = results[0]
        print(f"[ingest] Top result: {first['title']} — {first['url']}", file=sys.stderr)
        try:
            url, title, summary, body = fetch_url(first["url"])
            content = format_entry(
                title=title or first["title"],
                body=body,
                source_url=url,
                summary=summary or first.get("snippet", ""),
                tags=args.tag,
                subdir=subdir,
            )
        except Exception as e:
            print(f"[ingest] Warning: could not fetch top result: {e}", file=sys.stderr)
            # Use snippet only
            content = format_entry(
                title=first["title"],
                body=first.get("snippet", ""),
                source_url=first["url"],
                summary="",
                tags=args.tag + ["search"],
                subdir=subdir,
            )
        write_entry(content, title or first["title"], output_dir, args.dry_run)

        # Append additional search results as a related section
        if len(results) > 1:
            extra_path = output_dir / f"{_safe_filename(title or first['title'])}_related.md"
            extra_lines = [
                "---",
                f"created: {datetime.datetime.now().strftime('%Y-%m-%d')}",
                "tags: [ingested, search/related]",
                "---",
                "",
                f"# 相关资源: {input_text}",
                "",
            ]
            for r in results[1:]:
                extra_lines.append(f"- [{r['title']}]({r['url']})")
                if r.get("snippet"):
                    extra_lines.append(f"  - {r['snippet']}")
            extra_lines.append("")
            if not args.dry_run:
                extra_lines.append("^related")
                output_dir.mkdir(parents=True, exist_ok=True)
                extra_path.write_text("\n".join(extra_lines), encoding="utf-8")
                print(f"[ingest] ✓ Related links: {extra_path}", file=sys.stderr)
            else:
                print("[ingest] DRY-RUN: related links preview omitted", file=sys.stderr)


if __name__ == "__main__":
    main()
