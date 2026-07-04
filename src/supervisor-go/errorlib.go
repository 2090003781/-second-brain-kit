package main

import (
	"bufio"

	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

// ErrorEntry represents one entry in the error library.
type ErrorEntry struct {
	ID        int
	Title     string
	Frequency int
	Tools     []string
	Keywords  []string
	Solution  string
	Exclude   string // path to exclude from matching
	Domain    string
}

var (
	errorLibrary []ErrorEntry
)

func loadErrorLibrary(vaultPath string) {
	errorLibrary = nil
	path := filepath.Join(vaultPath, "记忆", "错误库.md")
	f, err := os.Open(path)
	if err != nil {
		writeLog("error library: cannot open %s: %v", path, err)
		return
	}
	defer f.Close()
	var current *ErrorEntry
	scanner := bufio.NewScanner(f)
	idCounter := 0
	for scanner.Scan() {
		line := scanner.Text()
		// Detect new entry: ## #N Title
		if strings.HasPrefix(line, "## #") {
			if current != nil {
				errorLibrary = append(errorLibrary, *current)
			}
			idCounter++
			current = &ErrorEntry{ID: idCounter}
			// Extract title after ## #N
			parts := strings.SplitN(line, " ", 3)
			if len(parts) >= 3 {
				current.Title = parts[2]
			}
			continue
		}
		if current == nil {
			continue
		}
		// Parse fields
		if strings.Contains(line, "- **次数：") {
			re := regexp.MustCompile(`\d+`)
			m := re.FindString(line)
			if m != "" {
				current.Frequency, _ = strconv.Atoi(m)
			}
		} else if strings.Contains(line, "- **工具：") {
			vals := extractValues(line)
			current.Tools = vals
		} else if strings.Contains(line, "- **关键词：") {
			vals := extractValues(line)
			current.Keywords = vals
		} else if strings.Contains(line, "- **解决：") {
			vals := extractValues(line)
			if len(vals) > 0 {
				current.Solution = vals[0]
			}
		} else if strings.Contains(line, "- **排除：") {
			vals := extractValues(line)
			if len(vals) > 0 {
				current.Exclude = vals[0]
			}
		} else if strings.Contains(line, "- **领域：") {
			vals := extractValues(line)
			if len(vals) > 0 {
				current.Domain = vals[0]
			}
		}
	}
	if current != nil {
		errorLibrary = append(errorLibrary, *current)
	}
	writeLog("error library: loaded %d entries from %s", len(errorLibrary), path)
}
func extractValues(line string) []string {
	idx := strings.Index(line, "**：")
	if idx < 0 {
		idx = strings.Index(line, ":**")
		if idx < 0 {
			return nil
		}
		idx += 3
	} else {
		idx += 3
	}
	rest := line[idx:]
	// Split by comma
	parts := strings.Split(rest, ",")
	var result []string
	for _, p := range parts {
		p = strings.TrimSpace(p)
		p = strings.Trim(p, "\"")
		if p != "" {
			result = append(result, p)
		}
	}
	return result
}

// dynamicConfidence computes confidence based on frequency + tool match + path.
func dynamicConfidence(entry ErrorEntry, toolName string, args map[string]any) int {
	base := 30
	// Frequency boost
	if entry.Frequency >= 10 {
		base += 40
	} else if entry.Frequency >= 4 {
		base += 20
	} else if entry.Frequency >= 2 {
		base += 10
	}
	// Tool match boost
	tl := strings.ToLower(toolName)
	for _, t := range entry.Tools {
		if strings.ToLower(t) == tl {
			base += 20
			break
		}
	}
	// Path sensitivity
	if p, ok := args["path"].(string); ok {
		if strings.Contains(p, "/etc/") || strings.Contains(p, "/data/") || strings.Contains(p, "/usr/") {
			base += 15
		}
		if strings.Contains(p, "/tmp/") || strings.Contains(p, "test") {
			base -= 15
		}
	}
	// Cap at 0-100
	if base < 0 {
		base = 0
	}
	if base > 100 {
		base = 100
	}
	return base
}

// matchToolError checks if toolResult matches any entry in the error library.
// Returns the best matching entry and confidence.
func matchToolError(toolResult string) (*ErrorEntry, int) {
	bestScore := 0
	var best *ErrorEntry
	tl := strings.ToLower(toolResult)
	for i := range errorLibrary {
		e := &errorLibrary[i]
		if len(e.Keywords) == 0 {
			continue
		}
		matches := 0
		for _, kw := range e.Keywords {
			if strings.Contains(tl, strings.ToLower(kw)) {
				matches++
			}
		}
		score := matches * 100 / len(e.Keywords)
		if score > bestScore {
			bestScore = score
			best = e
		}
	}
	if bestScore >= 50 {
		return best, bestScore
	}
	return nil, 0
}
