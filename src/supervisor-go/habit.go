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

// HabitEntry represents one behavioral habit entry.
type HabitEntry struct {
	ID        int
	Title     string
	Domain    string
	Count     int
	Scenes    []string
	Template  string
	Source    string
	Threshold int
	CountOK   int // times AI followed this habit
}

var (
	habitLibrary []*HabitEntry
	habMu        sync.RWMutex
)

func loadHabitLibrary(vaultPath string) {
	habMu.Lock()
	habitLibrary = nil
	habMu.Unlock()
	path := filepath.Join(vaultPath, "记忆", "习惯库.md")
	f, err := os.Open(path)
	if err != nil {
		writeLog("habits: cannot open %s: %v", path, err)
		return
	}
	defer f.Close()

	var current *HabitEntry
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()

		if strings.HasPrefix(line, "## [") {
			if current != nil {
				current.ID = nextHabitID()
				habMu.Lock()
				habitLibrary = append(habitLibrary, current)
				habMu.Unlock()
			}
			current = &HabitEntry{CountOK: 0}
			// Extract title after ## [Domain]
			if idx := strings.Index(line, "] "); idx > 0 {
				current.Title = strings.TrimSpace(line[idx+2:])
				current.Domain = strings.TrimSpace(line[4:idx])
			}
			continue
		}
		if current == nil {
			continue
		}

		if strings.Contains(line, "- **次数：") {
			re := regexp.MustCompile(`\d+`)
			if m := re.FindString(line); m != "" {
				current.Count, _ = strconv.Atoi(m)
			}
		} else if strings.Contains(line, "- **场景：") {
			current.Scenes = commaSplit(line)
		} else if strings.Contains(line, "- **模板：") {
			vals := commaSplit(line)
			if len(vals) > 0 {
				current.Template = vals[0]
			}
		} else if strings.Contains(line, "- **来源：") {
			vals := commaSplit(line)
			if len(vals) > 0 {
				current.Source = vals[0]
			}
		}
	}
	if current != nil {
		current.ID = nextHabitID()
		habMu.Lock()
		habitLibrary = append(habitLibrary, current)
		habMu.Unlock()
	}
	writeLog("habits: loaded %d entries", len(habitLibrary))
}

func commaSplit(line string) []string {
	idx := strings.Index(line, "**：")
	if idx < 0 {
		idx = strings.Index(line, ":**")
		if idx < 0 {
			return nil
		}
		idx += 3
	} else {
		idx += len("**：")
	}
	parts := strings.Split(line[idx:], ",")
	var res []string
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p != "" {
			res = append(res, p)
		}
	}
	return res
}

// matchHabit checks if toolName+args match any habit scene.
// Returns best matching habit and match percentage.
func matchHabit(toolName string, args map[string]any) (*HabitEntry, int) {
	tl := strings.ToLower(toolName)
	argStr := ""
	for _, v := range args {
		if s, ok := v.(string); ok {
			argStr += " " + s
		}
	}
	argStr = strings.ToLower(argStr)

	bestScore := 0
	var best *HabitEntry

	habMu.RLock()
	defer habMu.RUnlock()
	for _, h := range habitLibrary {
		if len(h.Scenes) == 0 {
			continue
		}
		matches := 0
		for _, scene := range h.Scenes {
			kw := strings.ToLower(strings.TrimSpace(scene))
			if strings.Contains(argStr, kw) || strings.Contains(tl, kw) {
				matches++
			}
		}
		score := matches * 100 / len(h.Scenes)
		if score > bestScore {
			bestScore = score
			best = h
		}
	}

	if bestScore > 0 {
		return best, bestScore
	}
	return nil, 0
}

var (
	habitCounters = make(map[int]*rateEntry)
	habitMu       sync.Mutex
	habitIDSeq    int
)

func nextHabitID() int {
	habitMu.Lock()
	defer habitMu.Unlock()
	habitIDSeq++
	return habitIDSeq
}

func incrementHabitCount(id int) {
	habitMu.Lock()
	defer habitMu.Unlock()
	e, ok := habitCounters[id]
	if !ok {
		habitCounters[id] = &rateEntry{count: 1, firstSeen: time.Now()}
		return
	}
	if time.Since(e.firstSeen) > 5*time.Minute {
		e.count = 1
		e.firstSeen = time.Now()
		return
	}
	e.count++
}

func checkHabitCounter(id int) int {
	habitMu.Lock()
	defer habitMu.Unlock()
	e, ok := habitCounters[id]
	if !ok {
		return 0
	}
	if time.Since(e.firstSeen) > 5*time.Minute {
		e.count = 0
		return 0
	}
	return e.count
}
