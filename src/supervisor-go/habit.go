package main

import (
	"bufio"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

type HabitEntry struct {
	ID       int
	Title    string      // [编程] 配置修改
	Domain   string      // 编程
	Scene    string      // .toml, .conf, 配置
	Keywords []string    // parsed from Scene
	Template string      // 备份 → 编辑 → 验证 → 告知
	Count    int
	Threshold int        // 0-100, default 50
}

var (
	habitLibrary []*HabitEntry
	habitMu      sync.RWMutex
	// Per-habit sliding 5min counter for 50-80% matches
	habitCounter   = make(map[int]*rateEntry)
	habitCounterMu sync.Mutex
)

func loadHabitLibrary(vaultPath string) {
	path := filepath.Join(vaultPath, "记忆", "习惯库.md")
	f, err := os.Open(path)
	if err != nil {
		writeLog("habit: cannot open %s: %v", path, err)
		return
	}
	defer f.Close()

	var current *HabitEntry
	scanner := bufio.NewScanner(f)
	id := 0
	domainRe := regexp.MustCompile(`\[(.+?)\]`)

	for scanner.Scan() {
		line := scanner.Text()

		if strings.HasPrefix(line, "## [") {
			if current != nil {
				habitLibrary = append(habitLibrary, current)
			}
			id++
			current = &HabitEntry{ID: id, Threshold: 50}
			m := domainRe.FindStringSubmatch(line)
			if len(m) > 1 {
				current.Domain = m[1]
			}
			// Title after domain tag
			parts := strings.SplitN(line, " ", 3)
			if len(parts) >= 3 {
				current.Title = parts[2]
			}
			continue
		}

		if current == nil {
			continue
		}

		if strings.Contains(line, "- **次数：") {
			re := regexp.MustCompile(`\d+`)
			m := re.FindString(line)
			if m != "" {
				current.Count, _ = strconv.Atoi(m)
			}
		} else if strings.Contains(line, "- **触发场景：") {
			v := extractValues(line)
			current.Keywords = v
			current.Scene = strings.Join(v, ", ")
		} else if strings.Contains(line, "- **模板：") {
			v := extractValues(line)
			if len(v) > 0 {
				current.Template = v[0]
			}
		} else if strings.Contains(line, "- **阈值：") {
			re := regexp.MustCompile(`\d+`)
			m := re.FindString(line)
			if m != "" {
				current.Threshold, _ = strconv.Atoi(m)
			}
		}
	}
	if current != nil {
		habitLibrary = append(habitLibrary, current)
	}
	writeLog("habit: loaded %d entries", len(habitLibrary))
}

// matchHabit returns the best matching habit entry and match score (0-100).
func matchHabit(toolName string, args map[string]any) (*HabitEntry, int) {
	habitMu.RLock()
	defer habitMu.RUnlock()

	// Build a search string from tool + args
	search := strings.ToLower(toolName + " " + flattenArgs(args))

	bestScore := 0
	var best *HabitEntry

	for _, h := range habitLibrary {
		if len(h.Keywords) == 0 {
			continue
		}
		matches := 0
		for _, kw := range h.Keywords {
			kw = strings.ToLower(strings.TrimSpace(kw))
			if strings.Contains(search, kw) {
				matches++
			}
		}
		score := matches * 100 / len(h.Keywords)
		if score > bestScore {
			bestScore = score
			best = h
		}
	}
	return best, bestScore
}

// checkHabitCounter increments and returns count for a 50-80% match.
func checkHabitCounter(habitID int) int {
	habitCounterMu.Lock()
	defer habitCounterMu.Unlock()

	now := time.Now()
	e, ok := habitCounter[habitID]
	if !ok {
		habitCounter[habitID] = &rateEntry{count: 1, firstSeen: now}
		return 1
	}
	if now.Sub(e.firstSeen) > 5*time.Minute {
		e.count = 1
		e.firstSeen = now
		return 1
	}
	e.count++
	return e.count
}

func flattenArgs(args map[string]any) string {
	var parts []string
	for k, v := range args {
		parts = append(parts, k)
		if s, ok := v.(string); ok {
			parts = append(parts, s)
		}
	}
	return strings.Join(parts, " ")
}

// updateHabitThreshold adjusts habit threshold based on outcome.
func updateHabitThreshold(habitID int, delta int) {
	habitMu.Lock()
	defer habitMu.Unlock()
	for _, h := range habitLibrary {
		if h.ID == habitID {
			h.Threshold += delta
			if h.Threshold < 10 {
				h.Threshold = 10
			}
			if h.Threshold > 90 {
				h.Threshold = 90
			}
			break
		}
	}
}

// incrementHabitCount adds 1 to habit frequency.
func incrementHabitCount(habitID int) {
	habitMu.Lock()
	defer habitMu.Unlock()
	for _, h := range habitLibrary {
		if h.ID == habitID {
			h.Count++
			break
		}
	}
}
