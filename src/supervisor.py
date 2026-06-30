#!/usr/bin/env python3
"""
Reasonix Supervisor Daemon v2
==============================
TCP :49522 (configurable) — rule-checking interceptor for PreToolUse events.

Architecture:
  1. hook_logger forwards PreToolUse payload → supervisor
  2. supervisor runs plain-text rule detection (<50ms, no LLM call)
  3. On violation → returns systemMessage; on pass → {"violated": false}
  4. hook_logger emits the systemMessage to stdout, which is injected into the
     AI's context on the next turn.

Start:
    python supervisor.py

Self-test (simulate a violation):
    python -c "import json,socket;s=socket.socket();s.connect(('127.0.0.1',49522));\
s.sendall(json.dumps({'event':'PreToolUse','toolName':'write_file',\
'toolArgs':{'path':'D:\\test\\中文路径\\config.toml'}}).encode());print(s.recv(4096).decode());s.close()"
"""

import json
import socket
import datetime
import threading
import re
import os
import sys
from pathlib import Path

from config import load_config, vault_path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
cfg = load_config()
SUPERVISOR_PORT = cfg.get("supervisor", {}).get("port", 49522)
HOST = "127.0.0.1"
VAULT_PATH = vault_path()
MEMORY_DIR = VAULT_PATH / "记忆"
SUPERVISION_LOG = VAULT_PATH / "监督日志.md"

# ---------------------------------------------------------------------------
# Global caches
# ---------------------------------------------------------------------------
_rules: list[tuple[str, str, str]] = []          # (domain, rule_name, rule_text)
_error_patterns: list[tuple[str, str, int, str, str]] = []  # (domain, name, freq, sol, phen)

# Loop-detection state
_tool_fail_tracker: dict[str, dict] = {}
_FAIL_THRESHOLD = 3


# ═══════════════════════════════════════════════════════════════════════════
# Rule loading
# ═══════════════════════════════════════════════════════════════════════════

def load_all_memory():
    """Scan 记忆/ for all 规则.md and 高频错误.md files and cache them."""
    global _rules, _error_patterns
    _rules = []
    _error_patterns = []

    if not MEMORY_DIR.exists():
        print(f"[supervisor] ERROR: memory dir not found: {MEMORY_DIR}", file=sys.stderr)
        return

    for domain_dir in sorted(MEMORY_DIR.iterdir()):
        if not domain_dir.is_dir():
            continue
        domain = domain_dir.name

        rules_file = domain_dir / "规则.md"
        if rules_file.exists():
            text = rules_file.read_text(encoding="utf-8")
            _extract_rules(domain, text)

        errors_file = domain_dir / "高频错误.md"
        if errors_file.exists():
            text = errors_file.read_text(encoding="utf-8")
            _extract_error_patterns(domain, text)

    print(f"[supervisor] Loaded {len(_rules)} rules, {len(_error_patterns)} error patterns "
          f"from {MEMORY_DIR}", file=sys.stderr)


def _extract_rules(domain: str, text: str):
    """Parse a 规则.md file into (domain, rule_name, rule_text) tuples."""
    lines = text.split("\n")
    current_rule = None
    for line in lines:
        m = re.match(r"##\s+\S+\s+(.+)$", line)
        if m:
            current_rule = m.group(1).strip()
            continue
        m2 = re.match(r"\d+\.\s+\*\*(.+?)\*\*", line)
        if m2 and current_rule:
            _rules.append((domain, current_rule, m2.group(1).strip()))


def _extract_error_patterns(domain: str, text: str):
    """Parse a 高频错误.md file into structured patterns."""
    lines = text.split("\n")
    cur = None
    freq = 0
    sol = ""
    phen = ""
    for line in lines:
        m = re.match(r"##\s+#(\d+)\s+(.+)$", line)
        if m:
            if cur:
                _error_patterns.append((domain, cur, freq, sol, phen))
            cur = m.group(2).strip()
            freq = 0
            sol = ""
            phen = ""
        fm = re.match(r"\*\*次数：\*\*\s*(\d+)", line)
        if fm and cur:
            freq = int(fm.group(1))
        pm = re.match(r"\*\*现象：\*\*\s*(.+)", line)
        if pm and cur:
            phen = pm.group(1).strip()
        sm = re.match(r"\*\*解决：\*\*\s*(.+?)(?:\*\*来源|\Z)", line)
        if sm and cur:
            sol = sm.group(1).strip()
        sm2 = re.match(r"\*\*规则：\*\*\s*(.+)", line)
        if sm2 and cur:
            sol = sm2.group(1).strip()
    if cur:
        _error_patterns.append((domain, cur, freq, sol, phen))


# ═══════════════════════════════════════════════════════════════════════════
# Rule engine — plain-text / keyword matching
# ═══════════════════════════════════════════════════════════════════════════

def has_chinese(text: str) -> bool:
    """Check if *text* contains any CJK characters."""
    if not isinstance(text, str):
        return False
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f':
            return True
    return False


_PATH_LIKE_EXTS = frozenset({
    ".py", ".go", ".md", ".txt", ".toml", ".json",
    ".yaml", ".yml", ".exe", ".bat", ".ps1", ".sh",
    ".csv", ".xml", ".ini", ".cfg", ".conf",
})


def is_path_like(value):
    """Heuristic: does *value* look like a file path?"""
    if not isinstance(value, str) or not value:
        return False
    if "\\" in value or "/" in value:
        return True
    return any(value.lower().endswith(ext) for ext in _PATH_LIKE_EXTS)


def iter_arg_values(tool_args: dict):
    """Recursively yield (key, value) pairs from tool_args."""
    if not isinstance(tool_args, dict):
        return
    for key, value in tool_args.items():
        yield key, value
        if isinstance(value, dict):
            yield from iter_arg_values(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield from iter_arg_values(item)
                else:
                    yield None, item


def _detect_hardcoded_rules(tool_name: str, tool_args: dict) -> dict | None:
    """Built-in rules — fast matching for common high-frequency error patterns."""
    tool_lower = tool_name.lower()
    all_values = list(iter_arg_values(tool_args))

    # Global high-frequency #1 — GBK encoding conflict
    if any(kw in tool_lower for kw in ["echo", "bash", "powershell", "shell", "cmd"]):
        for _, val in all_values:
            if has_chinese(val):
                return {
                    "rule": "Global HF #1 — GBK encoding conflict",
                    "detail": "Command/script arguments contain CJK characters; may trigger UnicodeDecodeError under GBK",
                    "solution": "Pass Chinese paths/filenames via variables; explicitly specify encoding='utf-8'; for Go files use encoding='gbk'",
                    "domain": "全局",
                }

    # Global HF #1 (variant) — Chinese paths in file tools
    if any(kw in tool_lower for kw in ["read_file", "write_file", "edit_file",
                                        "glob", "grep", "move_file", "copy"]):
        for _, val in all_values:
            if has_chinese(val) and is_path_like(val):
                return {
                    "rule": "Global HF #1 — Chinese path in tool argument",
                    "detail": f"File tool path argument contains CJK characters",
                    "solution": "Ensure filesystem encoding is UTF-8; for GBK systems explicitly pass encoding='gbk'",
                    "domain": "全局",
                }

    # Global rule — back up config files before overwriting
    if tool_lower == "write_file":
        path_arg = tool_args.get("path", "")
        if isinstance(path_arg, str) and path_arg.strip():
            if any(path_arg.lower().endswith(ext) for ext in [".toml", ".json", ".yaml", ".yml"]):
                return {
                    "rule": "Global rule — back up config before writing",
                    "detail": f"Writing directly to `{path_arg}` without a backup",
                    "solution": f"Run `Copy-Item '{path_arg}' '{path_arg}.bak'` first",
                    "domain": "全局",
                }

    # Programming / QQ Bot: build-directory error
    if tool_lower in ("bash", "powershell", "shell"):
        for _, val in all_values:
            val_str = str(val).lower()
            if "go build" in val_str and "cmd" not in val_str:
                return {
                    "rule": "Programming HF #4 / QQ Bot HF #1 — wrong build directory",
                    "detail": "`go build` not executed inside the `cmd/reasonix` subdirectory",
                    "solution": "cd into `cmd/reasonix` then `go build -o ..\\..\\reasonix.exe .`",
                    "domain": "编程",
                }

    # QQ Bot: process-hold timeout
    if tool_lower in ("bash", "powershell", "shell"):
        for _, val in all_values:
            val_str = str(val)
            if "reasonix" in val_str.lower() and "bot start" in val_str.lower():
                if "Start-Process" not in val_str and "start /B" not in val_str and "-NoNewWindow" not in val_str:
                    return {
                        "rule": "QQ Bot HF #5 — process-hold timeout",
                        "detail": "Bot start command runs in foreground, causing bash timeout (>2m)",
                        "solution": "Use `Start-Process -NoNewWindow` or `cmd /c start /B` to launch in background",
                        "domain": "QQ Bot",
                    }

    return None


def _detect_loop(tool_name: str, tool_args: dict) -> dict | None:
    """Detect tool-call loops — same tool + similar args failing >= 3 times."""
    import datetime
    global _tool_fail_tracker

    args_str = json.dumps(tool_args, ensure_ascii=False)
    now = datetime.datetime.now()
    tracker = _tool_fail_tracker

    if tool_name not in tracker:
        tracker[tool_name] = {"count": 0, "first_seen": now, "last_seen": now}
        return None

    t = tracker[tool_name]

    # Reset if more than 5 minutes since last call
    if (now - t["last_seen"]).total_seconds() > 300:
        t["count"] = 0
        t["first_seen"] = now
        t["count"] = 1
        t["first_seen"] = now
        t["last_seen"] = now
        return None

    t["count"] += 1
    t["last_seen"] = now

    if t["count"] >= _FAIL_THRESHOLD:
        t["count"] = 0
        return {
            "rule": "Loop detection — repeated tool calls",
            "detail": f"`{tool_name}` called {_FAIL_THRESHOLD} times in a row; may be stuck in a loop",
            "solution": "Consider a different approach: switch tools, adjust parameters, or check preconditions",
            "domain": "全局",
        }

    return None


# ═══════════════════════════════════════════════════════════════════════════
# Combined check
# ═══════════════════════════════════════════════════════════════════════════

def check_tool_call(tool_name: str, tool_args: dict) -> dict | None:
    """Run all rule checks against a tool call. Returns None or a violation dict."""

    # 0) Loop detection
    loop_hit = _detect_loop(tool_name, tool_args)
    if loop_hit:
        return _build_violation(tool_name, loop_hit)

    # 1) Built-in rules (covers the most common mistakes)
    hit = _detect_hardcoded_rules(tool_name, tool_args)
    if hit:
        return _build_violation(tool_name, hit)

    # 2) Match against loaded error patterns from memory files
    args_str = json.dumps(tool_args, ensure_ascii=False)
    tool_and_args = f"{tool_name} {args_str}".lower()

    for domain, error_name, freq, solution, phenomenon in _error_patterns:
        keywords = re.findall(r'[\u4e00-\u9fff\w]+', phenomenon + " " + error_name)
        keywords = [w for w in keywords if len(w) >= 2]
        if any(kw.lower() in tool_and_args for kw in keywords):
            hit = {
                "rule": f"{domain} HF — {error_name} (occurred {freq} times)",
                "detail": f"Matching keywords: {phenomenon[:100]}",
                "solution": solution or "See memory file for solution",
                "domain": domain,
            }
            return _build_violation(tool_name, hit)

    # 3) Match against loaded rules from memory files
    for domain, rule_name, rule_text in _rules:
        if "delete" in rule_text.lower() and "explain" in rule_text.lower():
            if tool_name in ("delete_range", "delete_symbol", "move_file"):
                hit = {
                    "rule": f"{domain} rule — {rule_name}",
                    "detail": "Delete operations must explain impact first",
                    "solution": "Explain to the user what files will be deleted and the impact",
                    "domain": domain,
                }
                return _build_violation(tool_name, hit)

    return None


def _build_violation(tool_name: str, hit: dict) -> dict:
    """Wrap a rule hit into a complete response with systemMessage."""
    return {
        "violated": True,
        "rule": hit["rule"],
        "detail": hit.get("detail", ""),
        "solution": hit.get("solution", ""),
        "domain": hit.get("domain", ""),
        "systemMessage": (
            f"⚠️ **Supervisor detected a rule violation**\n\n"
            f"- **Tool:** `{tool_name}`\n"
            f"- **Rule:** {hit['rule']}\n"
            f"- **Detail:** {hit.get('detail', '')}\n"
            f"- **Suggestion:** {hit.get('solution', '')}\n"
            f"- **Source:** 记忆/{hit.get('domain', '?')}/高频错误.md"
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Supervision log
# ═══════════════════════════════════════════════════════════════════════════

def append_supervision_log(violation: dict, payload: dict):
    """Append one violation record to 监督日志.md."""
    try:
        now = datetime.datetime.now()
        SUPERVISION_LOG.parent.mkdir(parents=True, exist_ok=True)

        is_new = not SUPERVISION_LOG.exists() or SUPERVISION_LOG.stat().st_size == 0

        tool_name = payload.get("toolName", "")
        tool_args = payload.get("toolArgs", {})

        parts = []
        if is_new:
            parts.append(
                f"# Supervision Log\n\n"
                f"Created at {now.strftime('%Y-%m-%d %H:%M')}\n"
                f"Source: supervisor.py (TCP :{SUPERVISOR_PORT})\n\n"
                "## Format\nEach record: time / tool / violated rule / solution\n\n---\n\n"
            )

        parts.append(f"## 🚨 {now.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        parts.append(f"- **Event:** PreToolUse\n")
        parts.append(f"- **Tool:** `{tool_name}`\n")
        parts.append(f"- **Args:** ```json\n{json.dumps(tool_args, ensure_ascii=False, indent=2)[:300]}\n```\n")
        parts.append(f"- **Rule violated:** {violation.get('rule', 'Unknown')}\n")
        parts.append(f"- **Detail:** {violation.get('detail', '')}\n")
        parts.append(f"- **Solution:** {violation.get('solution', '')}\n")
        parts.append("\n---\n\n")

        with open(SUPERVISION_LOG, "a", encoding="utf-8") as f:
            f.write("".join(parts))
    except Exception as e:
        print(f"[supervisor] Failed to write supervision log: {e}", file=sys.stderr)


# ═══════════════════════════════════════════════════════════════════════════
# TCP service (request-response)
# ═══════════════════════════════════════════════════════════════════════════

def handle_client(conn):
    try:
        data = conn.recv(65536)
        if not data:
            return

        payload = json.loads(data.decode("utf-8"))
        event = payload.get("event", "")

        if event != "PreToolUse":
            conn.sendall(json.dumps({"violated": False}).encode("utf-8"))
            return

        tool_name = payload.get("toolName", "")
        tool_args = payload.get("toolArgs", {})

        result = check_tool_call(tool_name, tool_args)

        if result and result["violated"]:
            print(f"[supervisor] ⚠️ VIOLATION: {result['rule']} | tool={tool_name}", file=sys.stderr)
            t_log = threading.Thread(target=append_supervision_log, args=(result, payload))
            t_log.daemon = True
            t_log.start()
            response = json.dumps(result, ensure_ascii=False)
        else:
            response = json.dumps({"violated": False}, ensure_ascii=False)

        conn.sendall(response.encode("utf-8"))

    except json.JSONDecodeError:
        print(f"[supervisor] Invalid JSON received", file=sys.stderr)
    except Exception as e:
        print(f"[supervisor] Error: {e}", file=sys.stderr)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    load_all_memory()

    SUPERVISION_LOG.parent.mkdir(parents=True, exist_ok=True)

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server.bind((HOST, SUPERVISOR_PORT))
    except OSError as e:
        print(f"[supervisor] ❌ Bind failed: {e}", file=sys.stderr)
        sys.exit(1)

    server.listen(10)
    print(f"[supervisor] 🟢 Listening on {HOST}:{SUPERVISOR_PORT} (PID: {os.getpid()})", file=sys.stderr)
    print(f"[supervisor]    Rules: GBK encoding | Chinese paths | config backup | build dir | bot process | loop detection | +memory file matching", file=sys.stderr)
    print(f"[supervisor]    Log: {SUPERVISION_LOG}", file=sys.stderr)

    try:
        while True:
            conn, addr = server.accept()
            t = threading.Thread(target=handle_client, args=(conn,))
            t.daemon = True
            t.start()
    except KeyboardInterrupt:
        print("[supervisor] Shutdown", file=sys.stderr)
        server.close()


if __name__ == "__main__":
    main()
