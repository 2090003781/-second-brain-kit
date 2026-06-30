#!/usr/bin/env python3
"""
Obsidian Writer daemon
======================
Listens on TCP :49520 (configurable), receives hook events and writes Markdown
files into the Obsidian vault (raw timeline + topic-organized entries).

Usage:
    python obsidian_writer.py

Config (config.toml):
    [writer]
    enabled = true
    port = 49520
    [vault]
    path = "/path/to/your/vault"
"""

import json
import socket
import datetime
import threading
import os
import sys
import re
import portalocker
from pathlib import Path

from config import load_config, vault_path

# ---------------------------------------------------------------------------
# Configuration (from config file or env)
# ---------------------------------------------------------------------------
cfg = load_config()
VAULT_PATH = vault_path()
HOST = "127.0.0.1"
PORT = cfg.get("writer", {}).get("port", 49520)

_open_files: dict[str, object] = {}
_open_lock = threading.Lock()
_current_topic: str | None = None
_topic_lock = threading.Lock()


def _get_raw_handle(date: datetime.date):
    """Open (or reuse) the raw-timeline file for a given date."""
    raw_dir = VAULT_PATH / "reasonix-raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    file_path = raw_dir / f"{date.strftime('%Y-%m-%d')}.md"
    key = f"raw:{date.isoformat()}"
    with _open_lock:
        fh = _open_files.get(key)
        if fh and not fh.closed:
            return fh, file_path
        fh = open(file_path, "a", encoding="utf-8", buffering=1)
        _open_files[key] = fh
        return fh, file_path


def _get_topic_handle(topic_name: str):
    """Open (or reuse) the topic file for *topic_name*."""
    safe = re.sub(r'[\\/:*?"<>|]', "", topic_name).strip()
    if not safe:
        safe = "unknown-topic"
    if len(safe) > 80:
        safe = safe[:80]
    topic_dir = VAULT_PATH / "话题"
    topic_dir.mkdir(parents=True, exist_ok=True)
    file_path = topic_dir / f"{safe}.md"
    key = f"topic:{safe}"
    with _open_lock:
        fh = _open_files.get(key)
        if fh and not fh.closed:
            return fh, file_path, safe
        fh = open(file_path, "a", encoding="utf-8", buffering=1)
        _open_files[key] = fh
        return fh, file_path, safe


def _close_handle(key: str):
    """Flush and close an open file handle."""
    with _open_lock:
        fh = _open_files.pop(key, None)
        if fh and not fh.closed:
            fh.flush()
            fh.close()


def _write_locked(fh, file_path: Path, text: str):
    """Write *text* to *fh* with portalocker exclusive lock."""
    try:
        portalocker.lock(fh, portalocker.LOCK_EX)
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    finally:
        try:
            portalocker.unlock(fh)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Topic marker parsing
# ---------------------------------------------------------------------------

_TOPIC_PATTERN = re.compile(
    r"[─\-—]{2,}\s*话题分隔\s*[:：]\s*(.+?)\s*[─\-—]{2,}"
)


def _parse_topic_marker(text: str) -> str | None:
    """Extract topic from a marker like '── 话题分隔：my-topic ──'."""
    if not text:
        return None
    m = _TOPIC_PATTERN.search(text)
    if m:
        return m.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# Error detection helpers
# ---------------------------------------------------------------------------

_ERROR_PREFIXES = (
    "error:", "error ", "exception:", "traceback",
    "panic:", "fatal:", "syntaxerror", "importerror",
    "typeerror", "valueerror", "keyerror",
)


def _looks_like_error(text: str) -> bool:
    """Heuristic check whether *text* looks like an error message."""
    if not text:
        return False
    lower = text.lower().strip()[:300].lstrip()
    return any(lower.startswith(p) for p in _ERROR_PREFIXES)


# ---------------------------------------------------------------------------
# Event processing
# ---------------------------------------------------------------------------

def process_event(payload: dict):
    """Handle one hook event payload."""
    event = payload.get("event", "")
    if not event:
        return

    ts = payload.get("ts", datetime.datetime.now().isoformat())
    try:
        dt = datetime.datetime.fromisoformat(ts)
    except Exception:
        dt = datetime.datetime.now()

    date = dt.date()
    timestamp = dt.strftime("%H:%M:%S")
    date_str = dt.strftime("%Y-%m-%d %H:%M")
    date_compact = dt.strftime("%Y-%m-%d")
    session_id = payload.get("session_id") or payload.get("sessionId") or "unknown"

    tool_name = payload.get("toolName", "")
    tool_args = payload.get("toolArgs", {})
    tool_result = payload.get("toolResult", "")
    prompt = payload.get("prompt", "")
    msg = payload.get("message", "")
    content = payload.get("content", "")
    level = payload.get("level", "")

    # Detect topic marker in any text field
    topic_from_marker = None
    for src in [content, prompt, json.dumps(tool_args, ensure_ascii=False), str(tool_result)]:
        if src:
            t = _parse_topic_marker(src)
            if t:
                topic_from_marker = t
                break

    global _current_topic
    if topic_from_marker:
        with _topic_lock:
            _current_topic = topic_from_marker

    with _topic_lock:
        current_topic = _current_topic

    # Build raw-timeline entry
    raw_label = event
    raw_line = ""

    if event == "SessionStart":
        session_id = payload.get("session_id") or session_id
        cwd = payload.get("cwd", "")
        model = payload.get("model", "")
        raw_label = "Session Start"
        raw_line = f"Session: {session_id}  |  cwd: {cwd}  |  model: {model}"
    elif event == "SessionEnd":
        raw_label = "Session End"
        raw_line = f"Session: {session_id}"
    elif event == "UserPromptSubmit":
        raw_label = "User Prompt"
        raw_line = (prompt or "(empty)")[:300]
    elif event == "PreToolUse":
        raw_label = "Tool Call"
        args_preview = json.dumps(tool_args, ensure_ascii=False)[:200]
        raw_line = f"{tool_name} {args_preview}"
    elif event == "PostToolUse":
        is_err = _looks_like_error(str(tool_result)) if tool_result else False
        raw_label = "Tool Error" if is_err else "Tool Result"
        preview = str(tool_result)[:200] if tool_result else "(no return)"
        raw_line = f"{tool_name} -> {preview}"
    elif event == "Stop":
        turn = payload.get("turn", 0)
        raw_label = "Turn Complete"
        raw_line = f"Turn {turn}"
    elif event == "Checkpoint":
        raw_label = f"Checkpoint ({level})"
        raw_line = (content or "")[:300]
    elif event == "PreCompact":
        raw_label = "Context Compress"
        raw_line = f"trigger: {payload.get('trigger', 'auto')}"
    elif event == "Notification":
        raw_label = "Notification"
        raw_line = (msg or "")[:300]
    elif event == "PostLLMCall":
        raw_label = "Model Output"
        reply = payload.get("toolResult", "") or payload.get("message", "")
        raw_line = str(reply).replace(chr(10), " ")[:200]
    elif event == "SubagentStop":
        raw_label = "Subtask Done"
        raw_line = ""
    elif event == "PermissionRequest":
        raw_label = "Permission Request"
        raw_line = tool_name
    else:
        raw_label = event
        raw_line = (content or "")[:300]

    # 1) Write raw timeline
    fh_raw, raw_path = _get_raw_handle(date)
    if not raw_path.exists() or raw_path.stat().st_size == 0:
        _write_locked(fh_raw, raw_path, f"# {date_compact} Raw Timeline\n\n")
    raw_md = f"## {timestamp} {raw_label}\n{raw_line}\n\n"
    _write_locked(fh_raw, raw_path, raw_md)

    # 2) Write topic file if we have a current topic
    if not current_topic:
        return

    fh_topic, topic_path, safe_name = _get_topic_handle(current_topic)
    is_new = not topic_path.exists() or topic_path.stat().st_size == 0

    parts = []
    if is_new:
        parts.append(
            f"---\ncreated: {date_compact}\ntags: [reasonix/topic, reasonix/active]\nstatus: active\n---\n\n# {current_topic}\n\n"
        )
    parts.append(f"## {date_str} | from session {session_id}\n\n")

    if event == "SessionStart":
        cwd = payload.get("cwd", "")
        model = payload.get("model", "")
        parts.append(f"**Session Start** | cwd: `{cwd}` | model: {model}\n")
    elif event == "SessionEnd":
        parts.append("**→ Session End**\n")
        _close_handle(f"topic:{safe_name}")
    elif event == "UserPromptSubmit":
        parts.append(f"**User Prompt:** {prompt}\n")
    elif event == "PreToolUse":
        args_preview = json.dumps(tool_args, ensure_ascii=False)[:300]
        parts.append(f"**Tool Call:** `{tool_name}`\n  Args: `{args_preview}`\n")
    elif event == "PostToolUse":
        is_err = _looks_like_error(str(tool_result)) if tool_result else False
        preview = str(tool_result)[:300] if tool_result else "(no return)"
        if is_err:
            parts.append(f"**❌ Tool Error** `{tool_name}`:\n  ```\n{preview}\n  ```\n")
            parts.append(f"→ ❌ Error: {preview[:200]}\n")
        else:
            parts.append(f"**✅ Tool Complete** `{tool_name}` → {preview}\n")
    elif event == "Stop":
        turn = payload.get("turn", 0)
        parts.append(f"**Turn {turn} Complete**\n")
    elif event == "Checkpoint":
        emoji_map = {"milestone": "🎯", "progress": "📊", "blocker": "🚧"}
        emoji = emoji_map.get(level, "📌")
        # Strip topic marker from content for cleaner display
        clean_content = re.sub(r'[─\-—]{2,}\s*话题分隔\s*[:：]\s*.+?\s*[─\-—]{2,}', '', content).strip()
        parts.append(f"**{emoji} Checkpoint ({level}):** {clean_content}\n")
    elif event == "PreCompact":
        parts.append(f"**📦 Context Compress** (trigger: {payload.get('trigger', 'auto')})\n")
    elif event == "Notification":
        parts.append(f"**Notification:** {msg}\n")
    elif event == "PostLLMCall":
        reply = payload.get("toolResult", "") or payload.get("message", "")
        preview = str(reply).replace(chr(10), " ")[:300]
        parts.append(f"**🤖 Model Output:** {preview}\n")
    elif event == "SubagentStop":
        parts.append("**🔄 Sub-task Done**\n")
    elif event == "PermissionRequest":
        parts.append(f"**🔒 Permission Request:** `{tool_name}`\n")
    else:
        parts.append(f"**Event {event}:** {(content or '')[:200]}\n")

    parts.append("\n---\n\n")
    _write_locked(fh_topic, topic_path, "".join(parts))

    if event == "SessionEnd":
        _close_handle(f"raw:{date.isoformat()}")


# ---------------------------------------------------------------------------
# TCP server
# ---------------------------------------------------------------------------

def handle_client(conn):
    """Receive one JSON payload and process it."""
    try:
        data = conn.recv(65536)
        if not data:
            return
        payload = json.loads(data.decode("utf-8"))
        process_event(payload)
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    vault = VAULT_PATH
    if not vault.exists():
        print(f"[obsidian-writer] ERROR: vault not found: {vault}", file=sys.stderr)
        sys.exit(1)
    test_file = vault / ".ow_test"
    try:
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
    except Exception as e:
        print(f"[obsidian-writer] ERROR: vault not writable: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"[obsidian-writer] vault ok: {vault}", file=sys.stderr)
    (vault / "reasonix-raw").mkdir(parents=True, exist_ok=True)
    (vault / "话题").mkdir(parents=True, exist_ok=True)

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    server.bind((HOST, PORT))
    server.listen(5)
    print(f"[obsidian-writer] Listening on {HOST}:{PORT} (PID: {os.getpid()})", file=sys.stderr)
    try:
        while True:
            conn, addr = server.accept()
            t = threading.Thread(target=handle_client, args=(conn,))
            t.daemon = True
            t.start()
    except KeyboardInterrupt:
        print("[obsidian-writer] shutdown", file=sys.stderr)
        with _open_lock:
            for k, fh in list(_open_files.items()):
                try:
                    fh.flush()
                    fh.close()
                except Exception:
                    pass
            _open_files.clear()
        server.close()


if __name__ == "__main__":
    main()
