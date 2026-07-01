#!/usr/bin/env python3
"""Reasonix Supervisor Daemon v2"""
import json, socket, datetime, threading, re, os, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
from pathlib import Path

def load_all_memory():
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
            _extract_rules(domain, rules_file.read_text(encoding="utf-8"))
        errors_file = domain_dir / "高频错误.md"
        if errors_file.exists():
            _extract_error_patterns(domain, errors_file.read_text(encoding="utf-8"))
    print(f"[supervisor] Loaded {len(_rules)} rules, {len(_error_patterns)} error patterns from {MEMORY_DIR}", file=sys.stderr)


def _extract_rules(domain, text):
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


def _extract_error_patterns(domain, text):
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
            freq = 0; sol = ""; phen = ""
        fm = re.match(r"\*\*次数：\*\*\s*(\d+)", line)
        if fm and cur: freq = int(fm.group(1))
        pm = re.match(r"\*\*现象：\*\*\s*(.+)", line)
        if pm and cur: phen = pm.group(1).strip()
        sm = re.match(r"\*\*解决：\*\*\s*(.+?)(?:\*\*来源|\Z)", line)
        if sm and cur: sol = sm.group(1).strip()
        sm2 = re.match(r"\*\*规则：\*\*\s*(.+)", line)
        if sm2 and cur: sol = sm2.group(1).strip()
    if cur:
        _error_patterns.append((domain, cur, freq, sol, phen))

def has_chinese(text):
    if not isinstance(text, str): return False
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f': return True
    return False

_PATH_LIKE_EXTS = frozenset({".py",".go",".md",".txt",".toml",".json",".yaml",".yml",".exe",".bat",".ps1",".sh",".csv",".xml",".ini",".cfg",".conf"})

def is_path_like(value):
    if not isinstance(value, str) or not value: return False
    if "\\" in value or "/" in value: return True
    return any(value.lower().endswith(ext) for ext in _PATH_LIKE_EXTS)

def iter_arg_values(tool_args):
    if not isinstance(tool_args, dict): return
    for key, value in tool_args.items():
        yield key, value
        if isinstance(value, dict): yield from iter_arg_values(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict): yield from iter_arg_values(item)
                else: yield None, item

def _detect_hardcoded_rules(tool_name, tool_args):
    tool_lower = tool_name.lower()
    all_values = list(iter_arg_values(tool_args))
    if any(kw in tool_lower for kw in ["echo","bash","powershell","shell","cmd"]):
        for _, val in all_values:
            if has_chinese(val):
                return {"rule":"全局高频错误 #1 — GBK 编码冲突","detail":"命令/脚本参数含中文字符","solution":"将中文路径用变量传递；显式 encoding='utf-8'","domain":"全局"}
    if any(kw in tool_lower for kw in ["read_file","write_file","edit_file","glob","grep","move_file","copy"]):
        for _, val in all_values:
            if has_chinese(val) and is_path_like(val):
                return {"rule":"全局高频错误 #1 — 中文路径","detail":f"路径含中文字符","solution":"确认文件系统编码为 UTF-8","domain":"全局"}
    if tool_lower == "write_file":
        path_arg = tool_args.get("path","")
        if isinstance(path_arg,str) and path_arg.strip():
            if any(path_arg.lower().endswith(ext) for ext in [".toml",".json",".yaml",".yml"]):
                return {"rule":"全局规则 — 修改配置文件前应先备份","detail":f"直接写入 {path_arg}","solution":f"先用 Copy-Item 备份","domain":"全局"}
    if tool_lower in ("bash","powershell","shell"):
        for _, val in all_values:
            val_str = str(val).lower()
            if "go build" in val_str and "cmd" not in val_str:
                return {"rule":"编程高频错误 #4 — 编译目录错误","detail":"go build 未在 cmd/reasonix 下执行","solution":"cd cmd/reasonix 再 go build","domain":"编程"}
            if "reasonix" in val_str and "bot start" in val_str:
                if "Start-Process" not in val_str and "start /B" not in val_str and "-NoNewWindow" not in val_str:
                    return {"rule":"QQ Bot 高频错误 #5 — 进程保持超时","detail":"Bot 在前台运行","solution":"使用 Start-Process 后台启动","domain":"QQ Bot"}
    return None


def _detect_loop(tool_name, tool_args):
    global _tool_fail_tracker
    args_str = json.dumps(tool_args, ensure_ascii=False)
    now = datetime.datetime.now()
    tracker = _tool_fail_tracker
    if tool_name not in tracker:
        tracker[tool_name] = {"count": 0, "first_seen": now, "last_seen": now}
        return None
    t = tracker[tool_name]
    if (now - t["last_seen"]).total_seconds() > 300:
        t["count"] = 0; t["first_seen"] = now; t["count"] = 1; t["first_seen"] = now; t["last_seen"] = now
        return None
    # Skip loop detection for Bot log writes
    arg_str = json.dumps(tool_args, ensure_ascii=False)
    if "\u65e5\u5fd7.md" in arg_str or "Bot" in arg_str:
        t["count"] = 0
        t["first_seen"] = now
        t["last_seen"] = now
        return None
    t["count"] += 1
    t["last_seen"] = now
    if t["count"] >= _FAIL_THRESHOLD:
        t["count"] = 0
        return {"rule":"检测到工具调用死循环","detail":f"{tool_name} 连续执行 {_FAIL_THRESHOLD} 次","solution":"换个思路","domain":"全局"}
    return None


def check_tool_call(tool_name, tool_args):
    loop_hit = _detect_loop(tool_name, tool_args)
    if loop_hit: return _build_violation(tool_name, loop_hit)
    hit = _detect_hardcoded_rules(tool_name, tool_args)
    if hit: return _build_violation(tool_name, hit)
    args_str = json.dumps(tool_args, ensure_ascii=False)
    tool_and_args = f"{tool_name} {args_str}".lower()
    for domain, error_name, freq, solution, phenomenon in _error_patterns:
        keywords = re.findall(r'[\u4e00-\u9fff\w]+', phenomenon + " " + error_name)
        keywords = [w for w in keywords if len(w) >= 2]
        if any(kw.lower() in tool_and_args for kw in keywords):
            hit = {"rule":f"{domain}高频错误 — {error_name}（已出现 {freq} 次）","detail":f"匹配关键词: {phenomenon[:100]}","solution":solution or "参见记忆文件","domain":domain}
            return _build_violation(tool_name, hit)
    for domain, rule_name, rule_text in _rules:
        if "删除" in rule_text and "说明" in rule_text:
            if tool_name in ("delete_range","delete_symbol","move_file"):
                hit = {"rule":f"{domain}规则 — {rule_name}","detail":"删除操作需先说明影响","solution":"先向用户说明","domain":domain}
                return _build_violation(tool_name, hit)
    return None


def _build_violation(tool_name, hit):
    return {"violated":True,"rule":hit["rule"],"detail":hit.get("detail",""),"solution":hit.get("solution",""),"domain":hit.get("domain",""),
        "systemMessage":(
            "⚠️ **Supervisor 检测到规则违规**\n\n"
            f"- **工具：** {tool_name}\n"
            f"- **规则：** {hit['rule']}\n"
            f"- **详情：** {hit.get('detail','')}\n"
            f"- **建议：** {hit.get('solution','')}\n"
            f"- **来源：** 记忆/{hit.get('domain','?')}/高频错误.md"
        )}


def append_supervision_log(violation, payload):
    try:
        now = datetime.datetime.now()
        SUPERVISION_LOG.parent.mkdir(parents=True, exist_ok=True)
        is_new = not SUPERVISION_LOG.exists() or SUPERVISION_LOG.stat().st_size == 0
        tool_name = payload.get("toolName","")
        tool_args = payload.get("toolArgs",{})
        parts = []
        if is_new:
            parts.append(f"# 监督日志\n\n创建于 {now.strftime('%Y-%m-%d %H:%M')}\n来源: supervisor.py (TCP :{SUPERVISOR_PORT})\n\n## 格式\n每条记录：时间 / 工具 / 违反规则 / 解决方案\n\n---\n\n")
        parts.append(f"## 🚨 {now.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        parts.append(f"- **事件：** PreToolUse\n")
        parts.append(f"- **工具：** {tool_name}\n")
        parts.append(f"- **参数：** `json\n{json.dumps(tool_args, ensure_ascii=False, indent=2)[:300]}\n`\n")
        parts.append(f"- **违反规则：** {violation.get('rule','未知')}\n")
        parts.append(f"- **详情：** {violation.get('detail','')}\n")
        parts.append(f"- **解决方案：** {violation.get('solution','')}\n")
        parts.append("\n---\n\n")
        with open(SUPERVISION_LOG, "a", encoding="utf-8") as f:
            f.write("".join(parts))
    except Exception as e:
        print(f"[supervisor] Failed to write supervision log: {e}", file=sys.stderr)


def _read_hot_cache():
    try:
        if not HOT_CACHE_PATH.exists():
            return ""
        text = HOT_CACHE_PATH.read_text(encoding="utf-8")
        lines = text.split(chr(10))
        cnt = 0
        body_start = 0
        for i, line in enumerate(lines):
            if line.strip() == "---":
                cnt += 1
                if cnt == 2:
                    body_start = i + 1
                    break
        return chr(10).join(lines[body_start:]).strip()
    except Exception as e:
        print(f"[supervisor] Hot cache read error: {e}", file=sys.stderr)
        return ""


def _kw_match(text):
    if not text:
        return False
    low = text.lower()
    for kw in _HOT_CACHE_TRIGGER_KW:
        if kw.lower() in low:
            return True
    return False


def handle_client(conn):
    try:
        data = conn.recv(65536)
        if not data:
            return
        payload = json.loads(data.decode("utf-8"))
        event = payload.get("event", "")

        # UserPromptSubmit: detect hot cache keywords
        if event == "UserPromptSubmit":
            prompt = payload.get("prompt", "")
            if _kw_match(prompt):
                hot = _read_hot_cache()
                if hot:
                    sv = "📋 **热缓存（跨会话记忆）**\n\n" + hot + "\n\n---\n*以上是上一会话结束时保存的热缓存内容，供参考接续。*"
                    conn.sendall(json.dumps({"violated": False, "systemMessage": sv}, ensure_ascii=False).encode("utf-8"))
                    return
            conn.sendall(json.dumps({"violated": False}).encode("utf-8"))
            return

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
    print(f"[supervisor]    Rules: GBK编码 | 中文路径 | 配置备份 | 编译目录 | Bot进程保持 | 死循环 | +记忆文件匹配", file=sys.stderr)
    print(f"[supervisor]    Hot cache: {HOT_CACHE_PATH}", file=sys.stderr)
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

