# Changelog

All notable changes to the Second Brain Kit are documented here.

---

## [v2.3.1] — 2026-07-10

### 优化
- **缓存命中率优化**：新增 `context_inject.py` 按需知识检索脚本，替代 27KB 全量知识索引自动注入，减少 system prompt 体积波动
- **vault-template 结构重整**：`项目/第二大脑/` → `系统设计/`，统一存放系统架构文档、维护记录、功能更新日志
- `knowledge_indexer.py` 添加 CLI 子命令：`context_inject`、`search`、`stable`，支持命令行调用

### 文档
- AGENTS.md 添加缓存策略分层说明（🟢稳定/🟡常量/🟠按需/🔴会话）
- 新增 `系统设计/维护记录/热缓存优化.md` 缓存基线追踪

## [v2.3] — 2026-07

### 变更
- **规则库统一**：游戏开发规则合并进 `记忆/规则库.md`，拆分方式淘汰
- **流程库→技能库**：统一命名，重复内容合并
- **vault-template 清理**：只保留示例文件，删除冗余占位

### 修复
- 游戏开发规则与通用规则重复检测
- Supervisor 重启循环问题（`ensure_supervisor` + lifecycle monitor 冲突）

## [v2.1.2] — 2026-06

### 变更
- 日志格式改为 AI 可读的紧凑格式，减少 token 消耗
- bot 日志与 AI 会话日志分离

## [v2.1.1] — 2026-06

### 变更
- `obsidian_writer` 合并进 Go supervisor，移除 Python writer 进程
- 进程数量 3→2，降低资源占用

## [v2.1] — 2026-06

### 新增
- **实时知识索引**：Obsidian vault 写入后自动重建 `knowledge_index.json`
- 结构化日志格式

### 变更
- README 更新三层架构图

## [v2.0] — 2026-06

### 新增
- **知识索引器**（`knowledge_indexer.py`）：从 vault 知识库构建可搜索索引
- **习惯库**（`记忆/习惯库.md`）：按场景匹配的自适应协作偏好
- **Go supervisor 守护进程**：规则检查、循环检测、bot 日志同步

### 变更
- Python supervisor → Go binary 迁移完成
- 架构重组为 Agent + 小脑（Go）+ 大脑（Obsidian）三层
- NSFW 话题路由增强

### 修复
- TCP 自动重连
- GBK 编码导致 emoji 输出崩溃（UTF-8 encoding wrapper）
- vault 路径排除逻辑

## [v0.1] — 初始版本

- Second Brain Kit 首次发布

[Unreleased]: https://github.com/2090003781/-second-brain-kit/compare/v2.3.1...HEAD
[v2.3.1]: https://github.com/2090003781/-second-brain-kit/compare/v2.3...v2.3.1
[v2.3]: https://github.com/2090003781/-second-brain-kit/compare/v2.1.2...v2.3
[v2.1.2]: https://github.com/2090003781/-second-brain-kit/compare/v2.1.1...v2.1.2
[v2.1.1]: https://github.com/2090003781/-second-brain-kit/compare/v2.1...v2.1.1
[v2.1]: https://github.com/2090003781/-second-brain-kit/compare/v2.0...v2.1
[v2.0]: https://github.com/2090003781/-second-brain-kit/compare/v0.1...v2.0
[v0.1]: https://github.com/2090003781/-second-brain-kit/releases/tag/v0.1
