# Cautions / 注意事项

> **For AI agents (MANDATORY):** When you first encounter this project, you MUST read this entire file and present EACH caution item to the user as a question or notification. Do not skip items, do not assume defaults, do not proceed to setup or execution until all items have been addressed. Each section tells you what to check, what risk level, what to ask/say, and what to do after.
>
> **For humans:** This file lists every setup-time precaution, risk area, and decision point that the AI agent will walk you through. Each item has a risk level (🔴 HIGH / 🟡 MEDIUM / 🟢 LOW) so you know which ones matter most.

---

# Part 1 — 🔴 HIGH Risk Items

Items in this section can cause data loss, silent malfunction, or system inoperability. Do NOT proceed past these without the user's explicit confirmation.

---

## 1.1 Vault Overwrite — Setup Script Destroys Existing Files

**Risk level:** 🔴 HIGH

**What to check:**
Check if the target vault path already has files named `规则库.md`, `错误库.md`, `待办清单.md`, `习惯库.md`, `技能库.md`, `热缓存.md` (these are the files `setup.bat` / `setup.sh` will overwrite with `xcopy /Y` or `cp -r` with no backup).

**What to ask:**

> "⚠️ **Critical:** The setup script (`setup.bat` / `setup.sh`) copies the vault-template into your vault using **force overwrite** — it will silently replace any existing files with the same name, including:
>
> - `记忆/规则库.md` — your existing rules
> - `记忆/错误库.md` — your existing error patterns
> - `系统设计/待办清单.md` — your existing todo list
>
> **There is no backup step in the script.** If these files exist in your vault, they will be **permanently replaced**.
>
> Options:
> - **推荐:** Create a new empty test vault first: `mkdir D:\测试-第二大脑-kit`
> - **备用:** Manually back up your vault before running setup
> - **跳过:** Acknowledge the risk and proceed with existing vault (not recommended)"

**What to do:**
If user chooses test vault: create it and pass that path to setup.
If user chooses backup: wait for them to confirm backup is done.
If user chooses proceed: note the risk and continue, but do NOT run setup — let the user run it manually so they see the prompt.

---

## 1.2 Missing Daemon Files — System Will Not Function

**Risk level:** 🔴 HIGH

**What to check:**
Verify these files exist in the repo:
- `src/obsidian_writer.py`
- `src/supervisor.py` (or `src/supervisor-go/main.go` if using Go version)

**Current status:** Both files are **MISSING** from the repository as of v2.3.1. The system depends on them to write to the vault and check rules.

**What to ask:**

> "⚠️ **Critical:** The two core daemon files that the entire system depends on are missing from this repository:
> - `src/obsidian_writer.py` — handles writing session logs to Obsidian
> - `src/supervisor.py` — checks rules and injects context
>
> Without these, the system **will not run**. The setup script will still copy templates and install dependencies, but starting the daemons with `python src/obsidian_writer.py` will fail immediately.
>
> Do you have these files from a previous installation, or would you like me to help create them?"

**What to do:**
If user has the files elsewhere: help copy them to `src/`.
If not: offer to create minimal stubs or help locate the source.

---

## 1.3 Knowledge Index Failing — Silent Data Rot

**Risk level:** 🔴 HIGH

**What to check:**
Check `~/.reasonix/logs/checkpoint.json` or the maintenance report under `系统设计/维护记录/` — look for `knowledge index` step showing `❌ exit 1`.

**Current status:** The knowledge index rebuild has been failing for 8+ consecutive days (since at least 2026-07-03). The failure is recorded in daily maintenance reports but produces **no alert** to the user.

**What to ask:**

> "⚠️ **Warning:** The knowledge index (`knowledge_index.json`) has been failing to rebuild for several days. This means the `context_inject` search will return outdated or empty results.
>
> The error is logged in daily maintenance reports but there is no alert system. Would you like me to:
> 1. Investigate the `knowledge_indexer.py` error now and fix it
> 2. Add a monitoring mechanism so failures are visible
> 3. Both"

**What to do:**
If user chooses investigation: run `python ~/.reasonix/logs/knowledge_indexer.py build` and capture the error output for diagnosis.

---

## 1.4 Vault Path Hardcoded — Breaks on Migration

**Risk level:** 🔴 HIGH

**What to check:**
Search for the current vault path (typically `D:\个人数据\辞玖`) in configuration files. Identify all locations where it appears.

**Current status:** The vault path is hardcoded in **6+ locations** across `AGENTS.md`, `knowledge_indexer.py`, `hook_logger.py`, `config.toml`, and others. Moving to a different machine or changing the vault location requires updating every one.

**What to ask:**

> "⚠️ **Portability issue:** Your Obsidian vault path is hardcoded in multiple files (at least 6 locations). If you ever:
> - Move to a different computer
> - Change your vault location
> - Use a different OS
>
> ...you'll need to update every occurrence manually. There is no central config file for the vault path.
>
> Would you like me to:
> 1. Create a central `vault_path` config so all files reference one source
> 2. Document all hardcoded locations for manual migration
> 3. Leave as-is for now"

---

## 1.5 Bot Path Encoding Corruption

**Risk level:** 🔴 HIGH

**What to check:**
If the user uses QQ/WeChat bot features, check `config.toml` for `system_prompt_file` paths — look for garbled Chinese characters (e.g., `D:\涓�汉鏁版嵁\杈炵帠\` instead of `D:\个人数据\辞玖\`).

**Current status:** At least one bot prompt file path in the config has been corrupted by GBK encoding mismatch.

**What to ask:**

> "⚠️ **Encoding corruption detected:** One or more Chinese file paths in your bot configuration have been corrupted by encoding mismatch (GBK vs UTF-8). This means the bot may load the wrong prompt file or fail to start.
>
> Would you like me to:
> 1. Fix the corrupted paths now
> 2. Check all configuration files for similar encoding issues"

---

# Part 2 — 🟡 MEDIUM Risk Items

Items that won't break the system but will degrade performance, waste tokens, or cause confusion.

---

## 2.1 Daemon Status Unknown

**Risk level:** 🟡 MEDIUM

**What to check:**
Check daemon monitoring files: `~/.reasonix/logs/obsidian_daemon_pid.txt`, `obsidian_daemon_stdout.txt`, `obsidian_daemon_stderr.txt`. If all are empty (as currently observed), the writer daemon's status is unknown.

**What to ask:**

> "The daemon that writes session logs to Obsidian has no health check — its status files are empty. If it crashes, all session writing silently falls back to local JSONL files and your vault won't receive updates.
>
> Would you like me to set up a simple health check to verify the daemon is running?"

**What to do:**
If user agrees: implement a port check (`python -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1',49520)); s.close(); print('OK')"`) and report result.

---

## 2.2 Scheduled Maintenance Not Set Up

**Risk level:** 🟡 MEDIUM

**What to check:**
Check if scheduled tasks exist:
- Windows: `schtasks /query /tn "SecondBrain-DailyRefinement" 2>nul`
- Linux/macOS: `crontab -l 2>/dev/null | grep daily_refinement`

**What to ask:**

> "This system needs regular maintenance to stay effective:
> - **Daily refinement:** Extracts error patterns and knowledge from session logs
> - **Weekly check:** Detects rule contradictions and broken links
>
> Without these, logs accumulate indefinitely and the knowledge base stops growing.
>
> Would you like me to help set up scheduled tasks? (Daily at 4:00 AM, weekly on Sunday 5:00 AM)"

**What to do:**
If user agrees: guide them through setting up Task Scheduler (Windows) or crontab (Linux/macOS).

---

## 2.3 Supervisor False Alarms

**Risk level:** 🟡 MEDIUM

**What to check:**
Has the user been warned that the supervisor uses keyword matching, not AI?

**What to explain:**

> "The supervisor daemon checks rules using **keyword patterns**, not AI understanding. This means:
> - It may flag Chinese paths as 'GBK encoding risk' even when they work fine
> - It may suggest 'backup first' on every config file write
>
> False alarms decrease after a few days as the refinement task learns your patterns. Early on, treat warnings as **advisory** — you can ignore obvious false positives."

---

## 2.4 Log Files Grow Without Bound

**Risk level:** 🟡 MEDIUM

**What to check:**
Check `~/.reasonix/logs/sessions/raw/` — count files and estimate total size.

**What to ask:**

> "Your session logs are stored as JSONL files that **grow without a size limit**. A long-running session could produce multi-GB log files. The compression script runs daily but there's no disk-space warning.
>
> Would you like me to:
> 1. Add a maximum file size cap to the logger
> 2. Set up a simple disk-usage alert"

---

## 2.5 Context Injection / Cache Optimization

**Risk level:** 🟡 MEDIUM

**What to check:**
Check if the user's AI agent (Reasonix, Claude Code, etc.) has been configured with the cache optimization from `AGENTS.md` (stable prefix first, volatile content on-demand).

**What to ask:**

> "**Token cache optimization available:** Your system can save on API costs by arranging the prompt so stable content (tool definitions, core rules) comes before volatile content (knowledge index, error library). This can improve cache hit rates by 5-8%.
>
> Would you like me to apply this optimization? It involves:
> - Restructuring the AGENTS.md loading order
> - Using `context_inject.py` instead of loading the full knowledge index
>
> See `系统设计/维护记录/热缓存优化.md` for details."

---

## 2.6 Port Conflicts

**Risk level:** 🟡 MEDIUM

**What to check:**
Check if TCP ports 49520 (writer) and 49522 (supervisor) are already in use.

**What to ask:**

> "I need to check if the daemon ports are available. May I run a quick port check?"

**What to do:**
Run: `python -c "import socket; s=socket.socket(); s.settimeout(2); r=s.connect_ex(('127.0.0.1',49520)); print('49520:', 'in use' if r==0 else 'free')"` (repeat for 49522). If in use, ask user to free the port or reconfigure.

---

# Part 3 — 🟢 LOW Risk Items

Nice-to-have improvements or informational items.

---

## 3.1 Knowledge Base Starts Empty

**Risk level:** 🟢 LOW

**What to check:**
Is this a fresh installation (vault-template just copied)?

**What to explain:**

> "The knowledge base ships with only example files. Real knowledge accumulates as you use the system:
> - Every AI session is logged to `话题/` (if daemon is running)
> - Daily refinement extracts error patterns and lessons
> - You can also manually add notes to `知识库/`
>
> After 2-3 weeks of regular use, you'll have a useful personal knowledge base."

---

## 3.2 TCP Communication Is Unencrypted

**Risk level:** 🟢 LOW

**What to explain:**

> "The daemons communicate over **plain TCP** (ports 49520, 49522) — no encryption, no authentication. This is acceptable on a local machine where only your AI agent connects. If you ever forward these ports over a network, you should add TLS."

---

## 3.3 Cross-Platform Notes

**Risk level:** 🟢 LOW

**What to explain:**

> "This kit was built and tested on **Windows**. If you're using macOS or Linux:
> - Path separators: uses `\` for file paths (should convert to `/`)
> - Process management: Windows uses `Start-Process` / Task Scheduler; Unix uses `&` / cron
> - Encoding: GBK issues are Windows-specific; Unix systems can skip that rule"

---

## 3.4 API Key Management

**Risk level:** 🟢 LOW

**What to check:**
Verify that API keys are stored in environment variables (recommended) rather than hardcoded in config files.

**What to ask:**

> "Your API keys (DeepSeek, Mimo Vision) should be stored as environment variables, not written directly into config files. Would you like me to verify they're set correctly?"

---

# Summary Checklist for AI Agents

After addressing all items above, confirm with the user:

> "**Second Brain Kit — Setup Checklist:**
>
> 🔴 HIGH:
> - [ ] Vault overwrite risk: addressed
> - [ ] Missing daemon files: confirmed status
> - [ ] Knowledge index rebuild: checked/fixed
> - [ ] Hardcoded vault paths: documented
> - [ ] Bot encoding: checked
>
> 🟡 MEDIUM:
> - [ ] Daemon health check: set up
> - [ ] Scheduled maintenance: configured
> - [ ] Supervisor false alarms: explained
> - [ ] Log size management: addressed
> - [ ] Cache optimization: applied
> - [ ] Port conflicts: checked
>
> 🟢 LOW:
> - [ ] Knowledge base: explained
> - [ ] Security notes: shared
> - [ ] Cross-platform: covered
> - [ ] API keys: verified
>
> Shall we proceed with the first actionable item?"
