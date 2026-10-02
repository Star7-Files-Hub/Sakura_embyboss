# 安全策略 / Security Policy

## 报告漏洞

请**不要**通过公开 issue 报告安全漏洞。

- 优先使用 GitHub 的私有漏洞报告：仓库页 → `Security` → `Report a vulnerability`。
- 如果该入口不可用，请私下联系仓库维护者（见 `README.md` 中的联系方式）。

报告时请尽量包含：受影响版本 / commit、复现步骤、影响面评估，以及（可选）修复建议。
我们会在确认后尽快回复，并在修复发布后再公开细节。

## 请不要在公开渠道泄露这些内容

本项目的 `config.json` 一旦泄露，等同于把 bot 与 Emby 服务器一起交出去：

| 字段 | 泄露后果 |
| --- | --- |
| `bot_token` | Telegram bot 被完全接管（历史上它还被当作 Web API 的鉴权 token 使用） |
| `emby_api` | 可对 Emby 服务器执行管理操作（封禁/删除用户等） |
| `db_pwd` | 数据库读写权限 |
| `tz_password` / `moviepilot.access_token` | 第三方面板被接管 |
| `*.session`（Pyrogram 会话文件） | 等同于 Telegram 账号被接管 |

因此：

- 提交 issue / 日志 / 截图前，请先打码或删除以上字段；
- 不要粘贴完整的 `config.json`、`*.session`、`docker-compose.yml` 中的真实口令；
- 若怀疑已经泄露，请立即轮换 `bot_token`（BotFather `/revoke`）、Emby API Key、
  数据库口令与 Telegram 会话（删除 `*.session` 后重新登录）。

## 部署基线（强烈建议）

1. `cp config_example.json config.json && chmod 600 config.json`，且确认 `config.json`
   已被 `.gitignore` 忽略、不会进入 Docker 镜像（仓库已提供 `.dockerignore`）。
2. `config.json` 的 `api.http_url` 使用 `127.0.0.1`（只监听回环），
   `api.allow_origins` 只填写自己的反代域名，并为 `api.internal_token`
   设置一个强随机值（反代侧通过 `X-Internal-Token` 携带同一个值）。
3. 数据库使用强随机口令，且不要把 3306 发布到公网（compose 示例已限制为
   `127.0.0.1:3306`）；MySQL 的 `MYSQL_ROOT_HOST` 必须显式写成 `localhost`。
4. 反代（nginx / caddy）启用 HTTPS，不要把 Emby 凭据暴露在明文 HTTP 上。
5. 容器/服务以非 root 用户运行，并保持镜像版本固定（不要用 `:latest`）。
6. 定期执行 `docker compose pull` 与依赖升级，关注 Dependabot / `pip-audit` 告警。
