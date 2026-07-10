#!/usr/bin/env python3
"""
Reasonix Hook Logger v4.1 — 转发器模式 + JSONL 降级
=====================================================
从 stdin 读取 JSON payload，尝试转发给 obsidian_writer 守护进程 (TCP :49520)，
连接失败时自动降级到 JSONL 写入（保持原有逻辑）。
"""

import json
import sys
import datetime
import hashlib
import socket
from pathlib import Path

LOGS_DIR = Path.home() / ".reasonix" / "logs" / "sessions" / "raw"
SESSION_FILE = Path.home() / ".reasonix" / "logs" / ".current_session"
VAULT_PATH = Path(r"D:\个人数据\辞玖")

# ── 守护进程转发配置 ──
OBSIDIAN_WRITER_HOST = "127.0.0.1"
OBSIDIAN_WRITER_PORT = 49520
SUPERVISOR_HOST = "127.0.0.1"
SUPERVISOR_PORT = 49522

# .kw 文件的关键事件类型
_KW_TYPES = {"decision", "error", "milestone", "progress", "blocker", "resume", "write", "filecreate", "test"}



def read_session_id():
    if SESSION_FILE.exists():
        try:
            data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            return data.get("session_id")
        except (json.JSONDecodeError, KeyError):
            pass
    return None


def new_session_id():
    now = datetime.datetime.now()
    suffix = hashlib.md5(str(now.timestamp()).encode()).hexdigest()[:8]
    session_id = f"reasonix-{now.strftime('%Y%m%d%H%M%S')}-{suffix}"
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(
        json.dumps({"session_id": session_id}, ensure_ascii=False), encoding="utf-8"
    )
    return session_id


def clear_session():
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
    """写入 .jsonl 日志，同时为关键事件写 .kw（降级/后备路径）"""
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
        # Context injection now handled by supervisor
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


# ── TCP 转发 ──────────────────────────────────────────────────

def forward_to_daemon(payload: dict) -> bool:
    """将 payload 转发给 obsidian_writer 守护进程，成功返回 True"""
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
    将 payload 转发给 supervisor（请求-响应模式）。

    对 PreToolUse 事件，读取 supervisor 的 JSON 响应：
      - {"violated": False} → 通过
      - {"violated": True, "systemMessage": "..."} → 违规，返回完整响应

    对其他事件，fire-and-forget。
    返回 None 表示连接失败。
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

# ── 错误检测 ──────────────────────────────────────────────────

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


# ── 事件格式化（同 v4） ──────────────────────────────────────

def format_content(event, payload):
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
        # Context injection now handled by supervisor
        clear_session()
        session_id = new_session_id()
        content = f"会话开始 (工作目录: {payload.get('cwd', '')})"
        log_type = "resume"
        _auto_compress()

    elif event == "SessionEnd":
        session_id = read_session_id()
        content = "会话结束"
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
        content = f"调用工具 {tool_name} | 参数: {args_str}"
        log_type = "action"

    elif event == "PostToolUse":
        session_id = read_session_id()
        result_str = str(tool_result) if tool_result else ""
        if result_str and len(result_str) < 200:
            content = f"工具 {tool_name} → {result_str}"
        elif result_str:
            preview = truncate(result_str, 200)
            content = f"工具 {tool_name} → {preview}"
        else:
            content = f"工具 {tool_name} 执行完毕"
        is_real_error = looks_like_real_error(result_str)
        log_type = "error" if is_real_error else "action"

    elif event == "UserPromptSubmit":
        session_id = read_session_id()
        content = f"用户提问: {truncate(prompt, 300)}"
        log_type = "decision"

    elif event == "Stop":
        session_id = read_session_id()
        content = f"Turn {payload.get('turn', 0)} 完成"
        log_type = "summary"

    elif event == "Notification":
        session_id = read_session_id()
        content = f"通知: {truncate(msg, 200)}"
        log_type = "action"

    elif event == "PostLLMCall":
        session_id = read_session_id()
        reply = payload.get("toolResult", "") or payload.get("message", "")
        if reply:
            reply_preview = truncate(str(reply).replace(chr(10), " "), 200)
            content = f"模型输出 (turn {payload.get('turn', 0)}): {reply_preview}"
        else:
            content = f"模型输出 (turn {payload.get('turn', 0)})"
        log_type = "action"

    elif event == "PreCompact":
        session_id = read_session_id()
        content = f"会话压缩 ({payload.get('trigger', 'auto')})"
        log_type = "summary"

    elif event == "SubagentStop":
        session_id = read_session_id()
        content = "子任务完成"
        log_type = "summary"

    elif event == "PermissionRequest":
        session_id = read_session_id()
        content = f"权限请求: {truncate(tool_name, 100)}"
        log_type = "action"

    else:
        session_id = read_session_id()
        content = f"事件 {event}"
        log_type = "action"

    if not session_id:
        session_id = new_session_id()

    return level, log_type, content, session_id


def _auto_compress():
    try:
        script = Path.home() / ".reasonix" / "logs" / "compress_logs.py"
        if script.exists():
            import subprocess
            subprocess.run(
                [sys.executable, str(script)],
                capture_output=True, timeout=30
            )
    except Exception:
        pass


# ── 主入口 ────────────────────────────────────────────────────


def main():
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')
        if hasattr(sys.stdin, 'reconfigure'):
            sys.stdin.reconfigure(encoding='utf-8')
        raw = sys.stdin.read()
        if not raw.strip():
            return
        payload = json.loads(raw)
    except Exception:
        return

    event = payload.get("event", "")
    if not event:
        return

    # 转发到 obsidian_writer（全部事件）
    forward_to_daemon(payload.copy())

    # PreToolUse/UserPromptSubmit → supervisor（请求-响应，可返回 systemMessage）
    if event in ("PreToolUse", "UserPromptSubmit"):
        sv_resp = forward_to_supervisor(payload.copy())
        if sv_resp:
            sv_msg = sv_resp.get("systemMessage", "")
            if sv_msg:
                # emit systemMessage to stdout → hook系统自动注入下一回合AI上下文
                print(sv_msg, flush=True)
    # 其他事件 → supervisor（fire-and-forget）
    elif event:
        forward_to_supervisor(payload.copy())

    # 降级：写 JSONL 作为备份
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


_supervisor_started = False

def ensure_supervisor():
    """Start supervisor if not running. Checks port first to avoid duplicates."""
    import socket, subprocess, time
    # Check if already running
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", 49522))
        s.close()
        return True
    except:
        pass  # not running, start below
    
    sup_path = str(Path.home() / ".reasonix" / "bin" / "supervisor.exe")
    if not Path(sup_path).exists():
        return False
    try:
        subprocess.Popen([sup_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(5):
            time.sleep(0.5)
            try:
                s2 = socket.socket(); s2.settimeout(0.5)
                s2.connect(("127.0.0.1", 49522))
                s2.close()
                return True
            except: pass
        return False
    except:
        return False

if __name__ == "__main__":
    ensure_supervisor()
    main()









