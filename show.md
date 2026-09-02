# 简介

[← 返回首页](index.md)

---

## 🌸 Sakura_embyboss 初学练习版

### 📜 项目说明

推荐使用 Debian 11 操作系统，AMD 处理器架构的 vps 搭建（现已支持 ARM）

> 解决不了大的技术问题（因为菜菜），如需要，请自行 fork 修改，如果能提点有意思的 pr 就更好啦
> 反馈请尽量 issue，看到会处理
> 再次说明，此为 Emby 开服管理 Bot，个人请 pass

### 声明

本项目仅供学习交流使用，仅作为辅助工具借助 tg 平台方便用户管理自己的媒体库成员，对用户的其他行为及内容毫不知情

---

## 🤝 功能一览

### 用户面板

- 创建账户
- 绑定未登记账户、换绑 TG
- 兑换注册码
- 重置密码
- 删除账户
- 显示隐藏指定媒体库（默认不显示 播放列表）

### 服务器

- 显示 emby 线路，密码，播放人数
- 支持多服务器查看，查看服务器信息网速负载等（需要配置哪吒地址 api 等）

### admin 面板

- 管理注册 → 总限额，状态，定时注册
- 创建以及管理邀请码 → code 与深链接 两种形式
- 查看邀请码
- 开关各种定时任务
- 开关兑换商店

### config 面板

- 导出日志
- bot 内设置探针，emby 展示线路，指定显隐媒体库，控制注册码续期，自定义开关充电按钮

### 进阶功能

- 提示加群、退群删号、被拉入非授权群报警并退出
- 命令初始化根据身份显示不同的命令
- 各种命令管理
- 添加用户播放时长，媒体播放数排行榜日推周推
- 支持 docker 部署
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
| `rev_white_channel` | 移除皮套人白名单 |
| `white_channel` | 添加皮套人白名单 |
| `unban_channel` | 解封皮套人 |
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

## 💐 贡献

欢迎提供 ISSUE 或者 PR

---

## License

SakuraEmbyboss is licensed under GPL-3.0 and available on [GitHub](https://github.com/berry8838/Sakura_embyboss)

---

*最后更新: 2026-04-26*
