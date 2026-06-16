# 贡献指南 · Contributing

感谢你愿意给「个人总体设计部 / life-system-engineering」提贡献！
这是一个基于钱学森系统工程思想的个人自我管理框架，欢迎任何能让它更有用的改进。

## 这个项目的性质

- **方法论 + 小工具**：核心是一套 markdown 文件 + 纯 Python 标准库脚本，不是大型软件。
- **零依赖**：`tools/` 下只用 Python 标准库，**不接受任何 pip 依赖**——这是刻意的，"有 Python 就能跑"是它最大的优点。
- **中文为主**：文档和内容以中文为主，代码注释也用中文。

## 欢迎的贡献

- 🐛 **Bug**：`tools/` 下脚本（`assistant.py` / `server.py` / `jianwen.py` / `prompts.py`）的 bug
- 💡 **工具改进**：看板 UI、CLI 体验、AI Prompt 质量
- 📖 **文档**：README / 系统说明写得更清楚、修错别字、补示例
- 🌍 **翻译**：把核心文档翻译成其他语言（欢迎开 Issue 先认领）
- 🧩 **方法论**：子系统设计、规则库思路的讨论（建议先开 Issue 讨论，别直接改）

## 如何提 Issue

- Bug 用 **Bug report** 模板，新功能用 **Feature request** 模板。
- Bug 说清楚：做了什么、期望什么、实际什么、环境（Python 版本 / OS / 用的哪个 LLM 提供商）。

## 如何提 PR

1. Fork → 建分支：`git checkout -b fix/xxx`
2. 改完本地自测：
   ```bash
   python -m py_compile tools/*.py   # 至少能编译过
   python tools/assistant.py test    # 若改了 AI 相关功能
   python tools/server.py --no-open  # 若改了看板，确认能起来
   ```
3. **保持"零 pip 依赖"**——不要引入新的第三方库。
4. Commit 信息中文英文都行，说清改了什么、为什么（参考 conventional commits：`feat:` / `fix:` / `docs:` / `refactor:`）。
5. 开 PR，在描述里关联相关 Issue。

## 约定

- **代码**：纯 Python 标准库；单个函数 < 50 行；中文注释；和现有风格一致。
- **内容 .md**：仓库里的所有内容文件都是**示例**（硬约束、子系统 Plan、复盘、知识库笔记），别把你自己的真实个人数据写进 PR。
- **安全**：永远不要把 `tools/.env`、`tools/models.json` 或任何密钥写进提交（它们已在 `.gitignore` 里）。

## 关于"防过度系统化"

本项目刻意保持轻量。提功能建议时请想一下总纲领里的原则：

> 记录花的时间不能超过它省的时间。如果某个改进让系统更累更复杂，它可能不值得加。

简单、能跑、低维护 > 大而全。

## 行为准则

友善、就事论事。不同方法适合不同的人，不强加自己的体系给别人。
