# 配置说明

## config.json 完整示例

```json
{
  "bot_name": "xxxbot",
  "bot_token": "123456789:AAYourBotTokenPlaceholderxxxxxxxxx",
  "owner_api": 73711,
  "owner_hash": "",
  "owner": 1234567890,
  "group": [-1001234567890],
  "main_group": "Aaaaa_su",
  "chanel": "su_yxfy",
  "bot_photo": "https://telegra.ph/file/3b6cd2a89b652e72e0d3b.png",
  "admins": [],
  "money": "花币",
  "emby_api": "xxxxx",
  "emby_url": "http://255.255.255.255:8096",
  "emby_line": "susuyyds.com",
  "emby_whitelist_line": null,
  "blocked_clients": [
    ".*curl.*",
    ".*wget.*",
    ".*python.*",
    ".*bot.*",
    ".*spider.*",
    ".*crawler.*",
    ".*scraper.*",
    ".*downloader.*",
    ".*aria2.*",
    ".*youtube-dl.*",
    ".*yt-dlp.*",
    ".*ffmpeg.*",
    ".*vlc.*"
  ],
  "client_filter_enabled": false,
  "client_filter_mode": "blacklist",
  "allowed_clients": [".*"],
  "client_filter_terminate_session": true,
  "client_filter_block_user": false,
  "line_filter_terminate_session": true,
  "line_filter_block_user": false,
  "partition_libs": {},
  "db_host": "localhost",
  "db_user": "",
  "db_pwd": "",
  "db_name": "",
  "db_port": 3306,
  "emby_block": ["nsfw"],
  "extra_emby_libs": ["电视"],
  "open": {
    "stat": false,
    "all_user": 1000,
    "register_worker_count": 5,
    "register_queue_limit": 100,
    "timing": 0,
    "tem": 0,
    "allow_code": true,
    "checkin": true,
    "exchange": true,
    "whitelist": true,
    "invite": false,
    "leave_ban": true,
    "uplays": true,
    "exchange_cost": 100,
    "whitelist_cost": 9999,
    "invite_cost": 1000,
    "srank_cost": 5,
    "use_whitelist_code": true
  },
  "tz_ad": "",
  "tz_api": "",
  "tz_id": [],
  "tz_version": "v0",
  "tz_username": "",
  "tz_password": "",
  "tz_note": "tz_version 可选值: v0 (Nezha V0 Token认证), v1 (Nezha V1 用户名密码认证), komari (Komari API Key认证)",
  "ranks": {
    "logo": "SAKURA",
    "backdrop": false
  },
  "schedall": {
    "dayrank": true,
    "weekrank": true,
    "dayplayrank": false,
    "weekplayrank": false,
    "check_ex": true,
    "partition_check": true,
    "low_activity": false,
    "backup_db": false
  },
  "db_is_docker": true,
  "db_docker_name": "mysql",
  "db_backup_dir": "./db_backup",
  "db_backup_maxcount": 7,
  "w_anti_channel_ids": [],
  "kk_gift_days": 30,
  "fuxx_pitao": true,
  "activity_check_days": 21,
  "freeze_days": 5,
  "proxy": {
    "scheme": "",
    "hostname": "",
    "port": null,
    "username": "",
    "password": ""
  },
  "moviepilot": {
    "status": false,
    "host": null,
    "username": null,
    "password": null,
    "access_token": null,
    "price": 1
  },
  "auto_update": {
    "status": false,
    "git_repo": "berry8838/Sakura_embyboss",
    "commit_sha": null
  },
  "red_envelope": {
    "status": true,
    "allow_private": true
  },
  "api": {
    "status": true,
    "http_url": "127.0.0.1",
    "http_port": 8838,
    "allow_origins": [],
    "internal_token": null,
    "expose_docs": false
  },
  "concurrent_play_limit_enabled": false,
  "concurrent_play_limit": 2,
  "concurrent_play_warn_threshold": 3,
  "concurrent_play_check_interval": 60,
  "concurrent_play_limit_whitelist_enabled": false,
  "concurrent_play_limit_whitelist": 4,
  "playback_rate_limit_enabled": false,
  "playback_rate_limit": 8,
  "playback_rate_limit_whitelist": 20,
  "emby_server_type": "auto",
  "emby_admin_user": "",
  "emby_admin_password": ""
}
```

---

## 配置字段说明

### 基础配置

| 字段 | 类型 | 说明 |
|---|---|---|
| `bot_name` | string | bot 用户名 |
| `bot_token` | string | bot Token |
| `owner_api` | int | Telegram API ID |
| `owner_hash` | string | Telegram API Hash |
| `owner` | int | 主人 TG ID |
| `group` | list[int] | 授权群组 ID 列表 |
| `main_group` | string | 主群组用户名 |
| `chanel` | string | 频道用户名 |
| `admins` | list[int] | 管理员 TG ID 列表 |

### Emby 配置

| 字段 | 类型 | 说明 |
|---|---|---|
| `emby_api` | string | Emby API Key |
| `emby_url` | string | Emby 服务器地址 |
| `emby_line` | string | 展示给普通用户的线路 |
| `emby_whitelist_line` | string | 展示给白名单用户的线路 |
| `emby_block` | list[string] | 默认隐藏的媒体库 |
| `extra_emby_libs` | list[string] | 额外媒体库 |

### 客户端过滤

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `client_filter_enabled` | bool | `false` | 是否启用客户端过滤 |
| `client_filter_mode` | string | `"blacklist"` | 过滤模式：blacklist/whitelist |
| `blocked_clients` | list[string] | `[]` | 黑名单客户端正则列表 |
| `allowed_clients` | list[string]` | `[".*"]` | 白名单客户端正则列表 |
| `client_filter_terminate_session` | bool | `true` | 检测到可疑客户端时是否终止会话 |
| `client_filter_block_user` | bool | `false` | 检测到可疑客户端时是否封禁用户 |

### 🆕 同时播放限制

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `concurrent_play_limit_enabled` | bool | `false` | 是否启用同时播放限制检测 |
| `concurrent_play_limit` | int | `2` | 每人允许的同时播放流数量 |
| `concurrent_play_warn_threshold` | int | `3` | 警告次数上限，超过自动封禁 |
| `concurrent_play_check_interval` | int | `60` | 检测间隔（秒） |
| `concurrent_play_limit_whitelist_enabled` | bool | `false` | 白名单用户是否也纳入并发限制；`false` = 白名单豁免 |
| `concurrent_play_limit_whitelist` | int | `4` | 白名单用户适用的上限，仅在上项为 `true` 时生效 |

### 🆕 播放速率限制

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `playback_rate_limit_enabled` | bool | `false` | 是否启用播放速率限制（Emby 用户策略的 `RemoteClientBitrateLimit`） |
| `playback_rate_limit` | int | `8` | 普通用户的码率上限（MB/s），`0` = 不限速；写入 Emby 时按 `MB/s × 8 × 1024 × 1024` 换算成 bit/s |
| `playback_rate_limit_whitelist` | int | `20` | 白名单用户（`lv: a`）的码率上限（MB/s），`0` = 不限速 |

> 📌 面板输入范围 0-999；bot 管理员与 Emby 管理员始终不限速（写 `0`）；新用户建档时自动生效，存量用户需在面板点「⚡ 立即应用到全部用户」。详见 [播放速率限制](Playback_Rate_Limit.md)。

> ⚠️ 本功能依赖 Emby 官方服务端。若服务端是自研 **go-emby**（`ProductName` 为 `Go Emby STRM`），
> 其 Policy 写接口是空转的，限速无法生效（面板会明确提示）。详见 [go-emby 兼容性说明](Go_Emby_Compatibility.md)。

### 🆕 Emby 服务端兼容（`emby_server_type` 等）

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `emby_server_type` | str | `"auto"` | `auto` = 自动探测；`official` = 强制按官方 Emby 处理；`go_emby` = 强制走 go-emby 适配层 |
| `emby_admin_user` | str | `""` | go-emby 的管理员账号用户名（**仅 go-emby 需要**） |
| `emby_admin_password` | str | `""` | 该管理员账号密码（**仅 go-emby 需要**） |

> 📌 自研 **go-emby** 服务端未实现 `POST /emby/Users/{id}/Policy`（返回 204 但丢弃请求体），
> 因此封禁/解封与并发上限会改走它自研的 `/admin/users` 接口 —— 该接口**只认真实管理员账号换来的 token**，
> Emby API Key 会被 403 拒绝，所以必须填上面两项，否则**封禁与并发上限会静默失效**（日志无报错）。
> 官方 Emby 无需填写，留空即可。
>
> ⚠️ 建议单独创建一个 admin 权限账号专供 bot 使用；`config.json` 含明文密码，请收紧文件权限
> （如 `chmod 600 config.json`）且不要提交到版本库。详见 [go-emby 兼容性说明](Go_Emby_Compatibility.md)。

### 🔒 API 服务（`api`）

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `status` | bool | `false` | 是否启用内置 FastAPI 服务 |
| `http_url` | string | `"127.0.0.1"` | **监听地址**。默认仅本机回环，让同机反代（nginx/caddy）可访问而不暴露到公网。**除非确有需要，不要改成 `"0.0.0.0"`** ——那会把你所有 API 端点（含内部端点）直接暴露到网络。 |
| `http_port` | int | `8838` | 监听端口 |
| `allow_origins` | list[string] | `[]` | 允许的跨域来源白名单。**默认空 = 禁止一切跨域请求**（同源访问不受影响）。若你的前端与 API 不同源，在这里逐条列出其域名，**不要使用 `["*"]`**。 |
| `internal_token` | string | `null` | **内部端点令牌**。反代访问 `/emby/ban_playlist`、`/emby/line_report` 时必须通过 `X-Internal-Token` 请求头携带该值。留空则仅允许回环地址（`127.0.0.1`/`::1`）访问内部端点。 |
| `expose_docs` | bool | `false` | 是否开放 `/docs`、`/redoc`、`/openapi.json`。默认关闭，避免无鉴权泄露端点与参数清单。 |

> **部署提示（重要）**
>
> 1. 内部端点不再接受 `bot_token` 作为凭据（避免把 bot 凭据写进前端或 URL）。请在上游反代里配置 `X-Internal-Token`，取值与 `api.internal_token` 一致；例如 nginx：
>    ```nginx
>    proxy_set_header X-Internal-Token "你的内部令牌";
>    ```
> 2. 若 `api.internal_token` 留空，只有来自本机回环的请求能访问内部端点。
> 3. `http_url` 设为 `127.0.0.1` 后，**只有同机反代/进程**能访问 API。跨机访问必须经反代，不要直接放开监听地址。

### 探针配置

| 字段 | 类型 | 说明 |
|---|---|---|
| `tz_ad` | string | 探针地址 |
| `tz_api` | string | 探针 API Token |
| `tz_id` | list | 监控的节点 ID |
| `tz_version` | string | API 版本：v0/v1/komari |
| `tz_username` | string | V1 用户名 |
| `tz_password` | string | V1 密码 |

### 定时任务

| 字段 | 类型 | 说明 |
|---|---|---|
| `schedall.dayrank` | bool | 播放日榜 |
| `schedall.weekrank` | bool | 播放周榜 |
| `schedall.dayplayrank` | bool | 观影日榜 |
| `schedall.weekplayrank` | bool | 观影周榜 |
| `schedall.check_ex` | bool | 到期保号 |
| `schedall.low_activity` | bool | 活跃保号 |
| `schedall.backup_db` | bool | 自动备份数据库 |
| `schedall.partition_check` | bool | 分区授权检查 |

---

*最后更新: 2026-04-26*
