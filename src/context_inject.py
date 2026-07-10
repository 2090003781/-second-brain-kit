#!/usr/bin/env python3
"""
context_inject.py — 按需知识检索 + 缓存自检

替代 27KB 全量知识索引的自动注入，改为只返回匹配的少量条目。
被 AGENTS.md 引用为「需要知识时的优先入口」。

用法：
  python context_inject.py search "<query>"         → 检索并返回匹配知识
  python context_inject.py stable                    → 返回稳定知识结构概览
  python context_inject.py cache_check              → 缓存前缀分析
"""

import json, os, sys
from pathlib import Path

INDEXER_PATH = Path.home() / ".reasonix" / "logs" / "knowledge_indexer.py"
INDEX_PATH = Path.home() / ".reasonix" / "knowledge_index.json"
STABLE_INDEX = Path.home() / ".reasonix" / "knowledge" / "INDEX.md"


def search(query, top_k=3, min_score=3):
    """Search knowledge and return only matching items."""
    if not INDEXER_PATH.exists():
        return "<知识索引器未找到>", 0
    if not INDEX_PATH.exists():
        return "<知识索引文件未生成，请先运行 `python ~/.reasonix/logs/knowledge_indexer.py build`>", 0

    # Import dynamically to avoid path issues
    import importlib.util
    spec = importlib.util.spec_from_file_location("ki", str(INDEXER_PATH))
    ki = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ki)

    ctx, n = ki.context_inject(query, top_k=top_k, min_score=min_score)
    return ctx, n


def stable():
    """Return the stable knowledge structure overview (~1KB)."""
    if STABLE_INDEX.exists():
        return STABLE_INDEX.read_text("utf-8")
    return "# 全局知识库\n> 索引文件未找到。\n"


def cache_check():
    """Analyze cache prefix composition."""
    report = []
    report.append("## 缓存前缀分析")

    # Knowledge index size
    if INDEX_PATH.exists():
        kb = INDEX_PATH.stat().st_size
        report.append(f"- `knowledge_index.json`: {kb // 1024}KB {'⚠️ 每天变化，不推荐自动注入' if kb > 5120 else '✅ 较小，影响有限'}")
    else:
        report.append("- `knowledge_index.json`: 未生成")

    # Stable index size
    if STABLE_INDEX.exists():
        kb = STABLE_INDEX.stat().st_size
        report.append(f"- `INDEX.md`（稳定知识索引）: {kb // 1024}KB ✅ 跨会话缓存友好")
    else:
        report.append("- `INDEX.md`: 未找到")

    # Foundation knowledge
    foundation_dir = Path.home() / ".reasonix" / "knowledge" / "foundation"
    if foundation_dir.exists():
        files = list(foundation_dir.glob("*.md"))
        total_words = 0
        for f in files:
            total_words += len(f.read_text("utf-8").split())
        report.append(f"- Foundation 知识: {len(files)} 个文件, ~{total_words} words ✅ 稳定内容")

    # Error library
    vault_errors = Path("D:/个人数据/辞玖/记忆/错误库.md")
    if vault_errors.exists():
        lines = vault_errors.read_text("utf-8").count("\n")
        report.append(f"- 错误库.md: ~{lines} 行 {'⚠️ 偶尔更新' if lines > 50 else '✅ 小文件'}")

    report.append("")
    report.append("**建议：** `knowledge_index.json` 应保持按需读取，不要自动注入到 system prompt。")
    report.append("需要知识时优先调用 `context_inject.py search \"<query>\"`。")

    return "\n".join(report)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "search":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        ctx, n = search(query)
        if ctx:
            sys.stdout.reconfigure(encoding="utf-8")
            print(ctx)
        else:
            print("")
    elif len(sys.argv) >= 2 and sys.argv[1] == "stable":
        sys.stdout.reconfigure(encoding="utf-8")
        print(stable())
    elif len(sys.argv) >= 2 and sys.argv[1] == "cache_check":
        sys.stdout.reconfigure(encoding="utf-8")
        print(cache_check())
    else:
        print("用法:")
        print("  python context_inject.py search \"<query>\"")
        print("  python context_inject.py stable")
        print("  python context_inject.py cache_check")
