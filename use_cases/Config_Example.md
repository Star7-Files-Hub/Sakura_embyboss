# 配置说明

## config.json 完整示例

```json
{
  "bot_name": "xxxbot",
  "bot_token": "5701:AAEvAHzsg30",
  "owner_api": 73711,
  "owner_hash": "",
  "owner": xxxxxxx,
  "group": [-100xxxxxx],
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
  "w_anti_chanel_ids": [],
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
    "status": true,
    "git_repo": "berry8838/Sakura_embyboss",
    "commit_sha": null
  },
  "red_envelope": {
    "status": true,
    "allow_private": true
  },
  "api": {
    "status": true,
    "http_url": "0.0.0.0",
    "http_port": 8838,
    "allow_origins": ["*"]
  },
  "concurrent_play_limit_enabled": false,
  "concurrent_play_limit": 2,
  "concurrent_play_warn_threshold": 3,
  "concurrent_play_check_interval": 60,
  "tracearr_enabled": false,
  "tracearr_url": null,
  "tracearr_api_key": null
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

### 🆕 Tracearr 对接

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `tracearr_enabled` | bool | `false` | 是否启用 Tracearr 对接 |
| `tracearr_url` | string | `null` | Tracearr 服务地址 |
| `tracearr_api_key` | string | `null` | Tracearr API Key |

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
