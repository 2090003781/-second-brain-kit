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

type writerEvent struct {
	Event      string         `json:"event"`
	Ts         string         `json:"ts"`
	Content    string         `json:"content"`
	Session    string         `json:"session,omitempty"`
	Level      string         `json:"level,omitempty"`
	Type       string         `json:"type,omitempty"`
	ToolName   string         `json:"toolName,omitempty"`
	ToolArgs   map[string]any `json:"toolArgs,omitempty"`
	ToolResult string         `json:"toolResult,omitempty"`
	Prompt     string         `json:"prompt,omitempty"`
}

var (
	sessionErrors, sessionDecisions, sessionTopics []string
	sessionMu                                     sync.Mutex
	_currentSession                                = ""
	_currentTopic                                  = ""
)

var (
	decisionNewPat   = regexp.MustCompile(`(?i)\[DECISION:\s*(.+?)(?:\s*\|\s*context:\s*(.+?))?(?:\s*\|\s*scope:\s*(.+?))?\]`)
	errorNewPat      = regexp.MustCompile(`(?i)\[ERROR:\s*(.+?)(?:\s*\|\s*resolution:\s*(.+?))?(?:\s*\|\s*tool:\s*(.+?))?(?:\s*\|\s*fixed:\s*(.+?))?\]`)
	preferenceNewPat = regexp.MustCompile(`(?i)\[PREFERENCE:\s*(.+?)(?:\s*\|\s*context:\s*(.+?))?(?:\s*\|\s*source:\s*(.+?))?\]`)
	topicSplitPat    = regexp.MustCompile(`话题分隔\s*[:：]\s*(.+?)\s*[─\-—]`)
)

func initSession() {
	sessionMu.Lock()
	defer sessionMu.Unlock()
	sessionErrors, sessionDecisions, sessionTopics = nil, nil, nil
	_currentSession = ""
	_currentTopic = ""
}

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
		if err != nil { time.Sleep(time.Second); continue }
		go handleWriterConn(conn)
	}
}

func handleWriterConn(conn net.Conn) {
	defer conn.Close()
	conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	data := make([]byte, 65536)
	n, err := conn.Read(data)
	if err != nil { return }
	var evt writerEvent
	if err := json.Unmarshal(data[:n], &evt); err != nil { return }

	ts := evt.Ts
	date := ts[:10]

	if evt.Event == "SessionStart" {
		initSession()
		_currentSession = fmt.Sprintf("会话-%s", time.Now().Format("150405"))
	}

	// Detect topic marker
	if m := topicSplitPat.FindStringSubmatch(evt.Content); len(m) > 1 {
		_currentTopic = strings.TrimSpace(m[1])
	}
	if _currentTopic == "" && evt.Prompt != "" {
		if m := topicSplitPat.FindStringSubmatch(evt.Prompt); len(m) > 1 {
			_currentTopic = strings.TrimSpace(m[1])
		}
	}

	// Write compact event log
	writeRawEvent(date, ts, &evt)

	// Track session state
	if evt.Event == "PostToolUse" {
		r := evt.ToolResult
		if r != "" && (strings.HasPrefix(strings.ToLower(r), "error") || strings.Contains(strings.ToLower(r), "traceback")) {
			preview := r; if len(preview) > 100 { preview = preview[:100] }
			sessionMu.Lock(); sessionErrors = append(sessionErrors, fmt.Sprintf("%s: %s", evt.ToolName, preview)); sessionMu.Unlock()
		}
	}
	if m := decisionNewPat.FindStringSubmatch(evt.Content); len(m) > 1 {
		writeMarkerLine(date, evt.Ts, fmt.Sprintf("[DECISION: %s]", strings.TrimSpace(m[1])), &evt)
		sessionMu.Lock(); sessionDecisions = append(sessionDecisions, m[1])
		if _currentTopic != "" { sessionTopics = append(sessionTopics, _currentTopic) }
		sessionMu.Unlock()
	}
	if m := errorNewPat.FindStringSubmatch(evt.Content); len(m) > 1 {
		writeMarkerLine(date, evt.Ts, fmt.Sprintf("[ERROR: %s]", strings.TrimSpace(m[1])), &evt)
	}
	if m := preferenceNewPat.FindStringSubmatch(evt.Content); len(m) > 1 {
		writeMarkerLine(date, evt.Ts, fmt.Sprintf("[PREFERENCE: %s]", strings.TrimSpace(m[1])), &evt)
	}

	if evt.Event == "SessionEnd" { writeHotCache() }
	triggerIndexer()
	conn.Write([]byte(`{"written":true}`))
}

func sessionFileName() string {
	sessionMu.Lock()
	defer sessionMu.Unlock()
	if _currentTopic != "" { return _currentTopic }
	if _currentSession != "" { return _currentSession }
	return "会话"
}

func writeRawEvent(date, ts string, evt *writerEvent) {
	name := sessionFileName()
	dir := filepath.Join(vaultPath, "reasonix-raw", date)
	os.MkdirAll(dir, 0755)
	f, err := os.OpenFile(filepath.Join(dir, name+".md"), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil { return }
	defer f.Close()

	timeStr := ts[11:16]
	event := evt.Event
	var line string

	switch event {
	case "SessionStart":
		line = fmt.Sprintf("%s | ▶ 会话开始\n", timeStr)
	case "SessionEnd":
		line = fmt.Sprintf("%s | ■ 会话结束\n", timeStr)
	case "UserPromptSubmit":
		p := evt.Prompt
		if len(p) > 150 { p = p[:150] }
		line = fmt.Sprintf("%s | %s\n", timeStr, p)
	case "PreToolUse":
		line = fmt.Sprintf("%s | → %s\n", timeStr, evt.ToolName)
	case "PostToolUse":
		r := evt.ToolResult
		if len(r) > 100 { r = r[:100] }
		if r != "" && (strings.HasPrefix(strings.ToLower(r), "error") || strings.Contains(strings.ToLower(r), "traceback")) {
			line = fmt.Sprintf("%s | ✗ %s: %s\n", timeStr, evt.ToolName, r)
		} else {
			line = fmt.Sprintf("%s | ✓ %s\n", timeStr, evt.ToolName)
		}
	case "Stop":
		line = fmt.Sprintf("%s | ✔ 结束\n", timeStr)
	default:
		c := evt.Content; if len(c) > 100 { c = c[:100] }
		line = fmt.Sprintf("%s | %s\n", timeStr, c)
	}
	f.WriteString(line)
}

func writeMarkerLine(date, ts, marker string, _ *writerEvent) {
	name := sessionFileName()
	dir := filepath.Join(vaultPath, "reasonix-raw", date)
	os.MkdirAll(dir, 0755)
	f, err := os.OpenFile(filepath.Join(dir, name+".md"), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil { return }
	defer f.Close()
	f.WriteString(fmt.Sprintf("%s | %s\n", ts[11:16], marker))
}

func writeHotCache() {
	dir := filepath.Join(vaultPath, "记忆"); os.MkdirAll(dir, 0755)
	sessionMu.Lock()
	errs, decs, topics := sessionErrors, sessionDecisions, sessionTopics
	sessionMu.Unlock()

	var b strings.Builder
	b.WriteString(fmt.Sprintf("---\nupdated: %s\ntags: [reasonix/hotcache]\n---\n\n# 热缓存\n\n", time.Now().Format("2006-01-02")))
	if len(topics) > 0 {
		b.WriteString("## 当前活跃话题\n")
		for _, t := range topics {
			b.WriteString(fmt.Sprintf("- [[%s]]\n", t))
		}
		b.WriteString("\n")
	}
	if len(decs) > 0 {
		b.WriteString("## 最近决策\n")
		n := min(len(decs), 5)
		for _, d := range decs[len(decs)-n:] { b.WriteString(fmt.Sprintf("- %s\n", d)) }
		b.WriteString("\n")
	}
	if len(errs) > 0 {
		b.WriteString("## 最近错误\n")
		n := min(len(errs), 5)
		for _, e := range errs[len(errs)-n:] { b.WriteString(fmt.Sprintf("- %s\n", e)) }
		b.WriteString("\n")
	}
	os.WriteFile(filepath.Join(dir, "热缓存.md"), []byte(b.String()), 0644)
	writeLog("writer: hot cache written")
}

var idxLastRun time.Time

func triggerIndexer() {
	if time.Since(idxLastRun) < 30*time.Second { return }
	idxLastRun = time.Now()
	script := filepath.Join(os.Getenv("USERPROFILE"), ".reasonix", "logs", "knowledge_indexer.py")
	go exec.Command("python", script).Start()
}


