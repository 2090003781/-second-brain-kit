#!/usr/bin/env python3
"""
Reasonix Hook Logger v4.1 — Relay mode + JSONL fallback
=========================================================
Reads JSON payload from stdin, forwards to obsidian_writer daemon (TCP :49520).
On connection failure, falls back to JSONL logging.

Also forwards PreToolUse events to supervisor (TCP :49522) and emits any
violation systemMessage to stdout.

Usage (piped from hook system):
    echo '{"event":"UserPromptSubmit","prompt":"hello"}' | python hook_logger.py
"""

import json
import sys
import datetime
import hashlib
import socket
from pathlib import Path

from config import load_config, vault_path, logs_dir, session_file as _session_file

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
cfg = load_config()
LOGS_DIR = logs_dir()
SESSION_FILE = _session_file()
VAULT_PATH = vault_path()

# Daemon forward config
OBSIDIAN_WRITER_HOST = "127.0.0.1"
OBSIDIAN_WRITER_PORT = cfg.get("writer", {}).get("port", 49520)
SUPERVISOR_HOST = "127.0.0.1"
SUPERVISOR_PORT = cfg.get("supervisor", {}).get("port", 49522)

# Key event types for .kw file
_KW_TYPES = {"decision", "error", "milestone", "progress", "blocker", "resume", "write", "filecreate", "test"}
_SESSION_END = "SessionEnd"

_TOPIC_MARKER_PATTERN = (
    r'[─\-—]{2,}\s*话题分隔\s*[:：]\s*(.+?)\s*[─\-—]{2,}'
)


def read_session_id() -> str | None:
    """Read the current session ID from session file."""
    if SESSION_FILE.exists():
        try:
            data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            return data.get("session_id")
        except (json.JSONDecodeError, KeyError):
            pass
    return None


def new_session_id() -> str:
    """Generate a new session ID and persist it."""
    now = datetime.datetime.now()
    suffix = hashlib.md5(str(now.timestamp()).encode()).hexdigest()[:8]
    session_id = f"reasonix-{now.strftime('%Y%m%d%H%M%S')}-{suffix}"
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(
        json.dumps({"session_id": session_id}, ensure_ascii=False), encoding="utf-8"
    )
    return session_id


def clear_session():
    """Remove the session file."""
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()


def truncate(text, max_len=500):
    if not text:
        return ""
    text = str(text)
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def write_kw_entry(session_id, prefix, text):
    """Append a key-work entry to the .kw file for this session."""
    if not session_id or not text:
        return
    kw_path = LOGS_DIR / f"{session_id}.kw"
    try:
        line = f"{prefix}: {text[:200]}\n"
        with open(kw_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def write_log_entry(session_id, log_entry):
    """Write one JSONL entry; also write .kw for key events."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / f"{session_id}.jsonl"
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception:
        return

    level = log_entry.get("level", "")
    log_type = log_entry.get("type", "")
    content = log_entry.get("content", "")
    event = log_entry.get("event", "")

    if event == "SessionStart":
        write_kw_entry(session_id, "first", content.replace("会话开始", "").strip().strip("()").strip())
    elif event == "SessionEnd":
        write_kw_entry(session_id, "session_end", content)
    elif log_type in _KW_TYPES:
        clean = content
        if " | 参数: " in clean:
            clean = clean.split(" | 参数: ", 1)[0] + " | ..."
        write_kw_entry(session_id, log_type, clean)
    elif level in ("milestone", "progress", "blocker"):
        write_kw_entry(session_id, level, content)


# ---------------------------------------------------------------------------
# TCP forwarding
# ---------------------------------------------------------------------------

def forward_to_daemon(payload: dict) -> bool:
    """Forward payload to obsidian_writer daemon; return True on success."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect((OBSIDIAN_WRITER_HOST, OBSIDIAN_WRITER_PORT))
        payload["_vault_path"] = str(VAULT_PATH)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        sock.sendall(data)
        sock.close()
        return True
    except (ConnectionRefusedError, OSError, socket.timeout):
        return False


def forward_to_supervisor(payload: dict) -> dict | None:
    """
    Forward payload to supervisor (request-response mode).

    For PreToolUse events, read the JSON response:
      - {"violated": False} → pass
      - {"violated": True, "systemMessage": "..."} → violation

    For other events, fire-and-forget.
    Returns None on connection failure.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        sock.connect((SUPERVISOR_HOST, SUPERVISOR_PORT))
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        sock.sendall(data)

        event = payload.get("event", "")
        if event == "PreToolUse":
            resp_data = sock.recv(65536)
            if resp_data:
                result = json.loads(resp_data.decode("utf-8"))
                sock.close()
                return result

        sock.close()
        return {"violated": False}
    except (ConnectionRefusedError, OSError, socket.timeout):
        return None
    except json.JSONDecodeError:
        return {"violated": False}


# ---------------------------------------------------------------------------
# Error detection
# ---------------------------------------------------------------------------

_REAL_ERROR_PREFIXES = (
    "error:", "error ", "exception:", "exception ",
    "traceback (most recent", "panic:", "fatal:",
    "syntaxerror", "importerror", "typeerror",
    "valueerror", "keyerror", "attributeerror",
    "runtimeerror", "ioerror", "filenotfound",
)
_REAL_ERROR_PATTERNS = (
    "traceback", "stack trace", "failed with",
)


def looks_like_real_error(text):
    if not text:
        return False
    lower = text.lower().strip()[:300]
    lower_stripped = lower.lstrip()
    for prefix in _REAL_ERROR_PREFIXES:
        if lower_stripped.startswith(prefix):
            return True
    for pat in _REAL_ERROR_PATTERNS:
        if pat in lower_stripped:
            return True
    return False


# ---------------------------------------------------------------------------
# Event formatting
# ---------------------------------------------------------------------------

def format_content(event, payload):
    """Build the log fields for a given event."""
    tool_name = payload.get("toolName", "")
    tool_args = payload.get("toolArgs", {})
    tool_result = payload.get("toolResult", "")
    prompt = payload.get("prompt", "")
    msg = payload.get("message", "")

    level = "detail"
    log_type = "action"
    content = ""
    session_id = ""

    if event == "SessionStart":
        clear_session()
        session_id = new_session_id()
        content = f"Session start (cwd: {payload.get('cwd', '')})"
        log_type = "resume"
        _auto_compress()

    elif event == "SessionEnd":
        session_id = read_session_id()
        content = "Session end"
        log_type = "summary"
        clear_session()

    elif event == "Checkpoint":
        session_id = read_session_id()
        level = payload.get("level", "progress")
        content = truncate(payload.get("content", ""), 500)
        log_type = payload.get("logType", "progress")

    elif event == "PreToolUse":
        session_id = read_session_id()
        args_str = truncate(json.dumps(tool_args, ensure_ascii=False), 300)
        content = f"Tool call {tool_name} | Args: {args_str}"
        log_type = "action"

    elif event == "PostToolUse":
        session_id = read_session_id()
        result_str = str(tool_result) if tool_result else ""
        if result_str and len(result_str) < 200:
            content = f"Tool {tool_name} → {result_str}"
        elif result_str:
            preview = truncate(result_str, 200)
            content = f"Tool {tool_name} → {preview}"
        else:
            content = f"Tool {tool_name} executed"
        is_real_error = looks_like_real_error(result_str)
        log_type = "error" if is_real_error else "action"

    elif event == "UserPromptSubmit":
        session_id = read_session_id()
        content = f"User prompt: {truncate(prompt, 300)}"
        log_type = "decision"

    elif event == "Stop":
        session_id = read_session_id()
        content = f"Turn {payload.get('turn', 0)} complete"
        log_type = "summary"

    elif event == "Notification":
        session_id = read_session_id()
        content = f"Notification: {truncate(msg, 200)}"
        log_type = "action"

    elif event == "PostLLMCall":
        session_id = read_session_id()
        reply = payload.get("toolResult", "") or payload.get("message", "")
        if reply:
            reply_preview = truncate(str(reply).replace(chr(10), " "), 200)
            content = f"Model output (turn {payload.get('turn', 0)}): {reply_preview}"
        else:
            content = f"Model output (turn {payload.get('turn', 0)})"
        log_type = "action"

    elif event == "PreCompact":
        session_id = read_session_id()
        content = f"Session compress ({payload.get('trigger', 'auto')})"
        log_type = "summary"

    elif event == "SubagentStop":
        session_id = read_session_id()
        content = "Sub-task done"
        log_type = "summary"

    elif event == "PermissionRequest":
        session_id = read_session_id()
        content = f"Permission request: {truncate(tool_name, 100)}"
        log_type = "action"

    else:
        session_id = read_session_id()
        content = f"Event {event}"
        log_type = "action"

    if not session_id:
        session_id = new_session_id()

    return level, log_type, content, session_id


def _auto_compress():
    """Trigger log compression if the helper script exists."""
    try:
        script = logs_dir() / "compress_logs.py"
        if script.exists():
            import subprocess
            subprocess.run(
                [sys.executable, str(script)],
                capture_output=True, timeout=30
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    try:
        if hasattr(sys.stdin, 'reconfigure'):
            sys.stdin.reconfigure(encoding='utf-8')
        raw = sys.stdin.read()
        if not raw.strip():
            return
        payload = json.loads(raw)
    except (json.JSONDecodeError, Exception):
        return

    event = payload.get("event", "")
    if not event:
        return

    # Forward all events to obsidian_writer
    forward_to_daemon(payload.copy())

    # PreToolUse → supervisor (request-response, may return violation)
    if event == "PreToolUse":
        sv_resp = forward_to_supervisor(payload.copy())
        if sv_resp and sv_resp.get("violated"):
            sv_msg = sv_resp.get("systemMessage", "")
            if sv_msg:
                # Emit systemMessage to stdout → hook system injects into AI context
                print(sv_msg, flush=True)
    # Other events → supervisor (fire-and-forget)
    elif event:
        forward_to_supervisor(payload.copy())

    # Fallback: write JSONL as backup
    level, log_type, content, session_id = format_content(event, payload)
    log_entry = {
        "ts": datetime.datetime.now().isoformat(),
        "level": level,
        "type": log_type,
        "content": content,
        "session": session_id,
        "event": event,
    }
    write_log_entry(session_id, log_entry)


if __name__ == "__main__":
    main()
