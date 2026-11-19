<p align="center"><img src="assets/logo.svg" width="96" alt="CCC: 三个嵌套 C 与自动续跑箭头"></p>

# CCC · 让你的科研与工程 Agent 持续推进

[English](README.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [Agent 协作指南](docs/agent-guide.md) · [Herdr 支持说明](docs/herdr.md)

<picture>
  <source srcset="assets/banner.svg" type="image/svg+xml">
  <img src="assets/banner.png" width="1280" alt="CCC 架构：外层 cmux 工作区与内层多实例 Herdr 面板自动续跑">
</picture>

**CCC 在 macOS 上为 `cmux`（及嵌套在 `cmux` 中的多实例 `Herdr`）里的 Codex 与 Claude Code 提供带原生证据门禁的自动续跑守护。**

它只对真实原生终端完成事件与空输入框行动，严守不篡改用户草稿、不跨面板误发、不因单次投递不明而盲目重放的底线，并在终端 Supervisor TUI 中实时展示工作区、网络与抢位状态。

## 核心能力

| 模块 | 功能说明 |
| --- | --- |
| `cmux` 守护器 (`cmux_codex_watch.py`) | Codex / Claude Code 原生事件发现、外层 `a`/`w` 授权与安全自动续跑 |
| 50 路瞬时并发抢位 (`b 空目录 50`) | 50 路满并发直连 AnyRouter 自动测速策略组（绝不经 Magpie 降级或人工串行排队），未返回 `OK` 前每 1–2 秒持续重试，一旦返回 `OK` 立即锁定就绪槽位 |
| AnyRouter 网络守卫 (`ccc_network_guard.py`) | 监控 Clash Verge / Mihomo `AnyRouter` 自动测速线路，手选节点单次故障立即接替至可达自动节点 |
| `cmux` + 多开 `Herdr` 零配置适配 (`ccc_herdr_auto.py`) | 自动扫描 `~/.config/herdr/herdr.sock` 与 `~/.config/herdr/sessions/*/herdr.sock`，自动隔离外层 `cmux` 表面并接管内层全部 Codex (`0.164`–`0.170+` 含 `app-server` / 子代理树 / `Plan mode` / `max fast`) 与 Claude 面板；账本超阈值自动滚动归档 |
| Supervisor TUI (`cmux_supervisor_tui.py`) | 实时区分展示 `就绪`、`抢位续跑` 与 `异常` 槽位，支持一键工作区批处理与暂停/恢复 |

## 快速开始

环境要求：macOS 13+、Python 3.10+（推荐 Python 3.11+）以及 [cmux](https://cmuxterm.app/)。

```sh
git clone https://github.com/lzhs1995/ccc.git
cd ccc
python3 cmux_codex_watch.py --help
python3 cmux_codex_watch.py status
```

安装并启用守护服务（会更新本机 LaunchAgent）：

```sh
python3 cmux_codex_watch.py install
ccc track-surface FULL_SURFACE_UUID
ccc arm
ccc tui
```

## 给编码 Agent 的入口

请先阅读 [AGENTS.md](AGENTS.md) 与 [docs/agent-guide.md](docs/agent-guide.md)。`CLAUDE.md`、`GEMINI.md` 与 [llms.txt](llms.txt) 共享同一份规范。

发布或提交前请务必执行公开内容脱敏与测试检查：

```sh
python3 -B scripts/check_public_content.py
pytest
```

## 许可协议

本项目及原创 [Logo / Banner 视觉资产](docs/branding.md) 采用 [MIT License](LICENSE) 开源。本项目独立于 cmux、Herdr、OpenAI 与 Anthropic。
