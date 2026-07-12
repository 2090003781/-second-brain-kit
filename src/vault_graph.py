"""
vault_graph.py — Knowledge graph engine for Obsidian vault.

Extracts [[wikilink]] relationships, builds a graph,
supports 1-2 hop diffusion search and PageRank hub detection.

Usage:
    python vault_graph.py build        # Build graph from vault
    python vault_graph.py search <keyword>  # 1-2 hop diffusion
    python vault_graph.py hubs         # Top hub notes by PageRank
    python vault_graph.py stats        # Graph statistics

Storage: ~/.second-brain/graph/knowledge_graph.json (派生缓存)
"""

import argparse, json, os, re
from pathlib import Path
from collections import defaultdict

GRAPH_DIR = Path.home() / ".second-brain" / "graph"
GRAPH_DIR.mkdir(parents=True, exist_ok=True)
GRAPH_PATH = GRAPH_DIR / "knowledge_graph.json"


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


def extract_wikilinks(text):
    """Extract all [[wikilink]] targets from text."""
    return re.findall(r'\[\[([^\]]+)\]\]', text)


def extract_title(filepath):
    """Extract title from frontmatter or filename."""
    raw = filepath.read_text("utf-8", errors="replace")
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().split("\n"):
                if line.startswith("title:"):
                    return line.split(":", 1)[1].strip()
    return filepath.stem


def build(vault_path=None):
    """Build the knowledge graph from vault wikilinks."""
    vault = vault_path or get_vault_path()
    
    nodes = {}  # path -> {title, category, outgoing, incoming}
    all_wikilinks = defaultdict(list)  # source -> [targets]
    
    files = sorted(vault.rglob("*.md"))
    skip_dirs = {".obsidian", ".trash", "copilot", "node_modules", ".git"}
    files = [f for f in files if not any(d in f.parts for d in skip_dirs)]
    
    for f in files:
        rel_path = f.relative_to(vault).as_posix()
        title = extract_title(f)
        category = rel_path.split("/")[0] if "/" in rel_path else ""
        
        text = f.read_text("utf-8", errors="replace")
        links = extract_wikilinks(text)
        # Resolve wikilinks: handle [[Note|Alias]] → Note
        resolved = []
        for link in links:
            target = link.split("|")[0].split("#")[0].strip()
            if target:
                resolved.append(target)
        
        nodes[rel_path] = {
            "title": title,
            "category": category,
            "outgoing": resolved,
        }
        for target in resolved:
            all_wikilinks[rel_path].append(target)
    
    # Build adjacency and compute incoming links
    graph = {"nodes": {}, "edges": []}
    for src, targets in all_wikilinks.items():
        if src not in graph["nodes"]:
            graph["nodes"][src] = {"title": nodes[src]["title"], "category": nodes[src]["category"], "incoming": 0}
        for tgt in targets:
            graph["edges"].append({"source": src, "target": tgt})
            # Find the actual file for the target
            tgt_path = resolve_target(tgt, vault)
            if tgt_path:
                tgt_rel = tgt_path.relative_to(vault).as_posix()
                if tgt_rel not in graph["nodes"]:
                    graph["nodes"][tgt_rel] = {"title": nodes.get(tgt_rel, {}).get("title", tgt), "category": nodes.get(tgt_rel, {}).get("category", ""), "incoming": 0}
                graph["nodes"][tgt_rel]["incoming"] = graph["nodes"][tgt_rel].get("incoming", 0) + 1
    
    # Compute simple PageRank
    pagerank = compute_pagerank(graph)
    for node_id, pr in pagerank.items():
        if node_id in graph["nodes"]:
            graph["nodes"][node_id]["pagerank"] = round(pr, 4)
    
    # Add orphan count
    orphans = sum(1 for n in graph["nodes"].values() if n.get("incoming", 0) == 0)
    graph["meta"] = {"total_nodes": len(graph["nodes"]), "total_edges": len(graph["edges"]), "orphans": orphans}
    
    GRAPH_PATH.write_text(json.dumps(graph, ensure_ascii=False, indent=2), "utf-8")
    print(f"[graph] Built: {graph['meta']['total_nodes']} nodes, {graph['meta']['total_edges']} edges, {orphans} orphans")
    return graph


def resolve_target(target, vault):
    """Resolve a [[wikilink]] target to an actual file path."""
    target = target.replace("\\", "/")
    # Try exact match first
    candidates = list(vault.rglob(f"**/{target}.md"))
    if candidates:
        return candidates[0]
    # Try case-insensitive
    target_lower = target.lower()
    for f in vault.rglob("*.md"):
        if target_lower == f.stem.lower():
            return f
    return None


def compute_pagerank(graph, iterations=20, damping=0.85):
    """Simple PageRank computation."""
    nodes = list(graph["nodes"].keys())
    n = len(nodes)
    if n == 0:
        return {}
    pr = {node: 1.0 / n for node in nodes}
    out_degree = defaultdict(int)
    adj = defaultdict(list)
    
    for edge in graph["edges"]:
        s, t = edge["source"], edge["target"]
        adj[s].append(t)
        out_degree[s] += 1
    
    for _ in range(iterations):
        new_pr = {}
        for node in nodes:
            rank = (1 - damping) / n
            for src in nodes:
                if node in [e["target"] for e in graph["edges"] if e["source"] == src]:
                    rank += damping * pr[src] / max(out_degree[src], 1)
            new_pr[node] = rank
        pr = new_pr
    
    return pr


def search(keyword, vault_path=None):
    """1-2 hop diffusion search from keyword-matched nodes."""
    vault = vault_path or get_vault_path()
    if not GRAPH_PATH.exists():
        print("[graph] No graph found, run build first")
        return
    graph = json.loads(GRAPH_PATH.read_text("utf-8"))
    
    # Find seed nodes matching keyword in title/category/content
    seeds = set()
    kw_lower = keyword.lower()
    for node_id, node_data in graph["nodes"].items():
        if kw_lower in node_id.lower() or kw_lower in node_data.get("title", "").lower():
            seeds.add(node_id)
    
    # 1-hop: direct neighbors
    result = set(seeds)
    for edge in graph["edges"]:
        if edge["source"] in seeds:
            result.add(edge["target"])
        if edge["target"] in seeds:
            result.add(edge["source"])
    
    # 2-hop: neighbors of neighbors
    second_hop = set()
    for edge in graph["edges"]:
        if edge["source"] in result or edge["target"] in result:
            second_hop.add(edge["source"])
            second_hop.add(edge["target"])
    result.update(second_hop)
    result = {n for n in result if n in graph["nodes"]}  # filter valid
    
    return graph, seeds, result


def search_cli(keyword):
    """CLI wrapper for search."""
    graph, seeds, result = search(keyword)
    if not result:
        print(f"[graph] No results for '{keyword}'")
        return
    
    print(f"[graph] '{keyword}' — {len(seeds)} seeds, {len(result)} total (1-2 hop)\n")
    # Show by pagerank
    ranked = [(n, graph["nodes"][n].get("pagerank", 0)) for n in result]
    ranked.sort(key=lambda x: -x[1])
    for node_id, pr in ranked[:15]:
        title = graph["nodes"][node_id].get("title", node_id)
        cat = graph["nodes"][node_id].get("category", "")
        incoming = graph["nodes"][node_id].get("incoming", 0)
        marker = " ⭐" if node_id in seeds else ""
        print(f"  {pr:.3f} [{cat}] {title}{marker} (in: {incoming})")
        print(f"       {node_id}")


def hubs(top=10):
    """Show top hub notes by PageRank."""
    if not GRAPH_PATH.exists():
        print("[graph] No graph found, run build first")
        return
    graph = json.loads(GRAPH_PATH.read_text("utf-8"))
    ranked = [(n, d.get("pagerank", 0)) for n, d in graph["nodes"].items()]
    ranked.sort(key=lambda x: -x[1])
    
    print(f"[graph] Top {min(top, len(ranked))} Hub Notes by PageRank:\n")
    for node_id, pr in ranked[:top]:
        title = graph["nodes"][node_id].get("title", node_id)
        cat = graph["nodes"][node_id].get("category", "")
        incoming = graph["nodes"][node_id].get("incoming", 0)
        print(f"  {pr:.4f} [{cat}] {title}")
        print(f"       {node_id} ({incoming} incoming links)")


def stats():
    """Print graph statistics."""
    if not GRAPH_PATH.exists():
        print("[graph] No graph found, run build first")
        return
    graph = json.loads(GRAPH_PATH.read_text("utf-8"))
    meta = graph.get("meta", {})
    nodes = graph["nodes"]
    in_links = [n.get("incoming", 0) for n in nodes.values()]
    print(f"[graph] Knowledge Graph Status")
    print(f"  Nodes: {meta.get('total_nodes', len(nodes))}")
    print(f"  Edges: {meta.get('total_edges', len(graph['edges']))}")
    print(f"  Orphans: {meta.get('orphans', 0)}")
    print(f"  Avg in-links: {sum(in_links)/len(in_links):.1f}" if in_links else "  Avg in-links: 0")
    print(f"  Size: {GRAPH_PATH.stat().st_size / 1024:.0f} KB")


def main():
    parser = argparse.ArgumentParser(description="Vault knowledge graph engine")
    parser.add_argument("action", choices=["build", "search", "hubs", "stats"])
    parser.add_argument("keyword", nargs="?", default="")
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()
    
    if args.action == "build":
        build()
    elif args.action == "search":
        if not args.keyword:
            print("[graph] Provide a keyword to search")
            return
        search_cli(args.keyword)
    elif args.action == "hubs":
        hubs(args.top)
    elif args.action == "stats":
        stats()


if __name__ == "__main__":
    main()
