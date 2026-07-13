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
	sessionMu                                     sync.RWMutex
	_currentSession                                = ""
	_currentTopic                                  = ""
	lastWriterEvent                                time.Time
	lastWriterMu                                   sync.Mutex
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

	// Idle hotcache: write 10min after last event
	go func() {
		for {
			time.Sleep(60 * time.Second)
			lastWriterMu.Lock()
			idle := time.Since(lastWriterEvent) > 10*time.Minute
			lastWriterMu.Unlock()
			if idle && !lastWriterEvent.IsZero() {
				writeLog("writer: idle 10min, writing hot cache")
				writeHotCache()
			}
		}
	}()

	for {
		conn, err := listener.Accept()
		if err != nil { time.Sleep(time.Second); continue }
		go handleWriterConn(conn)
	}
}

func handleWriterConn(conn net.Conn) {
	defer conn.Close()
	defer func() {
		if r := recover(); r != nil {
			writeLog("writer: panic recovered: %v", r)
		}
	}()
	conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	data := make([]byte, 65536)
	n, err := conn.Read(data)
	if err != nil { return }
	var evt writerEvent
	if err := json.Unmarshal(data[:n], &evt); err != nil { return }

	ts := evt.Ts
	if len(ts) < 16 {
		ts = time.Now().Format("2006-01-02T15:04:05")
	}
	date := ts[:10]

	if evt.Event == "SessionStart" {
		initSession()
		sessionMu.Lock()
		_currentSession = fmt.Sprintf("会话-%s", time.Now().Format("150405"))
		_currentTopic = ""
		sessionMu.Unlock()
	}

	// Detect topic marker
	if m := topicSplitPat.FindStringSubmatch(evt.Content); len(m) > 1 {
		sessionMu.Lock()
		_currentTopic = strings.TrimSpace(m[1])
		sessionMu.Unlock()
	}
	sessionMu.RLock()
	ct := _currentTopic
	sessionMu.RUnlock()
	if ct == "" && evt.Prompt != "" {
		if m := topicSplitPat.FindStringSubmatch(evt.Prompt); len(m) > 1 {
			sessionMu.Lock()
			_currentTopic = strings.TrimSpace(m[1])
			sessionMu.Unlock()
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

	// Record event time for idle detection
	lastWriterMu.Lock()
	lastWriterEvent = time.Now()
	lastWriterMu.Unlock()
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
	dir := filepath.Join(vaultPath, "日志", "reasonix-raw", date)
	os.MkdirAll(dir, 0755)
	f, err := os.OpenFile(filepath.Join(dir, name+".md"), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil { return }
	defer f.Close()

	timeStr := ts[11:16]
	event := evt.Event
	tags := buildTags(evt)
	var line string

	switch event {
	case "SessionStart":
		line = fmt.Sprintf("%s | ▶ 会话开始%s\n", timeStr, tags)
	case "SessionEnd":
		line = fmt.Sprintf("%s | ■ 会话结束%s\n", timeStr, tags)
	case "UserPromptSubmit":
		p := evt.Prompt
		// Strip Reasonix system directives
		re := regexp.MustCompile("(?s)<[^>]+>")
		p = re.ReplaceAllString(p, "")
		for _, prefix := range []string{"可见推理", "Final answer", "reasoning-language", "response-language"} {
			if idx := strings.Index(p, prefix); idx >= 0 {
				if end := strings.Index(p[idx:], "\n"); end > 0 {
					p = p[idx+end:]
				}
			}
		}
		p = strings.TrimSpace(p)
		if len(p) > 150 { p = p[:150] }
		line = fmt.Sprintf("%s | %s%s\n", timeStr, p, tags)
	case "PreToolUse":
		line = fmt.Sprintf("%s | → %s%s\n", timeStr, evt.ToolName, tags)
	case "PostToolUse":
		r := evt.ToolResult
		if len(r) > 100 { r = r[:100] }
		if r != "" && (strings.HasPrefix(strings.ToLower(r), "error") || strings.Contains(strings.ToLower(r), "traceback")) {
			line = fmt.Sprintf("%s | ✗ %s: %s%s\n", timeStr, evt.ToolName, r, tags)
		} else {
			line = fmt.Sprintf("%s | ✓ %s%s\n", timeStr, evt.ToolName, tags)
		}
	case "Stop":
		line = fmt.Sprintf("%s | ✔ 结束%s\n", timeStr, tags)
	default:
		c := evt.Content; if len(c) > 100 { c = c[:100] }
		line = fmt.Sprintf("%s | %s%s\n", timeStr, c, tags)
	}
	f.WriteString(line)
}

func writeMarkerLine(date, ts, marker string, evt *writerEvent) {
	name := sessionFileName()
	dir := filepath.Join(vaultPath, "日志", "reasonix-raw", date)
	os.MkdirAll(dir, 0755)
	f, err := os.OpenFile(filepath.Join(dir, name+".md"), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil { return }
	defer f.Close()
	tags := buildTags(evt)
	f.WriteString(fmt.Sprintf("%s | %s%s\n", ts[11:16], marker, tags))
}

// buildTags generates structured tag annotations from a writer event.
// Format: " | @tag:value @tag:value"
func buildTags(evt *writerEvent) string {
	if evt == nil {
		return ""
	}
	var tags []string

	// Event type tag
	if evt.Event != "" {
		tags = append(tags, "@event:"+evt.Event)
		// User prompt tag for easy extraction
		if evt.Event == "UserPromptSubmit" {
			tags = append(tags, "@user_prompt")
		}
	}

	// Tool name tag
	if evt.ToolName != "" {
		tags = append(tags, "@tool:"+evt.ToolName)
	}

	// Status tag (from ToolResult)
	if evt.ToolResult != "" {
		r := strings.ToLower(evt.ToolResult)
		if strings.HasPrefix(r, "error") || strings.Contains(r, "traceback") || strings.Contains(r, "fail") {
			tags = append(tags, "@status:failed")
		} else {
			tags = append(tags, "@status:success")
		}
	}

	// Violation tag (from Content containing violation markers)
	if evt.Content != "" {
		c := strings.ToUpper(evt.Content)
		if strings.Contains(c, "[DECISION") {
			tags = append(tags, "@marker:decision")
		}
		if strings.Contains(c, "[ERROR") {
			tags = append(tags, "@marker:error")
		}
		if strings.Contains(c, "[PREFERENCE") {
			tags = append(tags, "@marker:preference")
		}
		if strings.Contains(c, "[PENDING") {
			tags = append(tags, "@marker:pending")
		}
	}

	if len(tags) == 0 {
		return ""
	}
	return " | " + strings.Join(tags, " ")
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

var (
	idxLastRun time.Time
	idxMu      sync.Mutex
)

func triggerIndexer() {
	// Use mutex to prevent concurrent calls within 30s window
	idxMu.Lock()
	last := idxLastRun
	now := time.Now()
	if now.Sub(last) < 30*time.Second {
		idxMu.Unlock()
		return
	}
	idxLastRun = now
	idxMu.Unlock()
	script := filepath.Join(os.Getenv("USERPROFILE"), ".reasonix", "logs", "knowledge_indexer.py")
	go exec.Command("python", script).Start()
}


