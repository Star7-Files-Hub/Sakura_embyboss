---
name: Bug report
about: Create a report to help us improve
title: ''
labels: ''
assignees: ''

---

> ⚠️ **提交前请先读这一段（安全提示）**
> 请勿在 issue 中粘贴任何敏感信息，包括但不限于：
> - `config.json` 的全部或部分内容（`bot_token` / `emby_api` / `db_pwd` / `tz_password` /
>   `moviepilot.access_token` / `tracearr_api_key`）；
> - `*.session` / `*.session-journal`（Pyrogram 登录会话，泄露等同于 Telegram 账号被接管）；
> - 数据库口令、服务器公网地址、Telegram / Emby 用户 ID 与真实昵称；
> - 未打码的日志与截图（日志里可能带 token 前缀、API Key 与用户信息）。
>
> 贴日志时请只保留报错堆栈，并先做打码处理。
> **安全漏洞请不要开公开 issue**，请按 [SECURITY.md](../SECURITY.md) 的流程私下报告。

**Describe the bug**
A clear and concise description of what the bug is.

**To Reproduce**
Steps to reproduce the behavior:
1. Go to '...'
2. Click on '....'
3. Scroll down to '....'
4. See error

**Expected behavior**
A clear and concise description of what you expected to happen.

**Screenshots**
If applicable, add screenshots to help explain your problem.
（请先打码：token、API Key、用户 ID、内网地址、域名等）

**Environment**
 - 部署方式: [docker compose / 直接 python3 / systemd]
 - 镜像或 commit: [例如 jingwei520/sakura_embyboss@sha256:... 或 commit sha]
 - OS / 架构: [例如 Debian 12 / amd64]
 - Python 版本（非 docker 部署时）: [例如 3.10.11]

**Desktop (please complete the following information):**
 - OS: [e.g. iOS]
 - Browser [e.g. chrome, safari]
 - Version [e.g. 22]

**Smartphone (please complete the following information):**
 - Device: [e.g. iPhone6]
 - OS: [e.g. iOS8.1]
 - Browser [e.g. stock browser, safari]
 - Version [e.g. 22]

**Additional context**
Add any other context about the problem here.
（再次提醒：不要粘贴 config.json / token / *.session 等敏感内容）
