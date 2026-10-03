# go-emby（自研 Go Emby 服务端）兼容性说明

本页说明 bot 在**非官方 Emby 服务端**上的能力边界与适配方式。

## 背景

部分 Emby 站点使用的并非官方 Emby Server，而是自研的兼容服务端。
本 bot 实测并适配的是开源项目 [sd87671067/go-emby](https://github.com/sd87671067/go-emby)，
它的 `GET /emby/System/Info/Public` 会返回：

```json
{"ProductName": "Go Emby STRM", "Version": "4.8.0.80", "ServerName": "go-emby"}
```

这类服务端**只实现了 Emby API 的一个子集**，而且对未实现的接口返回码并不统一，
容易出现「请求返回成功、但设置根本没生效」的静默失败。

## 如何判断自己的服务端类型

```bash
curl -sk "https://<你的 Emby 地址>/emby/System/Info/Public"
```

- `ProductName` 为 `Go Emby STRM` / 含 `go-emby` → **自研 go-emby**，请看下面的适配说明；
- `ProductName` 为 `Emby Server` → **官方 Emby**，全部功能正常，无需额外配置。

也可以在 bot 的 **配置面板 → 播放速率限制** 里直接看到探测结果（面板会显示「服务端类型」）。

## 能力对照表

| 功能 | bot 使用的接口 | 官方 Emby | go-emby |
| --- | --- | --- | --- |
| 同时播放**检测** | `GET /emby/Sessions` | ✅ | ✅ |
| 建号 | `POST /emby/Users/New` | ✅ | ✅ |
| 改密码 | `POST /emby/Users/{id}/Password` | ✅ | ✅ |
| **封禁 / 解封** | `POST /emby/Users/{id}/Policy` | ✅ | ⚠️ 已适配，改走 `/admin/users` |
| **并发上限** | `POST /emby/Users/{id}/Policy` | ✅ | ⚠️ 已适配，改走 `/admin/users` |
| **终止播放（踢流）** | `POST /emby/Sessions/{id}/Playing/Stop` | ✅ | ❌ 服务端未实现 |
| **播放速率限制** | `POST /emby/Users/{id}/Policy` | ✅ | ❌ 服务端未实现 |
| 媒体库权限 | `POST /emby/Users/{id}/Policy` | ✅ | ❌ 服务端未实现 |

### 为什么 go-emby 上封禁/限速会失效

go-emby 的源码里，`/users/{id}/{sub}` 的路由分发**没有 `policy` 分支**，
因此 `POST /emby/Users/{id}/Policy` 会穿过所有分支落到兜底逻辑，
返回 `204` 但**请求体被直接丢弃**。

同时，`GET` 返回的 `Policy` 是由服务端**硬编码合成**的：

```go
"RemoteClientBitrateLimit": 0,          // 恒为 0，无法设置
"IsDisabled": false,                    // 恒为 false
"SimultaneousStreamLimit": u.Max,       // 真正生效的并发上限，取自 users 表的 max_devices
```

所以：**限速字段在该服务端根本无法设置**（不是配置问题，是服务端没实现），
而并发上限与播放权限要通过它自研的 `/admin/users` 接口来改。

## 配置方法

在 `config.json` 中增加三项：

```json
{
  "emby_server_type": "auto",
  "emby_admin_user": "",
  "emby_admin_password": ""
}
```

| 字段 | 说明 |
| --- | --- |
| `emby_server_type` | `auto`（默认，自动探测）/ `official`（强制按官方 Emby 处理）/ `go_emby`（强制走适配层） |
| `emby_admin_user` | go-emby 的**管理员账号**用户名 |
| `emby_admin_password` | 该管理员账号的密码 |

### 为什么需要管理员账号密码

go-emby 的 `/admin/*` 接口守卫是：

```go
if !u.Admin || u.API { fail(w, 403, "需要管理员账号") }
```

即 **Emby API Key 会被明确拒绝**，必须是真实管理员账号登录换来的 token。
bot 会用这组凭据调用 `POST /emby/Users/AuthenticateByName` 换取 token（有效期 30 天）
并缓存复用，**不会**每次操作都重新登录（该接口有 1 分钟 15 次的频率限制）。

> **安全建议**：建议在 go-emby 后台**单独创建一个 admin 权限账号**专供 bot 使用，
> 而不要直接使用站点主管理员账号。`config.json` 含明文密码，请确保其文件权限收紧
> （例如 `chmod 600 config.json`），且不要提交到版本库。

### 只填 `auto` 不填管理员凭据会怎样

- **检测**仍然正常（`GET /emby/System/Info/Public` 无需鉴权）；
- 但**封禁/解封、并发上限不会生效** —— bot 会退回标准 Policy 写法，
  而 go-emby 会静默丢弃它，日志无报错。**这是最容易踩的坑**，请务必配好凭据。

## 在 go-emby 上的实际行为

配好凭据后：

- **封禁 / 解封** → `PUT /admin/users`，`AllowPlayback=false/true`。
  该服务端在禁止播放时还会一并删除该用户的播放记录。
- **并发上限** → `PUT /admin/users` 的 `MaxDevices`（取值 **1–100**）。
  注意它是「设备数」上限，在 go-emby 中同时被用作 `SimultaneousStreamLimit`。
- **建号** → 建号后自动按 `concurrent_play_limit` 写入该用户的 `MaxDevices`。
- **播放速率限制** → 面板会直接提示「当前服务端不支持」，点「立即应用」也会被拒绝，
  不会发送注定无效的请求。

> ⚠️ go-emby 的 `PUT /admin/users` 是
> `UPDATE users SET max_devices=?, admin=? WHERE id=?`，
> 即**未提交的字段会被一起覆盖**。bot 在修改前会先读取该用户当前值补齐，
> 因此只改播放权限时不会把管理员降权、也不会把并发上限清零。

## 常见问题

**Q: 面板显示「未探测到」或服务端类型为空？**
A: 检查 `emby_url` 是否可访问；`auto` 模式下探测失败会退化为按官方 Emby 处理。

**Q: 配了管理员凭据，但封禁仍然不生效？**
A: 依次检查：
1. `emby_admin_user` / `emby_admin_password` 是否正确（对照 bot 日志里的
   `go-emby 管理员登录失败` 之类提示）；
2. 该账号在 go-emby 里是否真的是 admin 权限；
3. 该账号是否被禁用了播放权限（被禁用的账号无法播放，但仍可登录）。

**Q: 为什么踢流没用？**
A: go-emby 未实现 `POST /emby/Sessions/{id}/Playing/Stop`（返回 404）。
在它上面「超限惩罚」只能靠封禁（`AllowPlayback=false`）完成，无法远程终止播放会话。

**Q: 换回官方 Emby 会出问题吗？**
A: 不会。把 `emby_server_type` 设为 `auto` 或 `official` 即可，
所有适配逻辑都有 `is_go_emby()` 前置判断，官方 Emby 走的是与原来完全一致的代码路径。
