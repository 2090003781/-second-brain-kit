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
	ownLogFile     string // supervisor 自身运行日志
)

func initPaths() {
	memoryDir = filepath.Join(vaultPath, "记忆")
	supervisionLog = filepath.Join(vaultPath, "监督日志.md")
	qqLogPath = filepath.Join(vaultPath, "个人", "Bot", "QQ-Bot", "日志.md")
	wxLogPath = filepath.Join(vaultPath, "个人", "Bot", "微信-Bot", "日志.md")
	ownLogFile = filepath.Join(os.Getenv("USERPROFILE"), ".reasonix", "logs", "supervisor_run.log")
}

// ── 自身日志 ──
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

// ── Rules ──
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
	mu            sync.RWMutex
)

func loadRules() {
	mu.Lock()
	defer mu.Unlock()
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
	writeLog("loaded %d rules, %d error patterns", len(rules), len(errorPatterns))
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
				kw := extractContent(line, "现象：")
				current.Keywords = append(current.Keywords, strings.Fields(kw)...)
			}
			if strings.Contains(line, "**解决：") {
				current.Solution = extractContent(line, "解决：")
			}
		}
	}
	if current != nil {
		errorPatterns = append(errorPatterns, *current)
	}
}

func extractContent(line, prefix string) string {
	start := strings.Index(line, prefix)
	if start < 0 {
		return ""
	}
	start += len(prefix)
	end := strings.Index(line[start:], "**")
	if end < 0 {
		return strings.TrimSpace(line[start:])
	}
	return strings.TrimSpace(line[start : start+end])
}

// ── Loop Detection ──
type toolTracker struct {
	count    int
	lastSeen time.Time
}

var (
	toolTrackers = make(map[string]*toolTracker)
	loopMu       sync.Mutex
)

func checkLoop(toolName string) bool {
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
	if t.count >= 3 {
		t.count = 0
		return true
	}
	return false
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

// ── 违规检测 ──
var vaultPathExcluded bool // 是否已排除 vault 路径

type toolEvent struct {
	Event    string         `json:"event"`
	ToolName string         `json:"toolName"`
	ToolArgs map[string]any `json:"toolArgs"`
}

type violation struct {
	Violated      bool   `json:"violated"`
	Rule          string `json:"rule,omitempty"`
	Detail        string `json:"detail,omitempty"`
	Solution      string `json:"solution,omitempty"`
	SystemMessage string `json:"systemMessage,omitempty"`
}

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

func checkToolCall(toolName string, toolArgs map[string]any) *violation {
	tl := strings.ToLower(toolName)

	// Loop detection
	if checkLoop(toolName) {
		return &violation{
			Violated: true, Rule: "Loop detection",
			Detail:      fmt.Sprintf("%s called 3+ times", toolName),
			Solution:    "Change approach or check preconditions",
			SystemMessage: fmt.Sprintf("⚠️ Supervisor: loop detected on %s", toolName),
		}
	}

	// GBK encoding check - skip if args contain vault path
	if tl == "bash" || tl == "echo" || tl == "powershell" || tl == "cmd" {
		for _, v := range toolArgs {
			if s, ok := v.(string); ok && hasChinese(s) && !isVaultPath(s) {
				return &violation{
					Violated: true, Rule: "GBK encoding",
					Detail:   "Command args contain CJK chars outside vault path",
					Solution: "Set encoding=utf-8 or pass via env var",
					SystemMessage: fmt.Sprintf("⚠️ Supervisor: GBK risk on %s", toolName),
				}
			}
		}
	}

	// Chinese path check - skip vault paths
	if tl == "write_file" || tl == "edit_file" || tl == "read_file" || tl == "glob" || tl == "grep" {
		if p, ok := toolArgs["path"].(string); ok {
			if hasChinese(p) && !isVaultPath(p) {
				return &violation{
					Violated: true, Rule: "Chinese file path",
					Detail:   "CJK path outside vault",
					Solution: "Use ASCII paths or verify encoding",
					SystemMessage: fmt.Sprintf("⚠️ Supervisor: Chinese path on %s", toolName),
				}
			}
			// Backup check
			if tl == "write_file" {
				ext := strings.ToLower(filepath.Ext(p))
				if ext == ".toml" || ext == ".json" || ext == ".yaml" || ext == ".yml" {
					return &violation{
						Violated: true, Rule: "Backup before overwrite",
						Detail:   fmt.Sprintf("Writing %s without backup", p),
						Solution:  fmt.Sprintf("Copy-Item '%s' '%s.bak'", p, p),
						SystemMessage: fmt.Sprintf("⚠️ Supervisor: backup %s", p),
					}
				}
			}
		}
	}
	return nil
}

func handleConnection(conn net.Conn) {
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
	if evt.Event != "PreToolUse" {
		conn.Write([]byte(`{"violated":false}`))
		return
	}
	v := checkToolCall(evt.ToolName, evt.ToolArgs)
	if v != nil {
		resp, _ := json.Marshal(v)
		conn.Write(resp)
		logViolation(evt.ToolName, v)
	} else {
		conn.Write([]byte(`{"violated":false}`))
	}
}

func logViolation(toolName string, v *violation) {
	now := time.Now().Format("2006-01-02 15:04:05")
	entry := fmt.Sprintf("\n## 🚨 %s\n- **Tool:** `%s`\n- **Rule:** %s\n- **Detail:** %s\n- **Solution:** %s\n---\n", now, toolName, v.Rule, v.Detail, v.Solution)
	f, err := os.OpenFile(supervisionLog, os.O_APPEND|os.O_WRONLY|os.O_CREATE, 0644)
	if err == nil {
		f.WriteString(entry)
		f.Close()
	}
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

// ════════════════════════════════════════════════════════════════════
// Writer (Obsidian) — port 49520
// ════════════════════════════════════════════════════════════════════

// writerEvent is the JSON payload received on the writer TCP port.
type writerEvent struct {
	Event     string         `json:"event"`
	SessionID string         `json:"session_id,omitempty"`
	Session   string         `json:"session,omitempty"`
	Ts        string         `json:"ts,omitempty"`
	Cwd       string         `json:"cwd,omitempty"`
	Model     string         `json:"model,omitempty"`
	ToolName  string         `json:"toolName,omitempty"`
	ToolArgs  map[string]any `json:"toolArgs,omitempty"`
	ToolResult string        `json:"toolResult,omitempty"`
	Prompt    string         `json:"prompt,omitempty"`
	Message   string         `json:"message,omitempty"`
	Content   string         `json:"content,omitempty"`
	Level     string         `json:"level,omitempty"`
	Turn      int            `json:"turn,omitempty"`
	Trigger   string         `json:"trigger,omitempty"`
}

// ── Writer state ──

var (
	// open file handles: key -> *os.File
	openFiles   = make(map[string]*os.File)
	openFilesMu sync.Mutex

	// current topic tracking
	currentTopic   string
	currentTopicMu sync.Mutex

	// session-level hot cache
	sessionErrors   []string
	sessionDecisions []string
	sessionPrompts  []string
	sessionTopics   []string
	sessionMu       sync.Mutex
)

// topic marker regex: matches "── 话题分隔: xxx ──" or "-- 话题分隔：xxx --" etc.
var topicPattern = regexp.MustCompile(`[─\-—]{2,}\s*话题分隔\s*[:：]\s*(.+?)\s*[─\-—]{2,}`)

// error-like looking prefixes (lowercase)
var errorPrefixes = []string{
	"error:", "error ", "exception:", "traceback", "panic:", "fatal:",
	"syntaxerror", "importerror", "typeerror", "valueerror", "keyerror",
}

func looksLikeError(text string) bool {
	if text == "" {
		return false
	}
	lower := strings.TrimLeft(strings.ToLower(text), " \t")
	if len(lower) > 300 {
		lower = lower[:300]
	}
	for _, p := range errorPrefixes {
		if strings.HasPrefix(lower, p) {
			return true
		}
	}
	return false
}

// parseTopicMarker returns the topic name if a marker is found in text, else "".
func parseTopicMarker(text string) string {
	if text == "" {
		return ""
	}
	m := topicPattern.FindStringSubmatch(text)
	if len(m) > 1 {
		return strings.TrimSpace(m[1])
	}
	return ""
}

// getRawHandle returns an open file handle for today's raw timeline.
func getRawHandle(date time.Time) (*os.File, string) {
	rawDir := filepath.Join(vaultPath, "reasonix-raw")
	os.MkdirAll(rawDir, 0755)
	dateStr := date.Format("2006-01-02")
	filePath := filepath.Join(rawDir, dateStr+".md")
	key := "raw:" + dateStr

	openFilesMu.Lock()
	defer openFilesMu.Unlock()

	if fh, ok := openFiles[key]; ok && fh != nil {
		return fh, filePath
	}
	fh, err := os.OpenFile(filePath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		writeLog("writer: cannot open raw file %s: %v", filePath, err)
		return nil, filePath
	}
	openFiles[key] = fh
	return fh, filePath
}

// getTopicHandle returns an open file handle for a topic file.
func getTopicHandle(topicName string, topicDir string) (*os.File, string, string) {
	// sanitize filename
	safe := topicName
	replacer := strings.NewReplacer(
		"\\", "", "/", "", ":", "", "*", "", "?", "", "\"", "", "<", "", ">", "", "|", "",
	)
	safe = replacer.Replace(safe)
	safe = strings.TrimSpace(safe)
	if safe == "" {
		safe = "unknown-topic"
	}
	if len([]rune(safe)) > 80 {
		safe = string([]rune(safe)[:80])
	}

	os.MkdirAll(topicDir, 0755)
	os.MkdirAll(topicDir, 0755)
	filePath := filepath.Join(topicDir, safe+".md")
	key := "topic:" + safe

	openFilesMu.Lock()
	defer openFilesMu.Unlock()

	if fh, ok := openFiles[key]; ok && fh != nil {
		return fh, filePath, safe
	}
	fh, err := os.OpenFile(filePath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		writeLog("writer: cannot open topic file %s: %v", filePath, err)
		return nil, filePath, safe
	}
	openFiles[key] = fh
	return fh, filePath, safe
}

// closeHandle closes and removes an open file handle by key.
func closeHandle(key string) {
	openFilesMu.Lock()
	defer openFilesMu.Unlock()
	if fh, ok := openFiles[key]; ok && fh != nil {
		fh.Close()
		delete(openFiles, key)
	}
}

// appendFile writes text to an already-open file, ensuring it's flushed.
func appendFile(fh *os.File, text string) {
	if fh == nil {
		return
	}
	fh.WriteString(text)
	fh.Sync()
}

// writeHotCache writes the session hot cache to 记忆/热缓存.md.
func writeHotCache(sessionID string) {
	hotPath := filepath.Join(vaultPath, "记忆", "热缓存.md")
	os.MkdirAll(filepath.Dir(hotPath), 0755)
	now := time.Now()
	dateStr := now.Format("2006-01-02")

	sessionMu.Lock()
	errors := copyStrings(sessionErrors)
	decisions := copyStrings(sessionDecisions)
	prompts := copyStrings(sessionPrompts)
	topics := copyStrings(sessionTopics)
	// Clear
	sessionErrors = nil
	sessionDecisions = nil
	sessionPrompts = nil
	sessionTopics = nil
	sessionMu.Unlock()

	var parts []string
	parts = append(parts, "---\n")
	parts = append(parts, fmt.Sprintf("updated: %s\n", dateStr))
	parts = append(parts, "tags: [reasonix/hotcache]\n")
	parts = append(parts, "---\n\n")
	parts = append(parts, "# 热缓存\n\n")

	if len(topics) > 0 {
		parts = append(parts, "## 当前活跃话题\n")
		for _, t := range topics {
			safe := strings.NewReplacer("[", "", "]", "").Replace(t)
			parts = append(parts, fmt.Sprintf("- [[话题/%s]] — 活跃中\n", safe))
		}
		parts = append(parts, "\n")
	}

	if len(decisions) > 0 {
		parts = append(parts, "## 最近关键决策\n")
		for _, d := range lastN(decisions, 3) {
			parts = append(parts, fmt.Sprintf("- %s\n", d))
		}
		parts = append(parts, "\n")
	}

	if len(prompts) > 0 {
		parts = append(parts, "## 待接续\n")
		for _, p := range lastN(prompts, 3) {
			parts = append(parts, fmt.Sprintf("- %s\n", p))
		}
		parts = append(parts, "\n")
	}

	if len(errors) > 0 {
		parts = append(parts, "## 最近错误\n")
		for _, e := range lastN(errors, 3) {
			parts = append(parts, fmt.Sprintf("- %s\n", e))
		}
		parts = append(parts, "\n")
	}

	body := strings.Join(parts, "")
	fh, err := os.OpenFile(hotPath, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0644)
	if err != nil {
		writeLog("writer: hot cache write error: %v", err)
		return
	}
	defer fh.Close()
	fh.WriteString(body)
	fh.Sync()
	writeLog("writer: hot cache written: %s", hotPath)
}

func copyStrings(src []string) []string {
	dst := make([]string, len(src))
	copy(dst, src)
	return dst
}

func lastN(s []string, n int) []string {
	if len(s) <= n {
		return s
	}
	return s[len(s)-n:]
}

// ensureRawHeader writes the day header if the raw file is empty.
func ensureRawHeader(fh *os.File, filePath, dateCompact string) {
	if fh == nil {
		return
	}
	info, err := os.Stat(filePath)
	if err != nil || info.Size() == 0 {
		appendFile(fh, fmt.Sprintf("# %s 原始时间线\n\n", dateCompact))
	}
}

// ensureTopicHeader writes the front matter if the topic file is empty.
func ensureTopicHeader(fh *os.File, filePath, topicName, dateCompact string) {
	if fh == nil {
		return
	}
	info, err := os.Stat(filePath)
	if err != nil || info.Size() == 0 {
		header := fmt.Sprintf("---\ncreated: %s\ntags: [reasonix/topic, reasonix/active]\nstatus: 活跃\n---\n\n# %s\n\n", dateCompact, topicName)
		appendFile(fh, header)
	}
}

// processWriterEvent handles one writer event: writes to raw timeline and topic file.

var nsfwKeywords = []string{"nsfw", "NSFW", "R18", "成人", "色情", "H场景", "H事件", "18禁"}

func isNSFWTopic(topicName string) bool {
	topicLower := strings.ToLower(topicName)
	for _, kw := range nsfwKeywords {
		if strings.Contains(topicLower, strings.ToLower(kw)) {
			return true
		}
	}
	return false
}

func getTopicDir(topicName string) string {
	if isNSFWTopic(topicName) {
		return filepath.Join(vaultPath, "个人", "话题")
	}
	return filepath.Join(vaultPath, "话题")
}

func processWriterEvent(payload writerEvent) {
	event := payload.Event
	if event == "" {
		return
	}

	// Parse timestamp
	ts := payload.Ts
	var dt time.Time
	if ts != "" {
		if parsed, err := time.Parse(time.RFC3339, ts); err == nil {
			dt = parsed
		} else if parsed, err := time.Parse("2006-01-02T15:04:05", ts); err == nil {
			dt = parsed
		} else if parsed, err := time.Parse("2006-01-02 15:04:05", ts); err == nil {
			dt = parsed
		} else {
			dt = time.Now()
		}
	} else {
		dt = time.Now()
	}
	date := dt
	timestamp := dt.Format("15:04:05")
	dateStr := dt.Format("2006-01-02 15:04")
	dateCompact := dt.Format("2006-01-02")

	sessionID := payload.SessionID
	if sessionID == "" {
		sessionID = payload.Session
	}
	if sessionID == "" {
		sessionID = "unknown"
	}

	toolName := payload.ToolName
	toolArgs := payload.ToolArgs
	toolResult := payload.ToolResult
	prompt := payload.Prompt
	msg := payload.Message
	content := payload.Content
	level := payload.Level

	// Detect topic marker from content, prompt, toolArgs, toolResult
	topicFromMarker := ""
	for _, src := range []string{content, prompt, mapToJSON(toolArgs), toolResult} {
		if src != "" {
			t := parseTopicMarker(src)
			if t != "" {
				topicFromMarker = t
				break
			}
		}
	}

	currentTopicMu.Lock()
	if topicFromMarker != "" {
		currentTopic = topicFromMarker
	}
	curTopic := currentTopic
	currentTopicMu.Unlock()

	// Track in session
	if curTopic != "" {
		sessionMu.Lock()
		found := false
		for _, t := range sessionTopics {
			if t == curTopic {
				found = true
				break
			}
		}
		if !found {
			sessionTopics = append(sessionTopics, curTopic)
		}
		sessionMu.Unlock()
	}

	// ── Build raw timeline entry ──
	rawLabel := event
	rawLine := ""

	switch event {
	case "SessionStart":
		cwd := payload.Cwd
		model := payload.Model
		rawLabel = "会话开始"
		rawLine = fmt.Sprintf("Session: %s  |  cwd: %s  |  model: %s", sessionID, cwd, model)

	case "SessionEnd":
		rawLabel = "会话结束"
		rawLine = fmt.Sprintf("Session: %s", sessionID)
		writeHotCache(sessionID)

	case "UserPromptSubmit":
		rawLabel = "用户提问"
		rawLine = truncate(prompt, 300)
		if prompt != "" {
			sessionMu.Lock()
			sessionPrompts = append(sessionPrompts, truncate(prompt, 120))
			if len(sessionPrompts) > 5 {
				sessionPrompts = sessionPrompts[1:]
			}
			sessionMu.Unlock()
		}

	case "PreToolUse":
		rawLabel = "工具调用"
		rawLine = fmt.Sprintf("%s %s", toolName, mapToJSON(toolArgs))

	case "PostToolUse":
		isErr := looksLikeError(toolResult)
		if isErr {
			rawLabel = "工具错误"
		} else {
			rawLabel = "工具结果"
		}
		preview := toolResult
		if preview == "" {
			preview = "(no return)"
		}
		rawLine = fmt.Sprintf("%s -> %s", toolName, truncate(preview, 200))
		if isErr && toolName != "" {
			errSummary := fmt.Sprintf("%s: %s", toolName, truncate(preview, 100))
			sessionMu.Lock()
			sessionErrors = append(sessionErrors, errSummary)
			if len(sessionErrors) > 10 {
				sessionErrors = sessionErrors[1:]
			}
			sessionMu.Unlock()
		}

	case "Stop":
		turn := payload.Turn
		rawLabel = "Turn 完成"
		rawLine = fmt.Sprintf("Turn %d", turn)

	case "Checkpoint":
		rawLabel = fmt.Sprintf("检查点 (%s)", level)
		rawLine = truncate(content, 300)
		if content != "" {
			sessionMu.Lock()
			sessionDecisions = append(sessionDecisions, truncate(content, 120))
			if len(sessionDecisions) > 8 {
				sessionDecisions = sessionDecisions[1:]
			}
			sessionMu.Unlock()
		}

	case "PreCompact":
		rawLabel = "上下文压缩"
		trigger := payload.Trigger
		if trigger == "" {
			trigger = "auto"
		}
		rawLine = fmt.Sprintf("trigger: %s", trigger)

	case "Notification":
		rawLabel = "通知"
		rawLine = truncate(msg, 300)

	case "PostLLMCall":
		rawLabel = "模型输出"
		reply := toolResult
		if reply == "" {
			reply = payload.Message
		}
		rawLine = truncate(strings.ReplaceAll(reply, "\n", " "), 200)

	case "SubagentStop":
		rawLabel = "子任务完成"
		rawLine = ""

	case "PermissionRequest":
		rawLabel = "权限请求"
		rawLine = toolName

	default:
		rawLabel = event
		rawLine = truncate(content, 300)
	}

	// 1) Write raw timeline
	fhRaw, rawPath := getRawHandle(date)
	if fhRaw != nil {
		ensureRawHeader(fhRaw, rawPath, dateCompact)
		rawMD := fmt.Sprintf("## %s %s\n%s\n\n", timestamp, rawLabel, rawLine)
		appendFile(fhRaw, rawMD)
	}

	// 2) Write topic file if we have a current topic
	if curTopic == "" {
		return
	}

	fhTopic, topicPath, safeName := getTopicHandle(curTopic, getTopicDir(curTopic))
	if fhTopic == nil {
		return
	}

	ensureTopicHeader(fhTopic, topicPath, curTopic, dateCompact)

	// Build topic content
	var topicParts []string
	topicParts = append(topicParts, fmt.Sprintf("## %s | 来自会话 %s\n\n", dateStr, sessionID))

	switch event {
	case "SessionStart":
		cwd := payload.Cwd
		model := payload.Model
		topicParts = append(topicParts, fmt.Sprintf("**会话开始** | cwd: %s | model: %s\n", cwd, model))

	case "SessionEnd":
		topicParts = append(topicParts, "**→ 会话结束**\n")
		closeHandle("topic:" + safeName)

	case "UserPromptSubmit":
		topicParts = append(topicParts, fmt.Sprintf("**用户提问:** %s\n", prompt))

	case "PreToolUse":
		argsPreview := mapToJSON(toolArgs)
		topicParts = append(topicParts, fmt.Sprintf("**工具调用:** %s\n  参数: %s\n", toolName, truncate(argsPreview, 300)))

	case "PostToolUse":
		isErr := looksLikeError(toolResult)
		preview := truncate(toolResult, 300)
		if preview == "" {
			preview = "(无返回)"
		}
		if isErr {
			topicParts = append(topicParts, fmt.Sprintf("**❌ 工具错误** %s:\n  `\n%s\n  `\n", toolName, preview))
			topicParts = append(topicParts, fmt.Sprintf("→ ❌ 错误: %s\n", truncate(toolResult, 200)))
		} else {
			topicParts = append(topicParts, fmt.Sprintf("**✅ 工具完成** %s → %s\n", toolName, preview))
		}

	case "Stop":
		turn := payload.Turn
		topicParts = append(topicParts, fmt.Sprintf("**Turn %d 完成**\n", turn))

	case "Checkpoint":
		emojiMap := map[string]string{"milestone": "🎯", "progress": "📊", "blocker": "🚧"}
		emoji := "📌"
		if e, ok := emojiMap[level]; ok {
			emoji = e
		}
		// Remove topic markers from content
		clean := topicPattern.ReplaceAllString(content, "")
		clean = strings.TrimSpace(clean)
		topicParts = append(topicParts, fmt.Sprintf("**%s 检查点 (%s):** %s\n", emoji, level, clean))

	case "PreCompact":
		trigger := payload.Trigger
		if trigger == "" {
			trigger = "auto"
		}
		topicParts = append(topicParts, fmt.Sprintf("**📦 上下文压缩** (触发: %s)\n", trigger))

	case "Notification":
		topicParts = append(topicParts, fmt.Sprintf("**通知:** %s\n", msg))

	case "PostLLMCall":
		reply := toolResult
		if reply == "" {
			reply = payload.Message
		}
		preview := truncate(strings.ReplaceAll(reply, "\n", " "), 300)
		topicParts = append(topicParts, fmt.Sprintf("**🤖 模型输出:** %s\n", preview))

	case "SubagentStop":
		topicParts = append(topicParts, "**🔄 子任务完成**\n")

	case "PermissionRequest":
		topicParts = append(topicParts, fmt.Sprintf("**🔒 权限请求:** %s\n", toolName))

	default:
		topicParts = append(topicParts, fmt.Sprintf("**事件 %s:** %s\n", event, truncate(content, 200)))
	}

	topicParts = append(topicParts, "\n---\n\n")
	appendFile(fhTopic, strings.Join(topicParts, ""))

	if event == "SessionEnd" {
		closeHandle("raw:" + dateCompact)
	}
}

func mapToJSON(m map[string]any) string {
	if m == nil {
		return ""
	}
	b, _ := json.Marshal(m)
	return string(b)
}

func handleWriterConn(conn net.Conn) {
	defer conn.Close()
	conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	data := make([]byte, 65536)
	n, err := conn.Read(data)
	if err != nil {
		return
	}
	var evt writerEvent
	if err := json.Unmarshal(data[:n], &evt); err != nil {
		return
	}
	processWriterEvent(evt)
	conn.Write([]byte(`{"ok":true}`))
}

func startWriterServer() {
	for {
		listener, err := net.Listen("tcp", "127.0.0.1"+writerPort)
		if err != nil {
			writeLog("writer bind %s failed: %v, retry in 10s", writerPort, err)
			time.Sleep(10 * time.Second)
			continue
		}
		writeLog("writer listening on %s", writerPort)
		for {
			conn, err := listener.Accept()
			if err != nil {
				writeLog("writer accept error: %v", err)
				listener.Close()
				break
			}
			go handleWriterConn(conn)
		}
	}
}

// ── Supervisor TCP server (port 49522) ──

func startSupervisorServer() {
	for {
		listener, err := net.Listen("tcp", "127.0.0.1"+port)
		if err != nil {
			writeLog("supervisor bind %s failed: %v, retry in 10s", port, err)
			time.Sleep(10 * time.Second)
			continue
		}
		writeLog("supervisor listening on %s", port)
		for {
			conn, err := listener.Accept()
			if err != nil {
				writeLog("supervisor accept error: %v, restarting listener", err)
				listener.Close()
				break
			}
			go handleConnection(conn)
		}
	}
}

// ── Main ──
func main() {
	flag.StringVar(&vaultPath, "vault", "", "Obsidian vault path")
	flag.StringVar(&writerPort, "writer-port", ":49520", "Writer TCP port")
	flag.StringVar(&port, "port", ":49522", "TCP listen port (supervisor)")
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

	// Check port conflict (supervisor port only; writer port checked by startWriterServer)
	if conn, err := net.DialTimeout("tcp", "127.0.0.1"+port, 2*time.Second); err == nil {
		conn.Close()
		writeLog("port %s already in use, exiting", port)
		os.Exit(0)
	}

	loadRules()
	loadBotState()

	// Lifecycle: exit only when BOTH reasonix desktop AND bot window are closed
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

	// Bot sync every 2s
	go func() {
		for {
			syncBotLogs()
			time.Sleep(2 * time.Second)
		}
	}()

	// Reload rules every 5min
	go func() {
		for {
			time.Sleep(5 * time.Minute)
			loadRules()
		}
	}()

	// Health report every 30min
	go func() {
		for {
			time.Sleep(30 * time.Minute)
			writeLog("health: running, rules=%d errors=%d", len(rules), len(errorPatterns))
		}
	}()

	// Start both TCP servers concurrently
	go startWriterServer()
	startSupervisorServer()
}


