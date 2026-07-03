package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

// ── Config ──
var (
	vaultPath      string
	port           string
	writerPort     string
	botSessionsDir string
	supervisionLog string
	memoryDir      string
	qqLogPath      string
	wxLogPath      string
	stateFile      string
	ownLogFile     string
)

func initPaths() {
	memoryDir = filepath.Join(vaultPath, "记忆")
	supervisionLog = filepath.Join(vaultPath, "监督日志.md")
	qqLogPath = filepath.Join(vaultPath, "个人", "Bot", "QQ-Bot", "日志.md")
	wxLogPath = filepath.Join(vaultPath, "个人", "Bot", "微信-Bot", "日志.md")
	ownLogFile = filepath.Join(os.Getenv("USERPROFILE"), ".reasonix", "logs", "supervisor_run.log")
}

var logMu sync.Mutex

func writeLog(format string, args ...any) {
	logMu.Lock()
	defer logMu.Unlock()
	msg := fmt.Sprintf("[%s] %s", time.Now().Format("15:04:05"), fmt.Sprintf(format, args...))
	f, err := os.OpenFile(ownLogFile, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err == nil {
		f.WriteString(msg + "\n")
		f.Close()
	}
}

// ═══════════════════════════════════════════════════════════════════════════════
// 1️⃣ 置信度 + 分级规则
// ═══════════════════════════════════════════════════════════════════════════════

type Confidence int

const (
	ConfSilent Confidence = iota // <40%, silently ignore
	ConfLow                     // 40-65%, log only, no warning
	ConfMedium                  // 65-85%, mild warning
	ConfHigh                    // >85%, active warning + systemMessage
)

// supervisorRuleConfig is loaded from 记忆/监督规则.md
type SupervisorRuleConfig struct {
	Enabled    bool
	Confidence Confidence
	Threshold  int  // for loop-detection
}

var supervisorRules = map[string]*SupervisorRuleConfig{}
var srMu sync.RWMutex

func loadSupervisorRules(vaultPath string) {
	path := filepath.Join(vaultPath, "记忆", "监督规则.md")
	data, err := os.ReadFile(path)
	if err != nil {
		writeLog("supervisor rules: %v", err)
		return
	}
	newRules := map[string]*SupervisorRuleConfig{}
	currentID := ""
	var currentCfg *SupervisorRuleConfig
	lines := strings.Split(string(data), "\n")
	for _, line := range lines {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "## ") && !strings.HasPrefix(line, "## #") {
			if currentID != "" && currentCfg != nil {
				newRules[currentID] = currentCfg
			}
			currentID = strings.TrimPrefix(line, "## ")
			currentCfg = &SupervisorRuleConfig{Enabled: true, Confidence: ConfMedium, Threshold: 3}
			continue
		}
		if currentCfg == nil {
			continue
		}
		if strings.Contains(line, "启用:") {
			currentCfg.Enabled = !strings.Contains(line, "false")
		} else if strings.Contains(line, "置信度:") {
			switch {
			case strings.Contains(strings.ToLower(line), "high"):
				currentCfg.Confidence = ConfHigh
			case strings.Contains(strings.ToLower(line), "medium"):
				currentCfg.Confidence = ConfMedium
			case strings.Contains(strings.ToLower(line), "low"):
				currentCfg.Confidence = ConfLow
			case strings.Contains(strings.ToLower(line), "silent") || strings.Contains(strings.ToLower(line), "off"):
				currentCfg.Confidence = ConfSilent
			}
		} else if strings.Contains(line, "阈值:") {
			parts := strings.Split(line, "阈值:")
			if len(parts) > 1 {
				if n, err := strconv.Atoi(strings.TrimSpace(parts[1])); err == nil && n > 0 {
					currentCfg.Threshold = n
				}
			}
		}
	}
	if currentID != "" && currentCfg != nil {
		newRules[currentID] = currentCfg
	}
	srMu.Lock()
	supervisorRules = newRules
	srMu.Unlock()
	writeLog("supervisor rules: loaded %d configs", len(newRules))
}

func getRuleConfig(ruleID string) *SupervisorRuleConfig {
	srMu.RLock()
	defer srMu.RUnlock()
	return supervisorRules[ruleID]
}

type RuleDef struct {
	ID         string
	Category   string // "shell" | "filesystem" | "sequence" | "error"
	Detect func(toolName string, args map[string]any) (matched bool, detail string)
	Confidence Confidence
	Solution   string
}

var builtinRules = []RuleDef{
	{
		ID: "backup-before-overwrite", Category: "filesystem",
		Confidence: ConfHigh,
		Solution:   "Backup config files before overwriting",
		Detect: func(tn string, args map[string]any) (bool, string) {
			if strings.ToLower(tn) != "write_file" {
				return false, ""
			}
			p, ok := args["path"].(string)
			if !ok {
				return false, ""
			}
			ext := strings.ToLower(filepath.Ext(p))
			if ext == ".toml" || ext == ".json" || ext == ".yaml" || ext == ".yml" {
				return true, fmt.Sprintf("Writing %s without backup", filepath.Base(p))
			}
			return false, ""
		},
	},
	{
		ID: "gbk-encoding", Category: "shell",
		Confidence: ConfMedium,
		Solution:   "Set encoding=utf-8 or pass Chinese strings via env var",
		Detect: func(tn string, args map[string]any) (bool, string) {
			tl := strings.ToLower(tn)
			if tl != "bash" && tl != "echo" && tl != "powershell" && tl != "cmd" {
				return false, ""
			}
			for _, v := range args {
				if s, ok := v.(string); ok && hasChinese(s) && !isVaultPath(s) {
					return true, "Command contains CJK characters"
				}
			}
			return false, ""
		},
	},
	{
		ID: "chinese-path", Category: "filesystem",
		Confidence: ConfLow,
		Solution:   "Use ASCII paths or verify file encoding",
		Detect: func(tn string, args map[string]any) (bool, string) {
			tl := strings.ToLower(tn)
			if tl != "read_file" && tl != "write_file" && tl != "edit_file" && tl != "glob" && tl != "grep" {
				return false, ""
			}
			p, ok := args["path"].(string)
			if ok && hasChinese(p) && !isVaultPath(p) {
				return true, fmt.Sprintf("CJK path: %s", filepath.Base(p))
			}
			return false, ""
		},
	},
	{
		ID: "loop-detection", Category: "sequence",
		Confidence: ConfHigh,
		Solution:   "Change approach or check preconditions first",
		Detect: func(tn string, args map[string]any) (bool, string) {
			if checkLoop(tn) {
				thresh := 3
			if cfg2 := getRuleConfig("loop-detection"); cfg2 != nil {
				thresh = cfg2.Threshold
			}
			return true, fmt.Sprintf("%s called %d+ times consecutively", tn, thresh)
			}
			return false, ""
		},
	},
}

// ═══════════════════════════════════════════════════════════════════════════════
// 2️⃣ 动态白名单（10 分钟滑动窗口）
// ═══════════════════════════════════════════════════════════════════════════════

type rateEntry struct {
	count     int
	firstSeen time.Time
}

var (
	rateLimiters = make(map[string]*rateEntry)
	rateMu       sync.Mutex
)

func checkRateLimit(ruleID string) int {
	rateMu.Lock()
	defer rateMu.Unlock()

	now := time.Now()
	e, ok := rateLimiters[ruleID]
	if !ok {
		rateLimiters[ruleID] = &rateEntry{count: 1, firstSeen: now}
		return 1
	}
	// Reset if outside 10-min window
	if now.Sub(e.firstSeen) > 10*time.Minute {
		e.count = 1
		e.firstSeen = now
		return 1
	}
	e.count++
	return e.count
}

// ═══════════════════════════════════════════════════════════════════════════════
// 3️⃣ 延迟裁决队列（等待 1-2 步看 AI 是否自纠正）
// ═══════════════════════════════════════════════════════════════════════════════

type pendingJudgment struct {
	RuleID     string
	ToolName   string
	Detail     string
	Solution   string
	Confidence Confidence
	CreatedAt  time.Time
	PrevTool   string
}

var (
	pendingQueue   []*pendingJudgment
	pendingMu      sync.Mutex
)

func enqueueDelayed(v *pendingJudgment) {
	pendingMu.Lock()
	defer pendingMu.Unlock()
	pendingQueue = append(pendingQueue, v)
}

func checkDelayedCorrection(toolName string, args map[string]any) *pendingJudgment {
	pendingMu.Lock()
	defer pendingMu.Unlock()

	if len(pendingQueue) == 0 {
		return nil
	}

	tl := strings.ToLower(toolName)
	now := time.Now()
	var resolved []int

	for i, pj := range pendingQueue {
		if now.Sub(pj.CreatedAt) > 30*time.Second {
			resolved = append(resolved, i)
			continue
		}
		// Check if AI self-corrected (added try/except, checked preconditions, etc.)
		if isSelfCorrectingAction(tl, args) {
			writeLog("delayed: self-correct detected for %s, cancelling warning", pj.RuleID)
			resolved = append(resolved, i)
			continue
		}
		// If AI moved on to a different tool, issue the pending warning
		if tl != strings.ToLower(pj.ToolName) && tl != pj.PrevTool {
			result := pj
			// Remove from queue
			resolved = append(resolved, i)
			return result
		}
	}

	// Remove resolved entries
	if len(resolved) > 0 {
		var kept []*pendingJudgment
		for i, pj := range pendingQueue {
			skip := false
			for _, ri := range resolved {
				if i == ri {
					skip = true
					break
				}
			}
			if !skip {
				kept = append(kept, pj)
			}
		}
		pendingQueue = kept
	}

	return nil
}

func isSelfCorrectingAction(toolName string, args map[string]any) bool {
	tl := strings.ToLower(toolName)
	// Check for try-except, defer, backup, encoding specifiers
	for _, v := range args {
		if s, ok := v.(string); ok {
			sl := strings.ToLower(s)
			if strings.Contains(sl, "try") || strings.Contains(sl, "except") ||
				strings.Contains(sl, "defer") || strings.Contains(sl, "encoding=utf-8") ||
				strings.Contains(sl, "encoding='utf-8'") || strings.Contains(sl, ".bak") ||
				strings.Contains(sl, "backup") {
				return true
			}
		}
	}
	// Check if it's a "check preconditions" tool
	if tl == "read_file" || tl == "stat" || tl == "exists" || tl == "ls" || tl == "glob" {
		return true
	}
	return false
}

// ═══════════════════════════════════════════════════════════════════════════════
// 4️⃣ 反馈解析 — 从 AI 回复中提取误报标注
// ═══════════════════════════════════════════════════════════════════════════════

var feedbackPattern = regexp.MustCompile(`<!--\s*audit_feedback:\s*(false_positive|true_positive|ignore)\s*(?::\s*(.+?))?\s*-->`)

func parseFeedback(text string) (kind, ruleHint string) {
	m := feedbackPattern.FindStringSubmatch(text)
	if len(m) > 1 {
		kind = m[1]
		if len(m) > 2 {
			ruleHint = strings.TrimSpace(m[2])
		}
	}
	return
}

// ═══════════════════════════════════════════════════════════════════════════════
// Tools
// ═══════════════════════════════════════════════════════════════════════════════

func isVaultPath(s string) bool {
	abs, _ := filepath.Abs(s)
	return strings.Contains(abs, vaultPath) || strings.Contains(abs, "个人数据\\辞玖")
}

func hasChinese(s string) bool {
	for _, r := range s {
		if r >= 0x4e00 && r <= 0x9fff {
			return true
		}
	}
	return false
}

type toolTracker struct {
	count    int
	lastSeen time.Time
}

var (
	toolTrackers = make(map[string]*toolTracker)
	loopMu       sync.Mutex
)

func checkLoop(toolName string) bool {
	// Get configurable threshold from vault (default 3)
	threshold := 3
	if cfg := getRuleConfig("loop-detection"); cfg != nil {
		threshold = cfg.Threshold
	}

	loopMu.Lock()
	defer loopMu.Unlock()
	t, ok := toolTrackers[toolName]
	if !ok {
		toolTrackers[toolName] = &toolTracker{count: 1, lastSeen: time.Now()}
		return false
	}
	if time.Since(t.lastSeen) > 5*time.Minute {
		t.count = 1
		t.lastSeen = time.Now()
		return false
	}
	t.count++
	t.lastSeen = time.Now()
	if t.count >= threshold {
		t.count = 0
		return true
	}
	return false
}

// ── Rules loading ──

type Rule struct {
	Domain  string
	Keyword string
}

type ErrorPattern struct {
	Domain   string
	Name     string
	Keywords []string
	Solution string
}

var (
	rules         []Rule
	errorPatterns []ErrorPattern
	ruleMu        sync.RWMutex
)

func loadRules() {
	ruleMu.Lock()
	defer ruleMu.Unlock()
	rules = nil
	errorPatterns = nil
	entries, err := os.ReadDir(memoryDir)
	if err != nil {
		writeLog("cannot read memory dir: %v", err)
		return
	}
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		domain := entry.Name()
		loadRuleFile(filepath.Join(memoryDir, domain, "规则.md"), domain)
		loadErrorFile(filepath.Join(memoryDir, domain, "高频错误.md"), domain)
	}
	writeLog("loaded %d rules, %d error patterns from memory", len(rules), len(errorPatterns))
}

func loadRuleFile(path, domain string) {
	data, err := os.ReadFile(path)
	if err != nil {
		return
	}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "- **") {
			rules = append(rules, Rule{Domain: domain, Keyword: line})
		}
	}
}

func loadErrorFile(path, domain string) {
	data, err := os.ReadFile(path)
	if err != nil {
		return
	}
	var current *ErrorPattern
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "## #") {
			if current != nil {
				errorPatterns = append(errorPatterns, *current)
			}
			current = &ErrorPattern{Domain: domain, Name: line}
		}
		if current != nil {
			if strings.Contains(line, "**现象：") {
				start := strings.Index(line, "**现象：")
				end := strings.Index(line[start+5:], "**")
				if end > 0 {
					current.Keywords = append(current.Keywords, strings.Fields(line[start+5:start+5+end])...)
				}
			}
			if strings.Contains(line, "**解决：") {
				start := strings.Index(line, "**解决：")
				end := strings.Index(line[start+5:], "**")
				if end > 0 {
					current.Solution = line[start+5 : start+5+end]
				}
			}
		}
	}
	if current != nil {
		errorPatterns = append(errorPatterns, *current)
	}
}

// ── PostToolUse error matching ──
func matchPostToolError(toolName, toolResult string) *violation {
	if toolResult == "" {
		return nil
	}
	lower := strings.ToLower(toolResult)
	ruleMu.RLock()
	defer ruleMu.RUnlock()

	for _, ep := range errorPatterns {
		for _, kw := range ep.Keywords {
			if kw == "" {
				continue
			}
			if strings.Contains(lower, strings.ToLower(kw)) {
				count := checkRateLimit("error:" + ep.Name)
				return &violation{
					Violated: true,
					Rule:     ep.Domain + " · " + ep.Name,
					Detail:   fmt.Sprintf("匹配到高频错误（已出现 %d 次）", count),
					Solution: ep.Solution,
					Evidence: fmt.Sprintf("tool=%s keyword=%q", toolName, kw),
				}
			}
		}
	}
	return nil
}

// ── Violation types ──

type violation struct {
	Violated      bool   `json:"violated"`
	Rule          string `json:"rule,omitempty"`
	Detail        string `json:"detail,omitempty"`
	Solution      string `json:"solution,omitempty"`
	Evidence      string `json:"evidence,omitempty"`
	SystemMessage string `json:"systemMessage,omitempty"`
}

// ═══════════════════════════════════════════════════════════════════════════════
// TCP Handler — 统一处理 PreToolUse + PostToolUse
// ═══════════════════════════════════════════════════════════════════════════════

type toolEvent struct {
	Event      string         `json:"event"`
	ToolName   string         `json:"toolName"`
	ToolArgs   map[string]any `json:"toolArgs"`
	ToolResult string         `json:"toolResult,omitempty"`
	Content    string         `json:"content,omitempty"`
	Prompt     string         `json:"prompt,omitempty"`
}

func handleSupervisorConn(conn net.Conn) {
	defer conn.Close()
	conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	data := make([]byte, 65536)
	n, err := conn.Read(data)
	if err != nil {
		return
	}
	var evt toolEvent
	if err := json.Unmarshal(data[:n], &evt); err != nil {
		return
	}

	// Check for feedback markers in any text field
	for _, txt := range []string{evt.Content, evt.ToolResult} {
		kind, hint := parseFeedback(txt)
		if kind != "" {
			writeLog("feedback: %s (hint=%s)", kind, hint)
		}
	}

	switch evt.Event {
	case "SessionStart":
		handleSessionStart(conn)
	case "PreToolUse":
		handlePreToolUse(evt.ToolName, evt.ToolArgs, conn)
	case "PostToolUse":
		handlePostToolUse(evt.ToolName, evt.ToolResult, conn)
	case "UserPromptSubmit":
		handleUserPrompt(evt.Prompt, conn)
	case "Shutdown":
		conn.Write([]byte(`{"shutdown":true}`))
		writeLog("shutdown requested via TCP, exiting")
		os.Exit(0)
	default:
		conn.Write([]byte(`{"violated":false}`))
	}
}

func handlePreToolUse(toolName string, args map[string]any, conn net.Conn) {
	// Check habit library first
	habit, habitScore := matchHabit(toolName, args)
	if habit != nil && habitScore >= habit.Threshold {
		resp, _ := json.Marshal(&violation{
			Violated: false,
			Rule:     habit.Title,
			Detail:   fmt.Sprintf("match %d%%", habitScore),
			Solution: habit.Template,
			SystemMessage: fmt.Sprintf("Habit:%s\n%s (match %d%%)", habit.Title, habit.Template, habitScore),
		})
		conn.Write(resp)
		incrementHabitCount(habit.ID)
		return
	}
	if habit != nil && habitScore >= 50 {
		cnt := checkHabitCounter(habit.ID)
		if cnt >= 3 {
			resp, _ := json.Marshal(&violation{
				Violated: false,
				Rule:     habit.Title,
				Detail:   fmt.Sprintf("habit scene %d times", cnt),
				Solution: habit.Template,
				SystemMessage: fmt.Sprintf("Habit:%s\n%s (accumulated %dx)", habit.Title, habit.Template, cnt),
			})
			conn.Write(resp)
			incrementHabitCount(habit.ID)
			return
		}
	}

	// First check delayed queue — see if a prior warning is now due
	if pj := checkDelayedCorrection(toolName, args); pj != nil {
		resp, _ := json.Marshal(&violation{
			Violated: true,
			Rule:     pj.RuleID,
			Detail:   pj.Detail,
			Solution: pj.Solution,
			SystemMessage: fmt.Sprintf("⚠️ 前一步 %s 可能有问题: %s — %s", pj.ToolName, pj.Detail, pj.Solution),
		})
		conn.Write(resp)
		logViolation(pj.ToolName, resp)
		return
	}

	// Check built-in rules against vault config
	for _, rule := range builtinRules {
		matched, detail := rule.Detect(toolName, args)
		if !matched {
			continue
		}

		// Check vault config: disabled? silent?
		cfg := getRuleConfig(rule.ID)
		if cfg != nil {
			if !cfg.Enabled {
				continue // rule disabled in vault
			}
		}

		// Effective confidence: vault override or rule default
		effectiveConf := rule.Confidence
		if cfg != nil {
			effectiveConf = cfg.Confidence
		}

		// If effective confidence is Silent, log only
		if effectiveConf == ConfSilent {
			writeLog("silent: %s matched %s — suppressed", rule.ID, toolName)
			continue
		}

		count := checkRateLimit(rule.ID)
		if count > 3 {
			writeLog("ratelimit: %s fired %d times in 10min, downgrading", rule.ID, count)
			if effectiveConf > ConfLow {
				effectiveConf--
			}
		}

		switch effectiveConf {
		case ConfHigh:
			resp, _ := json.Marshal(&violation{
				Violated: true,
				Rule:     rule.ID,
				Detail:   detail,
				Solution: rule.Solution,
				SystemMessage: fmt.Sprintf("⚠️ %s — %s（建议：%s）", rule.ID, detail, rule.Solution),
			})
			conn.Write(resp)
			logViolation(toolName, resp)
			return

		case ConfMedium:
			resp, _ := json.Marshal(&violation{
				Violated: true,
				Rule:     rule.ID,
				Detail:   detail,
				Solution: rule.Solution,
				SystemMessage: fmt.Sprintf("⚠️ 检测到 %s — %s，请向用户二次确认是否继续", rule.ID, detail),
			})
			conn.Write(resp)
			logViolation(toolName, resp)
			return

		case ConfLow:
			// Don't alert yet — enqueue for delayed judgment
			enqueueDelayed(&pendingJudgment{
				RuleID:     rule.ID,
				ToolName:   toolName,
				Detail:     detail,
				Solution:   rule.Solution,
				Confidence: ConfLow,
				CreatedAt:  time.Now(),
			})
			writeLog("delayed: enqueued %s for %s", rule.ID, toolName)
			conn.Write([]byte(`{"violated":false,"note":"pending_review"}`))
			return
		default:
			writeLog("unknown confidence: %s level=%d", rule.ID, effectiveConf)
		}
	}

	conn.Write([]byte(`{"violated":false}`))
}

func handlePostToolUse(toolName, toolResult string, conn net.Conn) {
	// Match actual errors against error patterns
	v := matchPostToolError(toolName, toolResult)
	if v != nil {
		resp, _ := json.Marshal(v)
		conn.Write(resp)
		logViolation(toolName, resp)
		return
	}
	// Check if tool succeeded — this means a prior delayed warning might be over-cautious
	pendingMu.Lock()
	var kept []*pendingJudgment
	for _, pj := range pendingQueue {
		if strings.ToLower(pj.ToolName) == strings.ToLower(toolName) && toolResult != "" && !isErrorResult(toolResult) {
			writeLog("delayed: %s succeeded, cancelling pending warning", pj.RuleID)
			continue // drop this pending item
		}
		kept = append(kept, pj)
	}
	pendingQueue = kept
	pendingMu.Unlock()

	conn.Write([]byte(`{"violated":false}`))
}

// ── SessionStart: context injection ──

func handleSessionStart(conn net.Conn) {
	ctx := buildContextPackage()
	if ctx != "" {
		msg := fmt.Sprintf("## 小脑已激活\n\n%s", ctx)
		resp, _ := json.Marshal(&violation{Violated: false, SystemMessage: msg})
		conn.Write(resp)
		writeLog("session-start: context injected (%d chars)", len(msg))
	} else {
		conn.Write([]byte(`{"violated":false}`))
	}
}

func handleUserPrompt(prompt string, conn net.Conn) {
	if len(prompt) < 3 {
		conn.Write([]byte(`{"violated":false}`))
		return
	}
	results := searchKnowledge(prompt, 3)
	if len(results) == 0 {
		conn.Write([]byte(`{"violated":false}`))
		return
	}
	var lines []string
	lines = append(lines, "## 相关知识")
	for _, r := range results {
		lines = append(lines, fmt.Sprintf("- %s: %s", r.Title, r.Summary[:min(80, len(r.Summary))]))
	}
	msg := strings.Join(lines, "\n")
	resp, _ := json.Marshal(&violation{Violated: false, SystemMessage: msg})
	conn.Write(resp)
	writeLog("user-prompt: knowledge injected for %q (%d results)", prompt[:min(40,len(prompt))], len(results))
}

func min(a, b int) int {
	if a < b { return a }
	return b
}

// ── Context package builder (mirrors hook_logger's get_session_context) ──

func buildContextPackage() string {
	var b strings.Builder

	// 1. System capabilities
	b.WriteString("## 🧠 系统能力\n")
	b.WriteString("- 监督规则: 高风险操作自动警告（编辑 记忆/监督规则.md 可调整）\n")
	b.WriteString("- 错误检索: 匹配高频错误历史，推送解决方案\n")
	b.WriteString("- 习惯推送: 协作偏好自动推荐工作流程\n")
	b.WriteString("- 知识注入: 按话题关键词检索相关知识库\n\n")

	// 2. Active rules
	b.WriteString("## 📋 生效规则\n")
	rulesData, _ := os.ReadFile(filepath.Join(vaultPath, "记忆", "监督规则.md"))
	parseActiveRules(string(rulesData), &b)

	// 3. Recent errors
	b.WriteString("## 🔥 高频错误\n")
	errData, _ := os.ReadFile(filepath.Join(vaultPath, "记忆", "错误库.md"))
	parseTopErrors(string(errData), &b, 3)

	// 4. Quality habits
	b.WriteString("## 💡 可用习惯\n")
	habData, _ := os.ReadFile(filepath.Join(vaultPath, "记忆", "习惯库.md"))
	parseTopHabits(string(habData), &b, 3)

	// 5. Progress snapshot
	snapData, err := os.ReadFile(filepath.Join(vaultPath, "系统设计", "状态快照.md"))
	if err == nil {
		b.WriteString("## 📊 当前进度\n")
		raw := string(snapData)
		if strings.HasPrefix(raw, "---") {
			if end := strings.Index(raw[3:], "---"); end > 0 {
				raw = raw[3+end+3:]
			}
		}
		currentSec := ""
		for _, line := range strings.Split(raw, "\n") {
			line = strings.TrimSpace(line)
			if strings.HasPrefix(line, "## ") {
				currentSec = line[3:]
			} else if (currentSec == "当前话题" || currentSec == "已完成的" || currentSec == "进行中" || currentSec == "下一步" || currentSec == "下一步 (推荐)") && strings.HasPrefix(line, "- ") {
				b.WriteString("  " + line[:min(100, len(line))] + "\n")
			}
		}
		b.WriteString("\n")
	}

	return b.String()
}

func parseActiveRules(data string, b *strings.Builder) {
	currentName := ""
	enabled := false
	solution := ""
	for _, line := range strings.Split(data, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "## ") && !strings.Contains(line, " #") {
			if enabled && solution != "" {
				b.WriteString(fmt.Sprintf("- %s: %s\n", currentName, solution[:min(80,len(solution))]))
			}
			currentName = line[3:]
			enabled = false; solution = ""
		}
		if strings.Contains(line, "启用:") && strings.Contains(line, "true") && !strings.Contains(line, "false") {
			enabled = true
		}
		if strings.Contains(line, "方案:") {
			solution = strings.TrimSpace(strings.SplitN(line, "方案:", 2)[1])
		}
	}
	if enabled && solution != "" {
		b.WriteString(fmt.Sprintf("- %s: %s\n", currentName, solution[:min(80,len(solution))]))
	}
	b.WriteString("\n")
}

func parseTopErrors(data string, b *strings.Builder, maxN int) {
	type errEntry struct{ name string; count int; fix string }
	var errs []errEntry
	var cur errEntry
	for _, line := range strings.Split(data, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "## #") {
			if cur.count > 0 { errs = append(errs, cur) }
			cur = errEntry{name: line[3:]}
		}
		if strings.Contains(line, "次数：") && cur.name != "" {
			s := strings.SplitN(line, "次数：", 2)
			if len(s) > 1 {
				ns := strings.TrimSpace(strings.TrimRight(s[1], "*"))
				if n, e := strconv.Atoi(ns); e == nil { cur.count = n }
			}
		}
		if strings.Contains(line, "解决：") && cur.name != "" {
			s := strings.SplitN(line, "解决：", 2)
			if len(s) > 1 { cur.fix = strings.TrimSpace(strings.TrimRight(s[1], "*")); if len(cur.fix) > 80 { cur.fix = cur.fix[:80] } }
		}
	}
	if cur.count > 0 { errs = append(errs, cur) }
	// Sort by count descending (simple bubble for small N)
	for i := 0; i < len(errs); i++ {
		for j := i+1; j < len(errs); j++ {
			if errs[j].count > errs[i].count { errs[i], errs[j] = errs[j], errs[i] }
		}
	}
	for i := 0; i < len(errs) && i < maxN; i++ {
		b.WriteString(fmt.Sprintf("- %s (%d次): %s\n", errs[i].name, errs[i].count, errs[i].fix[:min(60,len(errs[i].fix))]))
	}
	b.WriteString("\n")
}

func parseTopHabits(data string, b *strings.Builder, maxN int) {
	type habEntry struct{ name string; tmpl string }
	var habs []habEntry
	var cur habEntry
	for _, line := range strings.Split(data, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "## [") && !strings.Contains(line[:min(50,len(line))], "{") {
			if cur.tmpl != "" { habs = append(habs, cur) }
			cur = habEntry{name: line[3:]}
		}
		if strings.Contains(line, "模板：") && cur.name != "" {
			t := strings.SplitN(line, "模板：", 2)
			if len(t) > 1 {
				tpl := strings.TrimSpace(strings.TrimRight(t[1], "*"))
				if tpl != "" && tpl != "(待提炼)" { cur.tmpl = tpl }
			}
		}
	}
	if cur.tmpl != "" { habs = append(habs, cur) }
	for i := 0; i < len(habs) && i < maxN; i++ {
		b.WriteString(fmt.Sprintf("- %s: %s\n", habs[i].name[:min(60,len(habs[i].name))], habs[i].tmpl[:min(80,len(habs[i].tmpl))]))
	}
	b.WriteString("\n")
}

// ── Knowledge search ──

type knowledgeEntry struct {
	Title   string `json:"title"`
	Summary string `json:"summary"`
	Tags    []string `json:"tags"`
	Path    string `json:"path"`
}

type knowledgeIndex struct {
	Entries map[string]knowledgeEntry `json:"entries"`
}

func searchKnowledge(query string, topK int) []knowledgeEntry {
	idxPath := filepath.Join(os.Getenv("USERPROFILE"), ".reasonix", "knowledge_index.json")
	data, err := os.ReadFile(idxPath)
	if err != nil { return nil }

	var idx knowledgeIndex
	if err := json.Unmarshal(data, &idx); err != nil { return nil }

	type scored struct {
		e knowledgeEntry
		s int
	}
	var results []scored
	qWords := strings.Fields(strings.ToLower(query))

	for _, e := range idx.Entries {
		score := 0
		titleL := strings.ToLower(e.Title)
		summaryL := strings.ToLower(e.Summary)
		for _, w := range qWords {
			if strings.Contains(titleL, w) { score += 10 }
			if strings.Contains(summaryL, w) { score += 3 }
			for _, t := range e.Tags {
				if strings.Contains(strings.ToLower(t), w) { score += 5 }
			}
		}
		if score > 0 { results = append(results, scored{e, score}) }
	}

	// Sort by score
	for i := 0; i < len(results); i++ {
		for j := i+1; j < len(results); j++ {
			if results[j].s > results[i].s { results[i], results[j] = results[j], results[i] }
		}
	}

	var out []knowledgeEntry
	for i := 0; i < len(results) && i < topK; i++ {
		out = append(out, results[i].e)
	}
	return out
}


func logViolation(toolName string, resp []byte) {
	var v violation
	if err := json.Unmarshal(resp, &v); err != nil {
		return
	}
	now := time.Now().Format("2006-01-02 15:04:05")
	entry := fmt.Sprintf("\n## 🚨 %s\n- **Tool:** `%s`\n- **Rule:** %s\n- **Detail:** %s\n- **Solution:** %s\n---\n", now, toolName, v.Rule, v.Detail, v.Solution)
	f, err := os.OpenFile(supervisionLog, os.O_APPEND|os.O_WRONLY|os.O_CREATE, 0644)
	if err == nil {
		f.WriteString(entry)
		f.Close()
	}
}

// ── Bot Sync ──

var (
	botState = make(map[string]int)
	stateMu  sync.Mutex
)

func loadBotState() {
	data, err := os.ReadFile(stateFile)
	if err != nil {
		return
	}
	stateMu.Lock()
	defer stateMu.Unlock()
	json.Unmarshal(data, &botState)
}

func saveBotState() {
	data, _ := json.Marshal(botState)
	os.MkdirAll(filepath.Dir(stateFile), 0755)
	os.WriteFile(stateFile, data, 0644)
}

func syncBotLogs() {
	entries, err := os.ReadDir(botSessionsDir)
	if err != nil {
		return
	}
	stateMu.Lock()
	defer stateMu.Unlock()
	today := time.Now().Format("2006-01-02")
	for _, entry := range entries {
		if !strings.HasPrefix(entry.Name(), "bot-") || !strings.HasSuffix(entry.Name(), ".jsonl") {
			continue
		}
		key := entry.Name()
		fpath := filepath.Join(botSessionsDir, key)
		data, err := os.ReadFile(fpath)
		if err != nil {
			continue
		}
		lines := strings.Split(strings.TrimRight(string(data), "\n"), "\n")
		total := len(lines)
		known := botState[key]
		if total < known {
			botState[key] = total
			continue
		}
		if total <= known {
			continue
		}
		var dst string
		header := string(data[:min(2000, len(data))])
		if strings.Contains(header, "花火") || strings.Contains(header, "Sparkle") {
			dst = wxLogPath
		} else {
			dst = qqLogPath
		}
		var out []string
		for _, line := range lines[known:] {
			line = strings.TrimSpace(line)
			if line == "" {
				continue
			}
			var msg struct {
				Role      string `json:"role"`
				Content   string `json:"content"`
				ToolCalls any    `json:"tool_calls,omitempty"`
			}
			if err := json.Unmarshal([]byte(line), &msg); err != nil {
				continue
			}
			if msg.Content == "" || len(msg.Content) < 2 {
				continue
			}
			if msg.Role == "user" {
				out = append(out, fmt.Sprintf("\n**%s 用户:** %s", today, truncate(msg.Content, 300)))
			} else if msg.Role == "assistant" && msg.ToolCalls == nil {
				if !strings.HasPrefix(msg.Content, "正在执行") && len(msg.Content) > 5 {
					out = append(out, fmt.Sprintf("**Bot:** %s", truncate(msg.Content, 300)))
				}
			}
		}
		if len(out) > 0 {
			f, _ := os.OpenFile(dst, os.O_APPEND|os.O_WRONLY|os.O_CREATE, 0644)
			if f != nil {
				f.WriteString(strings.Join(out, "\n") + "\n")
				f.Close()
			}
		}
		botState[key] = total
	}
	saveBotState()
}

func truncate(s string, n int) string {
	runes := []rune(s)
	if len(runes) > n {
		return string(runes[:n])
	}
	return s
}

// ── Lifecycle ──

func isReasonixRunning() bool {
	cmd := exec.Command("tasklist", "/FI", "IMAGENAME eq reasonix.exe", "/NH")
	out, err := cmd.Output()
	if err != nil {
		return false
	}
	return strings.Contains(string(out), "reasonix.exe")
}

func isBotWindowOpen() bool {
	cmd := exec.Command("tasklist", "/FI", "IMAGENAME eq cmd.exe", "/NH")
	out, err := cmd.Output()
	if err != nil {
		return false
	}
	return strings.Contains(string(out), "cmd.exe")
}

// ── TCP servers ──

func startSupervisorServer() {
	for {
		listener, err := net.Listen("tcp", "127.0.0.1"+port)
		if err != nil {
			writeLog("supervisor bind %s failed: %v, retry", port, err)
			time.Sleep(10 * time.Second)
			continue
		}
		writeLog("supervisor listening on %s", port)
		for {
			conn, err := listener.Accept()
			if err != nil {
				writeLog("supervisor accept error: %v, restarting", err)
				listener.Close()
				break
			}
			go handleSupervisorConn(conn)
		}
	}
}

// ── Main ──

func main() {
	flag.StringVar(&vaultPath, "vault", "", "Obsidian vault path")
	flag.StringVar(&writerPort, "writer-port", ":49520", "Writer TCP port")
	flag.StringVar(&port, "port", ":49522", "Supervisor TCP port")
	flag.StringVar(&botSessionsDir, "bot-dir", "", "Bot sessions directory")
	flag.Parse()

	if vaultPath == "" {
		vaultPath = "D:\\个人数据\\辞玖"
	}
	if botSessionsDir == "" {
		botSessionsDir = "C:\\Users\\20900\\AppData\\Roaming\\reasonix\\projects\\C--Users-20900-DeepSeek-Reasonix\\sessions"
	}
	stateFile = filepath.Join(os.Getenv("USERPROFILE"), ".reasonix", "logs", "bot_sync_state.json")
	initPaths()

	// Check port conflict
	if conn, err := net.DialTimeout("tcp", "127.0.0.1"+port, 2*time.Second); err == nil {
		conn.Close()
		writeLog("port %s already in use, exiting", port)
		os.Exit(0)
	}

	loadRules()
	loadErrorLibrary(vaultPath)
	loadHabitLibrary(vaultPath)
	loadSupervisorRules(vaultPath)
	loadBotState()

	// Lifecycle monitor
	go func() {
		for {
			time.Sleep(30 * time.Second)
			running := isReasonixRunning()
			botOpen := isBotWindowOpen()
			if !running && !botOpen {
				writeLog("no reasonix.exe and no bot window, shutting down")
				os.Exit(0)
			}
		}
	}()

	// Start writer (port 49520) and bot sync
	go startWriter(writerPort)

	// Bot sync every 2s
	go func() {
		for {
			syncBotLogs()
			time.Sleep(2 * time.Second)
		}
	}()

	// Rules reload every 5min
	go func() {
		for {
			time.Sleep(5 * time.Minute)
			loadRules()
			loadErrorLibrary(vaultPath)
			loadHabitLibrary(vaultPath)
			loadSupervisorRules(vaultPath)
		}
	}()

	// Health report every 30min
	go func() {
		for {
			time.Sleep(30 * time.Minute)
			writeLog("health: running, rules=%d err_patterns=%d rate_limiters=%d pending=%d",
				len(rules), len(errorPatterns), len(rateLimiters), len(pendingQueue))
		}
	}()

	writeLog("started: vault=%s port=%s", vaultPath, port)
	startSupervisorServer()
}






