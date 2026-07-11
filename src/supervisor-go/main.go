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
	supervisionLog = filepath.Join(vaultPath, "日志", "监督日志.md")
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

// supervisorRuleConfig is loaded from 记忆/规则库.md
type SupervisorRuleConfig struct {
	Enabled    bool
	Confidence Confidence
	Threshold  int  // for loop-detection
}

var supervisorRules = map[string]*SupervisorRuleConfig{}
var srMu sync.RWMutex

func loadSupervisorRules(vaultPath string) {
	path := filepath.Join(vaultPath, "记忆", "规则库.md")
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
		if (strings.HasPrefix(line, "## ") || strings.HasPrefix(line, "### ")) && !strings.HasPrefix(line, "## #") && !strings.HasPrefix(line, "### #") {
			if currentID != "" && currentCfg != nil {
				newRules[currentID] = currentCfg
			}
			rawID := strings.TrimPrefix(line, "### ")
			rawID = strings.TrimPrefix(rawID, "## ")
			// Strip [N级] prefix to get clean rule name
			if idx := strings.Index(rawID, "] "); idx > 0 && strings.HasPrefix(rawID, "[") {
				rawID = strings.TrimSpace(rawID[idx+2:])
			}
			currentID = rawID
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
	var result *pendingJudgment

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
			resolved = append(resolved, i)
			result = pj
			break
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

	return result
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

func isErrorResult(text string) bool {
	if text == "" {
		return false
	}
	lower := strings.ToLower(text)
	prefixes := []string{"error:", "error ", "exception:", "traceback", "panic:", "fatal:", "syntaxerror", "importerror", "typeerror"}
	for _, p := range prefixes {
		if strings.HasPrefix(lower, p) {
			return true
		}
	}
	return false
}

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
		loadErrorFile(filepath.Join(memoryDir, domain, "高频错误.md"), domain)
	}
	writeLog("loaded %d rules, %d error patterns from memory", len(rules), len(errorPatterns))
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
				end := strings.Index(line[start+11:], "**")
				if end > 0 {
					current.Keywords = append(current.Keywords, strings.Fields(line[start+11:start+11+end])...)
				}
			}
			if strings.Contains(line, "**解决：") {
				start := strings.Index(line, "**解决：")
				end := strings.Index(line[start+11:], "**")
				if end > 0 {
					current.Solution = line[start+11 : start+11+end]
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
	defer func() {
		if r := recover(); r != nil {
			writeLog("supervisor: panic recovered: %v", r)
		}
	}()
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
		logMu.Lock()
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

	// Check rules from vault config
	// Each rule's detection logic is defined inline, config comes from 记忆/规则库.md
	if handleBackupCheck(toolName, args, conn) { return }
	if handleGBKCheck(toolName, args, conn) { return }
	if handleChinesePathCheck(toolName, args, conn) { return }
	if handleLoopCheck(toolName, args, conn) { return }

	conn.Write([]byte(`{"violated":false}`))
}



// respondRule applies vault config, rate limiting, and sends the appropriate response.
func respondRule(ruleID, detail, solution string, conn net.Conn, toolName string) bool {
	cfg := getRuleConfig(ruleID)
	if cfg != nil && !cfg.Enabled {
		writeLog("rule %s disabled in vault, skipping", ruleID)
		return false
	}

	effectiveConf := ConfHigh
	if cfg != nil {
		effectiveConf = cfg.Confidence
	}

	if effectiveConf == ConfSilent {
		writeLog("silent: %s matched %s — suppressed", ruleID, toolName)
		return false
	}

	count := checkRateLimit(ruleID)
	if count > 3 {
		writeLog("ratelimit: %s fired %d times in 10min, downgrading", ruleID, count)
		if effectiveConf > ConfLow {
			effectiveConf--
		}
	}

	switch effectiveConf {
	case ConfHigh:
		resp, _ := json.Marshal(&violation{
			Violated: true,
			Rule:     ruleID,
			Detail:   detail,
			Solution: solution,
			SystemMessage: fmt.Sprintf("⚠️ %s — %s（建议：%s）", ruleID, detail, solution),
		})
		conn.Write(resp)
		logViolation(toolName, resp)
		return true

	case ConfMedium:
		resp, _ := json.Marshal(&violation{
			Violated: true,
			Rule:     ruleID,
			Detail:   detail,
			Solution: solution,
			SystemMessage: fmt.Sprintf("⚠️ 检测到 %s — %s，请向用户二次确认是否继续", ruleID, detail),
		})
		conn.Write(resp)
		logViolation(toolName, resp)
		return true

	case ConfLow:
		enqueueDelayed(&pendingJudgment{
			RuleID:     ruleID,
			ToolName:   toolName,
			Detail:     detail,
			Solution:   solution,
			Confidence: ConfLow,
			CreatedAt:  time.Now(),
		})
		writeLog("delayed: enqueued %s for %s", ruleID, toolName)
		conn.Write([]byte(`{"violated":false,"note":"pending_review"}`))
		return true
	}
	return false
}

func handleBackupCheck(toolName string, args map[string]any, conn net.Conn) bool {
	if strings.ToLower(toolName) != "write_file" {
		return false
	}
	p, ok := args["path"].(string)
	if !ok { return false }
	ext := strings.ToLower(filepath.Ext(p))
	if ext == ".toml" || ext == ".json" || ext == ".yaml" || ext == ".yml" {
		return respondRule("配置修改前必须备份", fmt.Sprintf("Writing %s without backup", filepath.Base(p)), "备份配置文件后再写入", conn, toolName)
	}
	return false
}

func handleGBKCheck(toolName string, args map[string]any, conn net.Conn) bool {
	tl := strings.ToLower(toolName)
	if tl != "bash" && tl != "echo" && tl != "powershell" && tl != "cmd" {
		return false
	}
	for _, v := range args {
		if s, ok := v.(string); ok && hasChinese(s) && !isVaultPath(s) {
			return respondRule("GBK 编码预防", "Command contains CJK characters", "指定 encoding=utf-8 或通过环境变量传递中文", conn, toolName)
		}
	}
	return false
}

func handleChinesePathCheck(toolName string, args map[string]any, conn net.Conn) bool {
	tl := strings.ToLower(toolName)
	if tl != "read_file" && tl != "write_file" && tl != "edit_file" && tl != "glob" && tl != "grep" {
		return false
	}
	p, ok := args["path"].(string)
	if ok && hasChinese(p) && !isVaultPath(p) {
		return respondRule("中文路径提示", fmt.Sprintf("CJK path: %s", filepath.Base(p)), "使用 ASCII 路径或确认文件编码", conn, toolName)
	}
	return false
}

func handleLoopCheck(toolName string, args map[string]any, conn net.Conn) bool {
	if checkLoop(toolName) {
		thresh := 3
		if cfg := getRuleConfig("循环检测"); cfg != nil {
			thresh = cfg.Threshold
		}
		return respondRule("循环检测", fmt.Sprintf("%s called %d+ times consecutively", toolName, thresh), "考虑更换方案或先行检查前提条件", conn, toolName)
	}
	return false
}


func loadWorkflowLibrary(vaultPath string) {
	wfPath := filepath.Join(vaultPath, "\u8bb0\u5fc6", "\u6280\u80fd\u5e93.md")
	data, err := os.ReadFile(wfPath)
	if err != nil {
		writeLog("workflows: cannot open %s: %v", wfPath, err)
		return
	}
	count := 0
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "## [") {
			entry := &HabitEntry{ID: nextHabitID(), CountOK: 0}
			if idx := strings.Index(line, "] "); idx > 0 {
				entry.Title = strings.TrimSpace(line[idx+2:])
				entry.Domain = strings.TrimSpace(line[4:idx])
			}
			entry.Threshold = 50
			habMu.Lock()
			habitLibrary = append(habitLibrary, entry)
			habMu.Unlock()
			count++
		}
	}
	writeLog("workflows: loaded %d entries", count)
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

	// Check for resume keywords (接续/继续 + topic)
	if strings.Contains(prompt, "接续") || strings.Contains(prompt, "继续") {
		hotFile := filepath.Join(vaultPath, "记忆", "热缓存.md")
		hotData, err := os.ReadFile(hotFile)
		if err == nil && len(hotData) > 50 {
			raw := string(hotData)
			if strings.HasPrefix(raw, "---") {
				if end := strings.Index(raw[3:], "---"); end > 0 {
					raw = raw[3+end+3:]
				}
			}
			msg := fmt.Sprintf("## 接续热缓存\n\n%s", raw[:min(800, len(raw))])
			resp, _ := json.Marshal(&violation{Violated: false, SystemMessage: msg})
			conn.Write(resp)
			writeLog("hot-cache: injected on resume request")
			return
		}
	}

	// Normal knowledge search
	// Knowledge search
	results := searchKnowledge(prompt, 3)
	
	// Case retrieval (always, even if knowledge empty)
	cases := QueryCases(vaultPath, 3, 3)
	
	var lines []string
	if len(results) > 0 {
		lines = append(lines, "## 相关知识")
		for _, r := range results {
			lines = append(lines, fmt.Sprintf("- %s: %s", r.Title, r.Summary[:min(80, len(r.Summary))]))
		}
		lines = append(lines, "")
	}
	if caseText := FormatCaseInjection(cases); caseText != "" {
		lines = append(lines, caseText)
	}
	
	if len(lines) == 0 {
		conn.Write([]byte(`{"violated":false}`))
		return
	}
	msg := strings.Join(lines, "\n")
	resp, _ := json.Marshal(&violation{Violated: false, SystemMessage: msg})
	conn.Write(resp)
	writeLog("user-prompt: knowledge+case injected for %q", prompt[:min(40,len(prompt))])
}

func min(a, b int) int {
	if a < b { return a }
	return b
}

// ── Context package builder (mirrors hook_logger's get_session_context) ──

func buildContextPackage() string {
	var b strings.Builder

	// 1. System capabilities
	b.WriteString("## 🧠 第二大脑已连接\n")
	b.WriteString("- 规则/错误/习惯/技能库已加载\n")
	b.WriteString("- 详细说明 → AGENTS.md\n\n")

	// 2. Only show 1级 rules (must-execute)
	b.WriteString("## 📋 1级规则\n")
	rulesData, _ := os.ReadFile(filepath.Join(vaultPath, "记忆", "规则库.md"))
	parseLevel1Rules(string(rulesData), &b)

	return b.String()
}

// parseLevel1Rules extracts only [1级] rules with their solutions.
func parseLevel1Rules(data string, b *strings.Builder) {
	currentName := ""
	enabled := false
	solution := ""
	isLevel1 := false
	for _, line := range strings.Split(data, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "### [") && strings.HasPrefix(line, "### [1级]") {
			if isLevel1 && enabled && solution != "" {
				b.WriteString(fmt.Sprintf("- %s: %s\n", currentName, solution[:min(80, len(solution))]))
			}
			currentName = strings.TrimSpace(line[7:])
			enabled = true
			solution = ""
			isLevel1 = true
			continue
		}
		if strings.HasPrefix(line, "### [") && !strings.HasPrefix(line, "### [1级]") {
			if isLevel1 && enabled && solution != "" {
				b.WriteString(fmt.Sprintf("- %s: %s\n", currentName, solution[:min(80, len(solution))]))
			}
			isLevel1 = false
			continue
		}
		if !isLevel1 {
			continue
		}
		if strings.Contains(line, "方案:") {
			solution = strings.TrimSpace(strings.SplitN(line, "方案:", 2)[1])
		}
	}
	if isLevel1 && enabled && solution != "" {
		b.WriteString(fmt.Sprintf("- %s: %s\n", currentName, solution[:min(80, len(solution))]))
	}
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

func startSupervisorServer() {
	loadWorkflowLibrary(vaultPath)
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
		botSessionsDir = "C:\\Users\\20900\\AppData\\Roamingeasonix\\projects\\C--Users-20900-DeepSeek-Reasonix\\sessions"
	}
	stateFile = filepath.Join(os.Getenv("USERPROFILE"), ".reasonix", "logs", "bot_sync_state.json")
	initPaths()

	// Check port conflict
	if conn, err := net.DialTimeout("tcp", "127.0.0.1"+port, 2*time.Second); err == nil {
		conn.Close()
		writeLog("port %s already in use, exiting", port)
		logMu.Lock()
		os.Exit(0)
	}

	loadRules()
	loadErrorLibrary(vaultPath)
	loadHabitLibrary(vaultPath)
	loadSupervisorRules(vaultPath)
	loadBotState()

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
			ruleMu.RLock()
			rLen := len(rules)
			epLen := len(errorPatterns)
			ruleMu.RUnlock()
			rateMu.Lock()
			rlLen := len(rateLimiters)
			rateMu.Unlock()
			pendingMu.Lock()
			pqLen := len(pendingQueue)
			pendingMu.Unlock()
			writeLog("health: running, rules=%d err_patterns=%d rate_limiters=%d pending=%d",
				rLen, epLen, rlLen, pqLen)
		}
	}()

	writeLog("started: vault=%s port=%s", vaultPath, port)
	initCaseQuery()
	loadWorkflowLibrary(vaultPath)
	startSupervisorServer()
	loadWorkflowLibrary(vaultPath)
}






