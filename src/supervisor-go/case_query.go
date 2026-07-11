package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

type CaseEntry struct {
	Date    string
	Time    string
	Type    string
	Summary string
	Tool    string
}

var rgAvailable bool

func initCaseQuery() {
	_, err := exec.LookPath("rg")
	rgAvailable = err == nil
	if !rgAvailable {
		writeLog("case-query: rg not found, case injection disabled")
	} else {
		writeLog("case-query: rg available, case injection ready")
	}
}

func QueryCases(vaultPath string, days int, limit int) []CaseEntry {
	if !rgAvailable {
		return nil
	}
	rawDir := filepath.Join(vaultPath, "日志", "reasonix-raw")
	if !dirExists(rawDir) {
		rawDir = filepath.Join(vaultPath, "reasonix-raw")
	}
	now := time.Now()
	var allCases []CaseEntry
	seen := make(map[string]int) // tool+summary → line index for dedup

	for i := 0; i < days; i++ {
		dateStr := now.AddDate(0, 0, -i).Format("2006-01-02")
		dayDir := filepath.Join(rawDir, dateStr)
		if !dirExists(dayDir) {
			continue
		}
		cmd := exec.Command("rg", "--no-heading", "-n", "@status:failed", dayDir)
		output, err := cmd.Output()
		if err != nil {
			continue
		}
		for _, line := range strings.Split(string(output), "\n") {
			line = strings.TrimSpace(line)
			if line == "" {
				continue
			}
			entry := parseRgLine(line, dateStr)
			if entry.Summary == "" {
				continue
			}
			key := entry.Tool + entry.Summary
			if _, exists := seen[key]; !exists {
				seen[key] = len(allCases)
				allCases = append(allCases, entry)
			}
		}
		if len(allCases) >= limit {
			break
		}
	}
	if len(allCases) > limit {
		allCases = allCases[:limit]
	}
	return allCases
}

func parseRgLine(line, dateStr string) CaseEntry {
	entry := CaseEntry{Date: dateStr}
	parts := strings.SplitN(line, ":", 3)
	if len(parts) < 3 {
		return entry
	}
	contentPart := parts[2]
	if idx := strings.Index(contentPart, "|"); idx > 0 {
		entry.Time = strings.TrimSpace(contentPart[:idx])
	}
	rawParts := strings.Split(contentPart, "|")
	if len(rawParts) >= 2 {
		entry.Summary = strings.TrimSpace(rawParts[1])
		if len(entry.Summary) > 120 {
			entry.Summary = entry.Summary[:120]
		}
	}
	for _, part := range rawParts {
		p := strings.TrimSpace(part)
		if strings.HasPrefix(p, "@tool:") {
			entry.Tool = strings.TrimPrefix(p, "@tool:")
		}
	}
	if strings.Contains(contentPart, "@status:failed") && strings.Contains(contentPart, "@marker:error") {
		entry.Type = "用户报错"
	} else if strings.Contains(contentPart, "@status:failed") {
		entry.Type = "工具失败"
	} else if strings.Contains(contentPart, "@marker:decision") {
		entry.Type = "纠正措施"
	} else {
		entry.Type = "异常"
	}
	return entry
}

func FormatCaseInjection(cases []CaseEntry) string {
	if len(cases) == 0 {
		return ""
	}
	var sb strings.Builder
	sb.WriteString("\n## 📝 历史案例（仅供参考）\n")
	for _, c := range cases {
		summary := c.Summary
		if len(summary) > 100 {
			summary = summary[:100] + "..."
		}
		sb.WriteString(fmt.Sprintf("- [%s %s] %s", c.Date, c.Type, summary))
		if c.Tool != "" {
			sb.WriteString(fmt.Sprintf(" (tool: %s)", c.Tool))
		}
		sb.WriteString("\n")
	}
	return sb.String()
}

func dirExists(p string) bool {
	info, err := os.Stat(p)
	return err == nil && info.IsDir()
}
