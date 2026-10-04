# 使用帮助

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

如图，向列表 `[]` 里面加入数字 id 即可。

---

## 管理按钮 - 定时任务

在 `config.json` 模板中已经说明得很明白，请仔细阅读。

> 唯一需要注意的是：为了记录用户观看数据，请下载 Emby 插件 **playback reporting**

---

## WebHook - 追剧推送

> 🔐 **关于 token（请先读）**
>
> Webhook 的 URL 会出现在 Emby 日志、反向代理 access log 与浏览器历史里，因此
> **不要在这里填 bot 的 token**。请在 `config.json` 的 `api` 段单独设置一个只用于内部调用的令牌：
>
> ```json
> "api": {
>   "status": true,
>   "http_url": "127.0.0.1",
>   "internal_token": "用 openssl rand -hex 32 生成的随机串"
> }
> ```
>
> 下面 URL 里的 `token=` 一律填这个 `internal_token`。
> 若调用方支持自定义请求头，更推荐改用请求头 `X-API-Token: <令牌>`，避免令牌进入 URL 与日志。

### 添加第一个 Webhook（收藏同步）

- **名称**：随便填，例如：favorites
- **URL**：`http://192.168.2.147:8838/emby/webhook/favorites?token=这里填入api.internal_token`
  - 将 IP 地址和端口替换成自己 bot 所在的地址和端口
  - token 填入 `config.json` 里的 `api.internal_token`（**不是 bot token**）
- **事件类型**：选中"添加到'最爱'"、"从'最爱'中移除"

### 添加第二个 Webhook（媒体更新推送）

- **名称**：随便填，例如：medias
- **URL**：`http://192.168.2.147:8838/emby/webhook/medias?token=这里填入api.internal_token`
- **事件类型**：选中"新媒体已添加"

---

## WebHook - 客户端过滤

### 添加 Webhook

- **名称**：随便填，例如：client-filter
- **URL**：`http://192.168.2.147:8838/emby/webhook/client-filter?token=这里填入api.internal_token`
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

### 配置方法

在 `config.json` 中添加：

```json
{
  "concurrent_play_limit_enabled": true,
  "concurrent_play_limit": 2,
  "concurrent_play_warn_threshold": 3,
  "concurrent_play_check_interval": 60,
  "concurrent_play_limit_whitelist_enabled": false,
  "concurrent_play_limit_whitelist": 4
}
```

或通过 bot 控制面板：`/config` → `🎬 同时播放限制`

### 参数说明

| 参数 | 说明 | 建议值 |
|---|---|---|
| `concurrent_play_limit` | 每人允许的同时播放流数量 | 2-3 |
| `concurrent_play_warn_threshold` | 警告次数上限 | 3-5 |
| `concurrent_play_check_interval` | 检测间隔（秒） | 30-120 |
| `concurrent_play_limit_whitelist_enabled` | 白名单用户（`lv: a`）是否也纳入并发限制 | `false`（默认，即白名单豁免） |
| `concurrent_play_limit_whitelist` | 白名单用户适用的上限，仅在上项为 `true` 时生效 | 4-6 |

### 判定顺序

1. **bot 管理员**：始终豁免，硬编码、没有开关可改（即使同时是白名单也豁免）
2. **不在 bot 数据库里的 Emby 账号**（先按 Emby UserId 查库，查不到即属此类）：跳过并记一条 `WARNING`（无法告警/封禁，等于不受限）
3. **白名单（`lv: a`）**：默认豁免；打开「白名单是否受限」后按 `concurrent_play_limit_whitelist` 判定
4. **其他用户**：按 `concurrent_play_limit` 判定

播放流数量**严格大于**该用户适用的上限才会被终止 + 警告（正好等于上限不算超限）。

### 注意事项

1. 白名单用户（`lv: a`）默认**自动豁免**，可在控制面板打开「白名单是否受限」改为按白名单上限判定；**bot 管理员始终豁免**，不受该开关影响
2. 不在 bot 数据库里的 Emby 账号（例如直接建在 Emby 侧）会被跳过并记 warning，等于永久不受并发限制
3. 检测间隔不宜过短，避免对 Emby 服务器造成压力
4. 与 Emby 原生的 `SimultaneousStreamLimit` 不同，本功能是在流已经开始后终止并警告

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

---

*最后更新: 2026-04-26*
