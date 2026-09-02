# Wiki For Emby bot

## 🌸 Sakura_embyboss

![bot2](https://berry8838.github.io/Sakura_embyboss/assets/images/bot2.png)

### 📜 项目说明

推荐使用 Debian 11 操作系统，AMD 处理器架构的 vps 搭建（现已支持 ARM）

> 解决不了大的技术问题（因为菜菜），如需要，请自行 fork 修改，如果能提点有意思的 pr 就更好啦
> 反馈请尽量 issue，看到会处理
> 再次说明，此为 Emby 开服管理 Bot，个人请 pass

### 声明

本项目仅供学习交流使用，仅作为辅助工具借助 tg 平台方便用户管理自己的媒体库成员，对用户的其他行为及内容毫不知情

---

## 🤝 功能一览

### 基础功能

- **用户面板**：创建账户、绑定未登记账户、换绑 TG、兑换注册码、重置密码、删除账户、显示隐藏指定媒体库
- **服务器**：显示 emby 线路、密码、播放人数，支持多服务器查看（需配置哪吒探针）
- **admin 面板**：管理注册、创建邀请码、开关定时任务、开关兑换商店
- **config 面板**：导出日志、设置探针、设置线路、指定显隐媒体库

### 进阶功能

- 提示加群、退群删号、被拉入非授权群报警并退出
- 命令初始化根据身份显示不同的命令
- 播放时长、媒体播放数排行榜日推周推
- 支持 Docker 部署
- 皮套人自动狙杀（可白名单）
- 红包功能
- 设备数查阅
- 追剧推送（Webhook）
- 客户端过滤（Webhook）
- 分区通行码

### 🆕 同时播放限制检测

- 定时检测 Emby 活跃会话
- 超限后自动终止所有播放流
- 私信警告 + 群内通报
- 超过警告次数自动封禁账号
- 所有限制参数均可通过控制面板调整

### 🆕 Tracearr 对接

- 通过 Tracearr API 获取会话信息
- 通过 Tracearr API 终止会话（备选方案）
- 获取用户播放数据和违规记录

---

## 🎯 命令帮助

> **Tip**：更完整的参数、示例和危险命令说明请看命令大全。

### 普通用户 (member)

| 命令 | 说明 |
|---|---|
| `start` | [私聊] 开启用户面板 |
| `myinfo` | [用户] 查看状态 |
| `count` | [用户] 媒体库数量 |
| `red` | [用户/禁言] 发红包 |
| `srank` | [用户/禁言] 查看计分 |

### 管理员 (admin)

| 命令 | 说明 |
|---|---|
| `kk` | 管理用户 |
| `score` | 加/减积分 |
| `coins` | 加/减币币 |
| `renew` | 调整到期时间 |
| `rmemby` | 删除用户（包括非 tg） |
| `prouser` | 增加白名单 |
| `revuser` | 减少白名单 |
| `syncgroupm` | 消灭不在群的人 |
| `syncunbound` | 消灭未绑定 bot 的 emby 账户 |
| `low_activity` | 手动运行活跃检测 |
| `check_ex` | 手动到期检测 |
| `uranks` | 召唤观影时长榜 |
| `days_ranks` | 召唤播放次数日榜 |
| `week_ranks` | 召唤播放次数周榜 |
| `embyadmin` | 开启 emby 控制台权限 |
| `ucr` | 私聊创建非 tg 的 emby 用户 |
| `uinfo` | 查询指定用户名 |
| `urm` | 删除指定用户名 |

### 主人 (owner)

| 命令 | 说明 |
|---|---|
| `restart` | 重启 bot |
| `proadmin` | 添加 bot 管理 |
| `revadmin` | 移除 bot 管理 |
| `renewall` | 一键派送天数 |
| `coinsall` | 一键派送币币 |
| `callall` | 群发消息 |
| `bindall_id` | 一键更新用户 EmbyID |
| `backup_db` | 手动备份数据库 |
| `config` | 开启 bot 高级控制面板 |
| `extraembylibs_blockall` | 一键关闭所有用户的额外媒体库 |
| `extraembylibs_unblockall` | 一键开启所有用户的额外媒体库 |

> 在 telegram 中，默认的命令符为 `/`，但 embyboss 支持多种前缀：
> `/start` = `.start` = `，start` = `!start` = `。start`

---

## 🆕 同时播放限制检测 - 详细文档

### 功能说明

同时播放限制检测功能可以监控 Emby 服务器上所有用户的活跃播放会话，当某个用户的同时播放流数量超过设定值时，自动执行警告、终止所有流、群内通报，并在超过警告次数后自动封禁账号。

### 配置项

在 `config.json` 中添加：

```json
{
  "concurrent_play_limit_enabled": false,
  "concurrent_play_limit": 2,
  "concurrent_play_warn_threshold": 3,
  "concurrent_play_check_interval": 60
}
```

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `concurrent_play_limit_enabled` | bool | `false` | 是否启用 |
| `concurrent_play_limit` | int | `2` | 每人允许的同时播放流数量 |
| `concurrent_play_warn_threshold` | int | `3` | 警告次数上限，超过自动封禁 |
| `concurrent_play_check_interval` | int | `60` | 检测间隔（秒） |

### 控制面板路径

```
/config → 🎬 同时播放限制
```

可配置：开关、限制数、警告阈值、检测间隔、重置计数

### 工作流程

1. **定时检测**：按设定间隔调用 `/emby/Sessions` 获取所有活跃会话
2. **按用户分组**：将会话按 `UserId` 分组，统计每个用户的播放流数量
3. **超限检测**：检查是否有用户的播放流超过限制
4. **终止流**：对超限用户，终止其所有播放流（发送消息 + `Playing/Stop`）
5. **私信警告**：向违规用户发送私信
6. **群内通报**：在主群发送违规通报
7. **警告计数**：该用户的 `concurrent_warn_count` +1
8. **自动封禁**：如果警告次数 ≥ 阈值，自动禁用该 Emby 账号

### 群内通报示例

```
⚠️ 同时播放限制警告

用户: testuser (TG: 123456789)
Emby ID: abc123def456
当前播放流: 3 个 (限制: 2 个)
检测时间: 2024-01-15 14:30:25
累计警告: 2 / 3 次

  1. 🎬 电影A | 📱 Chrome
  2. 🎬 电影B | 📱 iPhone
  3. 🎬 剧集C | 📱 Android TV

✅ 已终止: 3 个流

⚠️ 再犯 1 次将自动封禁账号！
```

### 注意事项

1. **白名单用户不受限**：建议将管理员账号设为白名单（`lv: a`），避免被误封
2. **检测间隔不宜过短**：建议不低于 30 秒，避免对 Emby 服务器造成压力
3. **与 Emby 原生限制的区别**：Emby 的 `SimultaneousStreamLimit` 策略会在播放开始时拒绝多余流，而本功能是在流已经开始后终止并警告

---

## 🆕 Tracearr 对接 - 详细文档

### 功能说明

Tracearr 是一个多服务器监控平台，支持 Plex、Jellyfin 和 Emby。通过对接 Tracearr，EmbyBoss 可以获取会话信息、终止会话、查询违规记录。

### 配置项

在 `config.json` 中添加：

```json
{
  "tracearr_enabled": false,
  "tracearr_url": "https://tracearr.example.com",
  "tracearr_api_key": "your-api-key-here"
}
```

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `tracearr_enabled` | bool | `false` | 是否启用 |
| `tracearr_url` | string | `null` | Tracearr 服务地址 |
| `tracearr_api_key` | string | `null` | Tracearr API Key |

### 控制面板路径

```
/config → 📡 Tracearr对接
```

### API Key 获取

1. 登录 Tracearr Web 界面
2. 进入 Settings → API Keys
3. 生成新的 API Key
4. 复制并保存到 config.json

### 终止会话对比

| 特性 | EmbyBoss 直接终止 | Tracearr 终止 |
|---|---|---|
| 检查客户端远程控制支持 | ❌ 不检查 | ✅ 检查 |
| 客户端不支持时 | 仍发送命令 | 报错拒绝执行 |
| 终止可靠性 | 较高 | 较低 |
| 安全性 | 较低 | 较高 |

### 常见问题

**Q: Tracearr 报错 "Client does not support remote control"**

A: 这是 Tracearr 的安全设计。Tracearr 在终止会话前会检查 `SupportsRemoteControl`，如果不支持则拒绝执行。解决方案：
- 使用 EmbyBoss 的直接终止方式
- 或忽略该错误

---

## 💐 贡献

欢迎提供 ISSUE 或者 PR

---

## License

SakuraEmbyboss is licensed under GPL-3.0 and available on [GitHub](https://github.com/berry8838/Sakura_embyboss)

---

*最后更新: 2026-04-26*
