# 使用帮助

[← 返回首页](../index.md)

---

## 用户功能

### 换绑与绑定的区别

- **换绑**：本来有 Emby，只是 TG 号被封了，可以自行换绑到当前账户
- **绑定**：本来有 Emby，但是未绑定过 TG，现在需要绑定到 TG

### 内联模式搜片

1. 打开 @Botfather
2. 开启内联模式
3. 编辑内联占位语

@Botfather 选择你创建的 bot，进入页面点击 **Bot Settings** → **Inline Mode** → **Turn On**

点击 **Edit inline placeholder**，回复：`搜索Emby`

> 未注册用户无法使用

注册用户可以在任意会话输入 `@bot_name [空格] [搜索影片名]`，第一次使用后，只需要输入一个 `@`，tg 会自动联想展示 bot。

---

## 服务器按钮 - Nezha 探针

把后台 `api_token` 拿到，然后在 `config.json` 输入要监控的 id。

---

## 管理按钮 - 定时任务

在 `config.json` 模板中已经说明得很明白，请仔细阅读。

> 唯一需要注意的是：为了记录用户观看数据，请下载 Emby 插件 **playback reporting**

---

## WebHook - 追剧推送

### 添加第一个 Webhook（收藏同步）

- **名称**：随便填，例如：favorites
- **URL**：`http://192.168.2.147:8838/emby/webhook/favorites?token=这里填入bot的token`
- **事件类型**：选中"添加到'最爱'"、"从'最爱'中移除"

### 添加第二个 Webhook（媒体更新推送）

- **名称**：随便填，例如：medias
- **URL**：`http://192.168.2.147:8838/emby/webhook/medias?token=这里填入bot的token`
- **事件类型**：选中"新媒体已添加"

---

## WebHook - 客户端过滤

### 添加 Webhook

- **名称**：随便填，例如：client-filter
- **URL**：`http://192.168.2.147:8838/emby/webhook/client-filter?token=这里填入bot的token`
- **事件类型**：
  - 播放：开始、暂停、取消暂停、停止
  - 用户：已验证用户身份、无法验证用户身份

配置 `config.json` 中的客户端过滤选项，配置完成后可实现自动拦截可疑客户端。

---

## 🆕 同时播放限制检测

### 功能说明

定时检测 Emby 活跃会话，当用户同时播放流超过设定值时：
1. 终止该用户所有播放流
2. 私信警告用户
3. 在群内通报违规事件
4. 超过警告次数自动封禁账号

详细说明请查看：[同时播放限制检测](Concurrent_Play_Limit.md)

### 配置方法

在 `config.json` 中添加：

```json
{
  "concurrent_play_limit_enabled": true,
  "concurrent_play_limit": 2,
  "concurrent_play_limit_warn_threshold": 3,
  "concurrent_play_limit_check_interval": 60
}
```

或通过 bot 控制面板：`/config` → `🎬 同时播放限制`

---

## 🆕 Tracearr 对接

### 功能说明

通过 Tracearr API 获取会话信息、终止会话、查询违规记录。

详细说明请查看：[Tracearr 对接](Tracearr_Integration.md)

### 配置方法

在 `config.json` 中添加：

```json
{
  "tracearr_enabled": true,
  "tracearr_url": "https://tracearr.example.com",
  "tracearr_api_key": "your-api-key"
}
```

或通过 bot 控制面板：`/config` → `📡 Tracearr对接`

---

## 其他设置说明

| 设置 | 说明 |
|---|---|
| 导出日志 | 导出 bot 运行日志 |
| 设置探针 | 在 bot 内设置 Nezha 探针 |
| emby 线路 | 设置显示给用户的 emby 地址 |
| 显示/隐藏指定媒体库 | 指定用户可以隐藏和显示的媒体库 |
| 注册码续期 | 开启时注册码可叠加时长 |
| 退群封禁 | 用户退群时直接封禁 |
| 观影奖励结算 | 看片榜结算时给予积分奖励 |
| 同时播放限制 | 🆕 检测并限制用户同时播放流数量 |
| Tracearr 对接 | 🆕 对接 Tracearr 监控平台 |

---

*最后更新: 2026-04-26*
