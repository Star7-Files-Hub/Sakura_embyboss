# Tracearr 对接

## 功能概述

Tracearr 是一个多服务器监控平台，支持 Plex、Jellyfin 和 Emby。通过对接 Tracearr，EmbyBoss 可以：

- 通过 Tracearr API 获取会话信息
- 通过 Tracearr API 终止会话（作为 Emby API 的备选方案）
- 获取用户播放数据和违规记录

## 前置条件

1. 已部署 Tracearr（[GitHub](https://github.com/connorgallopo/Tracearr)）
2. 已在 Tracearr 中配置好 Emby 服务器
3. 已获取 Tracearr API Key

## 配置说明

在 `config.json` 中添加以下配置项：

```json
{
  "tracearr_enabled": false,
  "tracearr_url": "https://tracearr.example.com",
  "tracearr_api_key": "your-api-key-here"
}
```

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `tracearr_enabled` | bool | `false` | 是否启用 Tracearr 对接 |
| `tracearr_url` | string | `null` | Tracearr 服务地址 |
| `tracearr_api_key` | string | `null` | Tracearr API Key |

## 控制面板操作

在 bot 中输入 `/config` 打开控制面板，找到 **📡 Tracearr对接** 进入设置子面板。

### 设置选项

- **开关 Tracearr 对接**：启用/禁用功能
- **设置 Tracearr 参数**：设置 URL 和 API Key

### 操作示例

```
/config → 📡 Tracearr对接 → 📝 设置Tracearr参数
```

按提示输入：
```
https://tracearr.example.com
your-api-key-here
```

## API Key 获取

1. 登录 Tracearr Web 界面
2. 进入 Settings → API Keys
3. 生成新的 API Key
4. 复制并保存到 config.json

## 功能对比

### 终止会话

| 特性 | EmbyBoss 直接终止 | Tracearr 终止 |
|---|---|---|
| 检查客户端远程控制支持 | ❌ 不检查 | ✅ 检查 `SupportsRemoteControl` |
| 客户端不支持时 | 仍发送命令（可能被忽略） | 报错拒绝执行 |
| 终止可靠性 | 较高（大部分客户端有效） | 较低（仅支持远程控制的客户端） |
| 安全性 | 较低（可能虚假成功） | 较高（避免虚假报告） |

### 获取会话信息

| 特性 | EmbyBoss | Tracearr |
|---|---|---|
| 数据来源 | Emby API `/Sessions` | Tracearr 数据库 |
| 实时性 | 实时 | 取决于 Tracearr 轮询间隔 |
| 历史数据 | ❌ 仅当前 | ✅ 完整历史 |
| 多服务器 | ❌ 单服务器 | ✅ 多服务器聚合 |

## 使用场景

### 场景 1：EmbyBoss 终止失败时，尝试 Tracearr

```python
# 先尝试 EmbyBoss 直接终止
success, fail = await terminate_all_user_sessions(emby_user_id, sessions)

# 如果有失败的，尝试通过 Tracearr 终止
if fail > 0:
    for session in sessions:
        await tracearr_terminate_fallback(
            session.get("Id"),
            reason="同时播放超出限制"
        )
```

### 场景 2：通过 Tracearr 获取更丰富的会话信息

```python
from bot.modules.extra.tracearr_helper import tracearr

ok, sessions = await tracearr.get_sessions()
if ok:
    for s in sessions:
        print(f"User: {s.get('user')}, Title: {s.get('title')}")
```

### 场景 3：查询违规记录

```python
ok, violations = await tracearr.get_violations(acknowledged=False)
if ok:
    for v in violations:
        print(f"Violation: {v.get('type')}, User: {v.get('user')}")
```

## 常见问题

### Q: Tracearr 报错 "Client does not support remote control"

**A:** 这是 Tracearr 的安全设计。Tracearr 在终止会话前会检查客户端是否支持远程控制（`SupportsRemoteControl`），如果不支持则拒绝执行。

**解决方案：**
1. 使用 EmbyBoss 的直接终止方式（不检查客户端能力）
2. 或者忽略该错误，因为客户端可能已经停止播放

### Q: 两种终止方式有什么区别？

**A:** 
- **EmbyBoss 方式**：直接调用 Emby API 发送 `Playing/Stop` 命令，不检查客户端能力。大部分客户端会响应，但部分客户端（如某些 WebView、智能 TV 应用）可能忽略该命令。
- **Tracearr 方式**：先检查 `SupportsRemoteControl`，只有客户端报告支持时才执行。更严谨但可能无法终止部分客户端的流。

### Q: 应该使用哪种方式？

**A:** 建议：
- 主要使用 EmbyBoss 的直接终止方式（更可靠）
- Tracearr 作为备选方案，用于获取更丰富的会话信息和历史数据
- 两种方式可以同时启用，互不冲突

## Tracearr API 参考

Tracearr 提供 REST API，EmbyBoss 对接使用的端点：

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/sessions` | GET | 获取活跃会话 |
| `/api/sessions/{id}/terminate` | POST | 终止会话 |
| `/api/servers` | GET | 获取服务器列表 |
| `/api/users` | GET | 获取用户列表 |
| `/api/violations` | GET | 获取违规记录 |

完整 API 文档请参考 [Tracearr 官方文档](https://docs.tracearr.com/api)。
