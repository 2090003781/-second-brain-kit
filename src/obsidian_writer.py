#!/usr/bin/env python3
"""Obsidian Writer daemon - TCP 49520, writes Markdown to Obsidian vault."""

import json, socket, datetime, threading, os, sys, re, portalocker
from pathlib import Path

VAULT_PATH = Path(r"D:\个人数据\辞玖")
HOST = "127.0.0.1"
PORT = 49520

_open_files = {}
_open_lock = threading.Lock()
_current_topic = None
_topic_lock = threading.Lock()
_index_dirty = False
_index_last_rebuild = 0

# 会话级状态跟踪（用于热缓存）
_session_errors = []
_session_decisions = []
_session_prompts = []
_session_topics = set()
_session_lock = threading.Lock()

def _get_raw_handle(date):
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


def _get_topic_handle(topic_name):
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


def _close_handle(key):
    with _open_lock:
        fh = _open_files.pop(key, None)
        if fh and not fh.closed:
            fh.flush()
            fh.close()


def _write_locked(fh, file_path, text):
    try:
        portalocker.lock(fh, portalocker.LOCK_EX)
        fh.write(text)
        fh.flush()
        global _index_dirty
        _index_dirty = True
        os.fsync(fh.fileno())
    finally:
        try: portalocker.unlock(fh)
        except: pass


_TOPIC_PATTERN = re.compile(r"[─\-—]{2,}\s*话题分隔\s*[:：]\s*(.+?)\s*[─\-—]{2,}")

def _parse_topic_marker(text):
    if not text: return None
    m = _TOPIC_PATTERN.search(text)
    return m.group(1).strip() if m else None


_ERROR_PREFIXES = ("error:","error ","exception:","traceback","panic:","fatal:","syntaxerror","importerror","typeerror","valueerror","keyerror")

def _looks_like_error(text):
    if not text: return False
    lower = text.lower().strip()[:300].lstrip()
    return any(lower.startswith(p) for p in _ERROR_PREFIXES)

def process_event(payload):
    event = payload.get("event", "")
    if not event: return

    ts = payload.get("ts", datetime.datetime.now().isoformat())
    try: dt = datetime.datetime.fromisoformat(ts)
    except: dt = datetime.datetime.now()

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

    # detect topic marker
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
        if current_topic:
            with _session_lock:
                _session_topics.add(current_topic)

    # build raw timeline entry
    raw_label = event
    raw_line = ""

    if event == "SessionStart":
        session_id = payload.get("session_id") or session_id
        cwd = payload.get("cwd", "")
        model = payload.get("model", "")
        raw_label = "会话开始"
        raw_line = f"Session: {session_id}  |  cwd: {cwd}  |  model: {model}"
    elif event == "SessionEnd":
        raw_label = "会话结束"
        raw_line = f"Session: {session_id}"
        _write_hot_cache(session_id)
    elif event == "UserPromptSubmit":
        raw_label = "用户提问"
        raw_line = (prompt or "(empty)")[:300]
        if prompt:
            with _session_lock:
                _session_prompts.append(prompt[:120])
                if len(_session_prompts) > 5:
                    _session_prompts.pop(0)
    elif event == "PreToolUse":
        raw_label = "工具调用"
        args_preview = json.dumps(tool_args, ensure_ascii=False)[:200]
        raw_line = f"{tool_name} {args_preview}"
    elif event == "PostToolUse":
        is_err = _looks_like_error(str(tool_result)) if tool_result else False
        raw_label = "工具错误" if is_err else "工具结果"
        preview = str(tool_result)[:200] if tool_result else "(no return)"
        raw_line = f"{tool_name} -> {preview}"
        if is_err and tool_name:
            err_summary = f"{tool_name}: {preview[:100]}"
            with _session_lock:
                _session_errors.append(err_summary)
                if len(_session_errors) > 10:
                    _session_errors.pop(0)
    elif event == "Stop":
        turn = payload.get("turn", 0)
        raw_label = "Turn 完成"
        raw_line = f"Turn {turn}"
    elif event == "Checkpoint":
        raw_label = f"检查点 ({level})"
        raw_line = (content or "")[:300]
        if content:
            with _session_lock:
                _session_decisions.append(content[:120])
                if len(_session_decisions) > 8:
                    _session_decisions.pop(0)
    elif event == "PreCompact":
        raw_label = "上下文压缩"
        raw_line = f"trigger: {payload.get('trigger', 'auto')}"
    elif event == "Notification":
        raw_label = "通知"
        raw_line = (msg or "")[:300]
    elif event == "PostLLMCall":
        raw_label = "模型输出"
        reply = payload.get("toolResult", "") or payload.get("message", "")
        raw_line = str(reply).replace(chr(10), " ")[:200]
    elif event == "SubagentStop":
        raw_label = "子任务完成"
        raw_line = ""
    elif event == "PermissionRequest":
        raw_label = "权限请求"
        raw_line = tool_name
    else:
        raw_label = event
        raw_line = (content or "")[:300]

    # 1) write raw timeline
    fh_raw, raw_path = _get_raw_handle(date)
    if not raw_path.exists() or raw_path.stat().st_size == 0:
        _write_locked(fh_raw, raw_path, f"# {date_compact} 原始时间线\n\n")
    raw_md = f"## {timestamp} {raw_label}\n{raw_line}\n\n"
    _write_locked(fh_raw, raw_path, raw_md)

    # 2) write topic file if we have a current topic
    if not current_topic:
        return

    fh_topic, topic_path, safe_name = _get_topic_handle(current_topic)
    is_new = not topic_path.exists() or topic_path.stat().st_size == 0

    parts = []
    if is_new:
        parts.append(f"---\ncreated: {date_compact}\ntags: [reasonix/topic, reasonix/active]\nstatus: 活跃\n---\n\n# {current_topic}\n\n")
    parts.append(f"## {date_str} | 来自会话 {session_id}\n\n")

    if event == "SessionStart":
        cwd = payload.get("cwd", "")
        model = payload.get("model", "")
        parts.append(f"**会话开始** | cwd: {cwd} | model: {model}\n")
    elif event == "SessionEnd":
        parts.append("**→ 会话结束**\n")
        _close_handle(f"topic:{safe_name}")
    elif event == "UserPromptSubmit":
        parts.append(f"**用户提问:** {prompt}\n")
    elif event == "PreToolUse":
        args_preview = json.dumps(tool_args, ensure_ascii=False)[:300]
        parts.append(f"**工具调用:** {tool_name}\n  参数: {args_preview}\n")
    elif event == "PostToolUse":
        is_err = _looks_like_error(str(tool_result)) if tool_result else False
        preview = str(tool_result)[:300] if tool_result else "(无返回)"
        if is_err:
            parts.append(f"**❌ 工具错误** {tool_name}:\n  `\n{preview}\n  `\n")
            parts.append(f"→ ❌ 错误: {preview[:200]}\n")
        else:
            parts.append(f"**✅ 工具完成** {tool_name} → {preview}\n")
    elif event == "Stop":
        turn = payload.get("turn", 0)
        parts.append(f"**Turn {turn} 完成**\n")
    elif event == "Checkpoint":
        emoji_map = {"milestone":"🎯","progress":"📊","blocker":"🚧"}
        emoji = emoji_map.get(level, "📌")
        clean = re.sub(r'[─\-—]{2,}\s*话题分隔\s*[:：]\s*.+?\s*[─\-—]{2,}', '', content).strip()
        parts.append(f"**{emoji} 检查点 ({level}):** {clean}\n")
    elif event == "PreCompact":
        parts.append(f"**📦 上下文压缩** (触发: {payload.get('trigger', 'auto')})\n")
    elif event == "Notification":
        parts.append(f"**通知:** {msg}\n")
    elif event == "PostLLMCall":
        reply = payload.get("toolResult", "") or payload.get("message", "")
        preview = str(reply).replace(chr(10), " ")[:300]
        parts.append(f"**🤖 模型输出:** {preview}\n")
    elif event == "SubagentStop":
        parts.append("**🔄 子任务完成**\n")
    elif event == "PermissionRequest":
        parts.append(f"**🔒 权限请求:** {tool_name}\n")
    else:
        parts.append(f"**事件 {event}:** {(content or '')[:200]}\n")

    parts.append("\n---\n\n")
    _write_locked(fh_topic, topic_path, "".join(parts))

    if event == "SessionEnd":
        _close_handle(f"raw:{date.isoformat()}")

    # Rebuild knowledge index if vault was modified (throttled: max once per 30s)
    global _index_dirty, _index_last_rebuild
    if _index_dirty:
        import time as _time
        now = _time.time()
        if now - _index_last_rebuild > 30:
            _index_last_rebuild = now
            _index_dirty = False
            try:
                import subprocess
                idx = Path.home() / ".reasonix" / "logs" / "knowledge_indexer.py"
                subprocess.Popen([sys.executable, str(idx)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except: pass

def _write_hot_cache(session_id):
    try:
        hot_path = VAULT_PATH / "记忆" / "热缓存.md"
        hot_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        with _session_lock:
            errors = list(_session_errors[-5:])
            decisions = list(_session_decisions[-5:])
            prompts = list(_session_prompts[-3:])
            topics = list(_session_topics)
        parts = []
        parts.append(f"---\nupdated: {date_str}\ntags: [reasonix/hotcache]\n---\n\n")
        parts.append("# 热缓存\n\n")
        if topics:
            parts.append("## 当前活跃话题\n")
            for t in topics:
                safe = t.replace("[","").replace("]","")
                parts.append(f"- [[话题/{safe}]] — 活跃中\n")
            parts.append("\n")
        if decisions:
            parts.append("## 最近关键决策\n")
            for d in decisions[-3:]:
                parts.append(f"- {d}\n")
            parts.append("\n")
        if prompts:
            parts.append("## 待接续\n")
            for p in prompts:
                parts.append(f"- {p}\n")
            parts.append("\n")
        if errors:
            parts.append("## 最近错误\n")
            for e in errors[-3:]:
                parts.append(f"- {e}\n")
            parts.append("\n")
        body = "".join(parts)
        fh = open(hot_path, "w", encoding="utf-8")
        try:
            portalocker.lock(fh, portalocker.LOCK_EX)
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            try: portalocker.unlock(fh)
            except: pass
            fh.close()
        with _session_lock:
            _session_errors.clear()
            _session_decisions.clear()
            _session_prompts.clear()
            _session_topics.clear()
        print(f"[obsidian-writer] Hot cache written: {hot_path}", file=sys.stderr)
    except Exception as e:
        print(f"[obsidian-writer] Hot cache error: {e}", file=sys.stderr)


def handle_client(conn):
    try:
        data = conn.recv(65536)
        if not data: return
        payload = json.loads(data.decode("utf-8"))
        process_event(payload)
    except: pass
    finally:
        try: conn.close()
        except: pass


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
                try: fh.flush(); fh.close()
                except: pass
            _open_files.clear()
        server.close()


if __name__ == "__main__":
    main()
