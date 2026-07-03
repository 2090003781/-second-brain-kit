package main

import (
	"encoding/json"
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

// writerEvent is the JSON payload from hook_logger.
type writerEvent struct {
	Event     string `json:"event"`
	Ts        string `json:"ts"`
	Content   string `json:"content"`
	Session   string `json:"session,omitempty"`
	Level     string `json:"level,omitempty"`
	Type      string `json:"type,omitempty"`
	ToolName  string `json:"toolName,omitempty"`
	ToolArgs  map[string]any `json:"toolArgs,omitempty"`
	ToolResult string `json:"toolResult,omitempty"`
	Prompt    string `json:"prompt,omitempty"`
}

// Session state for hot cache
var (
	sessionErrors   []string
	sessionDecisions []string
	sessionTopics   []string
	sessionMu       sync.Mutex
)

func initSession() {
	sessionMu.Lock()
	defer sessionMu.Unlock()
	sessionErrors = nil
	sessionDecisions = nil
	sessionTopics = nil
}

func addSessionError(err string) {
	sessionMu.Lock()
	defer sessionMu.Unlock()
	sessionErrors = append(sessionErrors, err)
	if len(sessionErrors) > 10 {
		sessionErrors = sessionErrors[len(sessionErrors)-10:]
	}
}

func addSessionDecision(d string) {
	sessionMu.Lock()
	defer sessionMu.Unlock()
	sessionDecisions = append(sessionDecisions, d)
	if len(sessionDecisions) > 10 {
		sessionDecisions = sessionDecisions[len(sessionDecisions)-10:]
	}
}

func addSessionTopic(t string) {
	sessionMu.Lock()
	defer sessionMu.Unlock()
	for _, existing := range sessionTopics {
		if existing == t {
			return
		}
	}
	sessionTopics = append(sessionTopics, t)
}

var (
	idxLastRun  time.Time
	idxMu       sync.Mutex
)

func triggerIndexer() {
	idxMu.Lock()
	defer idxMu.Unlock()
	if time.Since(idxLastRun) < 30*time.Second {
		return
	}
	idxLastRun = time.Now()
	go func() {
		script := filepath.Join(os.Getenv("USERPROFILE"), ".reasonix", "logs", "knowledge_indexer.py")
		exec.Command("python", script).Start()
	}()
}

// structured patterns
var (
	markerPattern    = regexp.MustCompile(`\[(\w+):([^\]]+)\]`)
	decisionNewPat   = regexp.MustCompile(`(?i)\[DECISION:\s*(.+?)(?:\s*\|\s*context:\s*(.+?))?(?:\s*\|\s*scope:\s*(.+?))?\]`)
	errorNewPat      = regexp.MustCompile(`(?i)\[ERROR:\s*(.+?)(?:\s*\|\s*resolution:\s*(.+?))?(?:\s*\|\s*tool:\s*(.+?))?(?:\s*\|\s*fixed:\s*(.+?))?\]`)
	preferenceNewPat = regexp.MustCompile(`(?i)\[PREFERENCE:\s*(.+?)(?:\s*\|\s*context:\s*(.+?))?(?:\s*\|\s*source:\s*(.+?))?\]`)
	decisionPat      = regexp.MustCompile(`决策：(.+)`)
	preferPat        = regexp.MustCompile(`偏好：(.+)`)
	pendingPat       = regexp.MustCompile(`待续：(.+)`)
)

func startWriter(port string) {
	listener, err := net.Listen("tcp", "127.0.0.1"+port)
	if err != nil {
		writeLog("writer: bind %s failed: %v", port, err)
		return
	}
	defer listener.Close()
	writeLog("writer: listening on %s", port)

	for {
		conn, err := listener.Accept()
		if err != nil {
			time.Sleep(time.Second)
			continue
		}
		go handleWriterConn(conn)
	}
}

func handleWriterConn(conn net.Conn) {
	defer conn.Close()
	conn.SetReadDeadline(time.Now().Add(5 * time.Second))

	data := make([]byte, 65536)
	n, err := conn.Read(data)
	if err != nil {
		return
	}

	var evt writerEvent
	if err := json.Unmarshal(data[:n], &evt); err != nil {
		return
	}

	ts := evt.Ts
	date := formatDate(ts)

	// 1. Initialize session on SessionStart
	if evt.Event == "SessionStart" {
		initSession()
	}

	// 2. Write to reasonix-raw (raw timeline, ALL events)
	writeRawEvent(date, &evt)

	// 3. Track session state for errors/decisions
	if evt.Event == "PostToolUse" {
		toolResult := evt.ToolResult
		if toolResult != "" && isErrorResult(toolResult) {
			preview := toolResult
			if len(preview) > 100 {
				preview = preview[:100]
			}
			addSessionError(fmt.Sprintf("%s: %s", evt.ToolName, preview))
		}
	}
	if strings.Contains(evt.Content, "[DECISION") {
		m := decisionNewPat.FindStringSubmatch(evt.Content)
		if len(m) > 1 {
			addSessionDecision(m[1])
		}
	}

	// 4. Extract topic entries (v2 format markers)
	topic := extractTopic(evt)
	if topic != "" {
		addSessionTopic(topic)
		entries := extractEntries(evt.Content, ts)
		if len(entries) > 0 {
			writeTopicEntries(topic, entries)
			// Write to daily report for decisions/errors
			for _, e := range entries {
				if strings.Contains(e, "[DECISION") || strings.Contains(e, "[ERROR") {
					writeDailyRef(ts, e)
					break
				}
			}
		}
	}

	// 5. Write hot cache on SessionEnd
	if evt.Event == "SessionEnd" {
		writeHotCache()
	}

	// 6. Trigger indexer after vault writes
	triggerIndexer()

	conn.Write([]byte(`{"written":true}`))
}

func writeRawEvent(date string, evt *writerEvent) {
	rawDir := filepath.Join(vaultPath, "reasonix-raw")
	os.MkdirAll(rawDir, 0755)
	rawFile := filepath.Join(rawDir, date+".md")

	timeStr := formatTime(evt.Ts)
	event := evt.Event
	action := ""

	switch event {
	case "SessionStart":
		action = "开始"
	case "SessionEnd":
		action = "结束"
	case "UserPromptSubmit":
		action = "用户: " + truncateStr(evt.Prompt, 120)
	case "PreToolUse":
		action = fmt.Sprintf("工具: %s %s", evt.ToolName, truncateStr(fmt.Sprintf("%v", evt.ToolArgs), 100))
	case "PostToolUse":
		res := evt.ToolResult
		if len(res) > 100 {
			res = res[:100]
		}
		if isErrorResult(res) {
			action = fmt.Sprintf("❌ %s: %s", evt.ToolName, res)
		} else {
			action = fmt.Sprintf("✅ %s: %s", evt.ToolName, res)
		}
	case "Stop":
		action = "完成"
	default:
		action = truncateStr(evt.Content, 120)
	}

	line := fmt.Sprintf("- %s | %s\n", timeStr, action)

	f, err := os.OpenFile(rawFile, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return
	}
	defer f.Close()
	f.WriteString(line)
}

func writeHotCache() {
	hotDir := filepath.Join(vaultPath, "记忆")
	os.MkdirAll(hotDir, 0755)
	hotFile := filepath.Join(hotDir, "热缓存.md")

	sessionMu.Lock()
	errs := make([]string, len(sessionErrors))
	copy(errs, sessionErrors)
	decs := make([]string, len(sessionDecisions))
	copy(decs, sessionDecisions)
	topics := make([]string, len(sessionTopics))
	copy(topics, sessionTopics)
	sessionMu.Unlock()

	content := fmt.Sprintf("---\nupdated: %s\ntags: [reasonix/hotcache]\n---\n\n# 热缓存\n\n", time.Now().Format("2006-01-02"))

	if len(topics) > 0 {
		content += "## 当前活跃话题\n"
		for _, t := range topics {
			content += fmt.Sprintf("- %s\n", t)
		}
		content += "\n"
	}
	if len(decs) > 0 {
		content += "## 最近决策\n"
		n := 5
		if len(decs) < n {
			n = len(decs)
		}
		for _, d := range decs[len(decs)-n:] {
			content += fmt.Sprintf("- %s\n", d)
		}
		content += "\n"
	}
	if len(errs) > 0 {
		content += "## 最近错误\n"
		n := 5
		if len(errs) < n {
			n = len(errs)
		}
		for _, e := range errs[len(errs)-n:] {
			content += fmt.Sprintf("- %s\n", e)
		}
		content += "\n"
	}

	f, err := os.OpenFile(hotFile, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0644)
	if err != nil {
		return
	}
	defer f.Close()
	f.WriteString(content)
	writeLog("writer: hot cache written to %s", hotFile)
}

func extractTopic(evt writerEvent) string {
	re := regexp.MustCompile(`话题分隔\s*[:：]\s*(.+?)\s*[─\-—]`)
	if m := re.FindStringSubmatch(evt.Content); len(m) > 1 {
		return strings.TrimSpace(m[1])
	}
	if strings.HasPrefix(evt.Content, "[DECISION") || strings.HasPrefix(evt.Content, "[ERROR") || strings.HasPrefix(evt.Content, "[PREFERENCE") || strings.HasPrefix(evt.Content, "[PENDING") {
		return "记录"
	}
	if m := markerPattern.FindStringSubmatch(evt.Content); len(m) > 1 {
		return "记录"
	}
	return "记录"
}

func extractEntries(content, ts string) []string {
	var entries []string
	date := formatDate(ts)

	if m := decisionNewPat.FindStringSubmatch(content); len(m) > 1 {
		entry := fmt.Sprintf("- %s | [DECISION: %s", date, strings.TrimSpace(m[1]))
		if len(m) > 2 && m[2] != "" {
			entry += fmt.Sprintf(" | context: %s", strings.TrimSpace(m[2]))
		}
		if len(m) > 3 && m[3] != "" {
			entry += fmt.Sprintf(" | scope: %s", strings.TrimSpace(m[3]))
		}
		entry += "]"
		entries = append(entries, entry)
	}

	if m := errorNewPat.FindStringSubmatch(content); len(m) > 1 {
		entry := fmt.Sprintf("- %s | [ERROR: %s", date, strings.TrimSpace(m[1]))
		if len(m) > 2 && m[2] != "" {
			entry += fmt.Sprintf(" | resolution: %s", strings.TrimSpace(m[2]))
		}
		if len(m) > 3 && m[3] != "" {
			entry += fmt.Sprintf(" | tool: %s", strings.TrimSpace(m[3]))
		}
		if len(m) > 4 && m[4] != "" {
			entry += fmt.Sprintf(" | fixed: %s", strings.TrimSpace(m[4]))
		}
		entry += "]"
		entries = append(entries, entry)
	}

	if m := preferenceNewPat.FindStringSubmatch(content); len(m) > 1 {
		entry := fmt.Sprintf("- %s | [PREFERENCE: %s", date, strings.TrimSpace(m[1]))
		if len(m) > 2 && m[2] != "" {
			entry += fmt.Sprintf(" | context: %s", strings.TrimSpace(m[2]))
		}
		if len(m) > 3 && m[3] != "" {
			entry += fmt.Sprintf(" | source: %s", strings.TrimSpace(m[3]))
		}
		entry += "]"
		entries = append(entries, entry)
	}

	if m := decisionPat.FindStringSubmatch(content); len(m) > 1 {
		entries = append(entries, fmt.Sprintf("- %s | [DECISION: %s]", date, m[1]))
	}
	if m := preferPat.FindStringSubmatch(content); len(m) > 1 {
		entries = append(entries, fmt.Sprintf("- %s | [PREFERENCE: %s]", date, m[1]))
	}
	if m := pendingPat.FindStringSubmatch(content); len(m) > 1 {
		entries = append(entries, fmt.Sprintf("- %s | [PENDING: %s]", date, m[1]))
	}
	errPat := regexp.MustCompile(`\[err:\s*([^\]]+)\]`)
	if m := errPat.FindStringSubmatch(content); len(m) > 1 {
		entries = append(entries, fmt.Sprintf("- %s | [ERROR: %s]", date, strings.TrimSpace(m[1])))
	}

	return entries
}

func writeTopicEntries(topic string, entries []string) {
	topicDir := filepath.Join(vaultPath, "话题")
	os.MkdirAll(topicDir, 0755)
	topicFile := filepath.Join(topicDir, topic+".md")

	var f *os.File
	var err error

	if _, err = os.Stat(topicFile); os.IsNotExist(err) {
		header := fmt.Sprintf("---\ntitle: %s\ncreated: %s\ntags: [话题]\n---\n\n# %s\n\n## 流水\n", topic, time.Now().Format("2006-01-02"), topic)
		f, err = os.OpenFile(topicFile, os.O_CREATE|os.O_WRONLY, 0644)
		if err != nil {
			return
		}
		f.WriteString(header)
	} else {
		f, err = os.OpenFile(topicFile, os.O_APPEND|os.O_WRONLY, 0644)
		if err != nil {
			return
		}
	}

	defer f.Close()
	for _, e := range entries {
		f.WriteString(e + "\n")
	}
}

func writeDailyRef(ts, entry string) {
	dailyDir := filepath.Join(vaultPath, "日报")
	os.MkdirAll(dailyDir, 0755)
	date := formatDate(ts)
	dailyFile := filepath.Join(dailyDir, date+".md")

	f, err := os.OpenFile(dailyFile, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return
	}
	defer f.Close()

	stat, _ := os.Stat(dailyFile)
	if stat.Size() == 0 {
		f.WriteString(fmt.Sprintf("# %s 日报\n\n## 关键记录\n", date))
	}
	cleanEntry := strings.TrimPrefix(entry, "- ")
	f.WriteString("- " + cleanEntry + "\n")
}

func formatDate(ts string) string {
	if len(ts) >= 10 {
		return ts[:10]
	}
	return time.Now().Format("2006-01-02")
}

func formatTime(ts string) string {
	if len(ts) >= 19 {
		t := ts[11:19]
		return t[:5]
	}
	return time.Now().Format("15:04")
}

func truncateStr(s string, max int) string {
	if len(s) <= max {
		return s
	}
	return s[:max]
}

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
