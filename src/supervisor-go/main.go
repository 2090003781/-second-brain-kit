package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

var (
	vaultPath      string
	port           string
	botSessionsDir string
	supervisionLog string
)

func initPaths() {
	memoryDir = filepath.Join(vaultPath, "记忆")
	supervisionLog = filepath.Join(vaultPath, "监督日志.md")
	qqLogPath = filepath.Join(vaultPath, "个人", "Bot", "QQ-Bot", "日志.md")
	wxLogPath = filepath.Join(vaultPath, "个人", "Bot", "微信-Bot", "日志.md")
}

// ── Config ──
var (
	memoryDir   string
	qqLogPath   string
	wxLogPath   string
	stateFile   string
)

// ── Rules ──
type Rule struct {
	Domain   string
	Keyword  string
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
		log.Printf("cannot read memory dir: %v", err)
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
	log.Printf("loaded %d rules, %d error patterns", len(rules), len(errorPatterns))
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
		t.count = 1; t.lastSeen = time.Now()
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
		if strings.Contains(string(data[:min(2000, len(data))]), "花火") || strings.Contains(string(data[:min(2000, len(data))]), "Sparkle") {
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

// ── TCP Server ──
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
	Domain        string `json:"domain,omitempty"`
	SystemMessage string `json:"systemMessage,omitempty"`
}

func checkToolCall(toolName string, toolArgs map[string]any) *violation {
	tl := strings.ToLower(toolName)
	if checkLoop(toolName) {
		return &violation{
			Violated: true, Rule: "Loop detection",
			Detail: fmt.Sprintf("%s called 3+ times consecutively", toolName),
			Solution: "Change approach or check preconditions first", Domain: "global",
			SystemMessage: fmt.Sprintf("⚠️ Supervisor: loop detected on %s", toolName),
		}
	}
	if tl == "bash" || tl == "echo" || tl == "powershell" || tl == "cmd" {
		for _, v := range toolArgs {
			if s, ok := v.(string); ok && hasChinese(s) {
				return &violation{
					Violated: true, Rule: "GBK encoding conflict",
					Detail: "Command args contain CJK characters, may fail on GBK terminal",
					Solution: "Set encoding=utf-8 or pass via env var", Domain: "global",
					SystemMessage: fmt.Sprintf("⚠️ Supervisor: GBK encoding risk on %s", toolName),
				}
			}
		}
	}
	if tl == "write_file" || tl == "edit_file" || tl == "read_file" {
		if path, ok := toolArgs["path"].(string); ok {
			if hasChinese(path) {
				return &violation{
					Violated: true, Rule: "Chinese file path",
					Detail: "File path contains CJK characters, risk of encoding issues",
					Solution: "Use ASCII paths or verify encoding", Domain: "global",
					SystemMessage: fmt.Sprintf("⚠️ Supervisor: Chinese path on %s", toolName),
				}
			}
			ext := strings.ToLower(filepath.Ext(path))
			if tl == "write_file" && (ext == ".toml" || ext == ".json" || ext == ".yaml" || ext == ".yml") {
				return &violation{
					Violated: true, Rule: "Backup before overwrite",
					Detail: fmt.Sprintf("Writing %s without backup", path),
					Solution: fmt.Sprintf("Run: Copy-Item '%s' '%s.bak' first", path, path),
					Domain: "global",
					SystemMessage: fmt.Sprintf("⚠️ Supervisor: backup required for %s", path),
				}
			}
		}
	}
	return nil
}

func hasChinese(s string) bool {
	for _, r := range s {
		if r >= 0x4e00 && r <= 0x9fff {
			return true
		}
	}
	return false
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

// ── Main ──
func main() {
	flag.StringVar(&vaultPath, "vault", "", "Obsidian vault path (default: D:\\个人数据\\辞玖)")
	flag.StringVar(&port, "port", ":49522", "TCP listen port")
	flag.StringVar(&botSessionsDir, "bot-dir", "", "Bot sessions directory")
	flag.Parse()

	if vaultPath == "" {
		vaultPath = "D:\\个人数据\\辞玖"
	}
	if botSessionsDir == "" {
		botSessionsDir = "C:\\Users\\20900\\AppData\\Roaming\\reasonix\\projects\\C--Users-20900-DeepSeek-Reasonix\\sessions"
	}

	initPaths()
	stateFile = filepath.Join(os.Getenv("USERPROFILE"), ".reasonix", "logs", "bot_sync_state.json")

	log.SetPrefix("[supervisor] ")
	log.SetFlags(log.Ltime | log.Lmsgprefix)

	// Check port conflict
	if conn, err := net.DialTimeout("tcp", "127.0.0.1"+port, 2*time.Second); err == nil {
		conn.Close()
		log.Println("port already in use, another instance running")
		os.Exit(0)
	}

	loadRules()
	loadBotState()

	listener, err := net.Listen("tcp", "127.0.0.1"+port)
	if err != nil {
		log.Fatalf("bind failed: %v", err)
	}
	defer listener.Close()

	log.Printf("running on %s, vault: %s", port, vaultPath)

	// Lifecycle monitor: exit when no reasonix.exe running
	go func() {
		for {
			time.Sleep(30 * time.Second)
			if !isReasonixRunning() {
				log.Println("no reasonix.exe running, shutting down")
				os.Exit(0)
			}
		}
	}()

	// Bot log sync every 2 seconds
	go func() {
		for {
			syncBotLogs()
			time.Sleep(2 * time.Second)
		}
	}()

	// Reload rules every 5 minutes
	go func() {
		for {
			time.Sleep(5 * time.Minute)
			loadRules()
		}
	}()

	for {
		conn, err := listener.Accept()
		if err != nil {
			continue
		}
		go handleConnection(conn)
	}
}
