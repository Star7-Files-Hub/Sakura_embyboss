# 🌸 Sakura_embyboss 初学练习版（重构中）

<p align="center">
<img src="image/bot2.png" alt="bot"><br>
<a href="https://github.com/berry8838/Sakura_embyboss/stargazers"><img src="https://img.shields.io/github/stars/berry8838/Sakura_embyboss" alt="stars"></a> 
<a href="https://github.com/berry8838/Sakura_embyboss/forks"><img src="https://img.shields.io/github/forks/berry8838/Sakura_embyboss" alt="forks"></a> 
<a href="https://github.com/berry8838/Sakura_embyboss/issues"><img src="https://img.shields.io/github/issues/berry8838/Sakura_embyboss" alt="issue"></a>  
<a href="https://github.com/berry8838/Sakura_embyboss/blob/master/LICENSE"><img src="https://img.shields.io/github/license/berry8838/Sakura_embyboss" alt="license"></a> 
<a href="https://hub.docker.com/r/jingwei520/sakura_embyboss" ><img src="https://img.shields.io/docker/v/jingwei520/sakura_embyboss/latest?logo=docker" alt="docker"></a>
<a href="https://hub.docker.com/r/jingwei520/sakura_embyboss/tags" ><img src="https://img.shields.io/badge/platform-amd64%20arm64-pink" alt="plat"></a>
<a href="https://github.com/berry8838/Sakura_embyboss/actions/workflows/publish-docker_on_master.yml">
    <img src="https://img.shields.io/github/actions/workflow/status/berry8838/Sakura_embyboss/publish-docker_on_master.yml?branch=master" alt="Build status" />
</a>
</p>
<br>

## 📜 项目说明（重构中，暂停更新）

- **用Telegram管理Emby用户**（开服） 安装使用 👉 [项目文档](https://berry8838.github.io/Sakura_embyboss)
- **推荐使用 Debian 11操作系统，AMD处理器架构。目前ARM也支持（如有问题请反馈issue）**
- 解决不了大的技术问题（因为菜菜），如需要，请自行fork修改，~~如果能提点有意思的pr更好啦~~
- 反馈请尽量 issue，看到会处理

> **声明：本项目仅供学习交流使用，仅作为辅助工具借助tg平台方便用户管理自己的媒体库成员，对用户的其他行为及内容毫不知情**
<br>

> **本 fork 说明（2026-10-27 同步）**
>
> 在 `berry8838/Sakura_embyboss` 基础上叠加了线上跑的一批改造，源码与运行中的容器一致：
>
> - **会员中心（Mini App）**：`bot/web/miniapp.py` + `miniapp.html/js/css`，含观影榜 / 奶茶榜 / 热播榜（Emby Playback Reporting 统计，支持今日/昨日/近 7 天）、在线人数（近 10 分钟活跃口径）、设备管理、背包与商城、邀请码（标准注册码格式）。
> - **并发限制**：`bot/modules/extra/concurrent_play_monitor.py` + `docs_extra/Concurrent_Play_Limit.md`（按警告次数分级通知）。
> - **注册节流与排队**：`bot/func_helper/register_throttle.py`、`register_queue`，避免批量建号把 Emby 打假死。
> - **分区权限 / 邀请码 / 签到 / 红包 / 保号 / 求片**：`bot/modules/panel/*`、`bot/scheduler/*`。
> - **服务器面板**：密码一键复制、线路公告、单集/整季清理等。
>
> ⚠️ **已脱敏**：示例配置、`docker-compose.yml` 里的口令都是占位符；`miniapp.js` 里的线路域名替换为
> `emby.example.com` / `emby2.example.com` / `white.example.com` / `white2.example.com`，
> Telegram 机器人链接替换为 `t.me/your_bot`。部署前请全部改成自己的。
> `config.json`、`*.session`、`keys/`、`log/`、`db_backup/` 不入库。

## 💐 Our Contributors

<a href="https://github.com/berry8838/Sakura_embyboss/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=berry8838/Sakura_embyboss" />
</a>  

## 特别感谢（排序不分先后）<img src="image/bixin.jpg" alt="比心" height=30>

- [Pyrogram • 一个现代、优雅和异步的MTProto API框架](https://github.com/pyrogram/pyrogram)
- [Nezha探针 • 自托管、轻量级、服务器和网站监控运维工具](https://github.com/naiba/nezha)
- [小宝 • 按钮风格](https://t.me/EmbyClubBot)
- [MisakaF_Emby • 启发](https://github.com/MisakaFxxk/MisakaF_Emby)
  以及  [EMBY API官方文档](https://swagger.emby.media/?staticview=true#/UserService)
- [Nolovenodie • 播放榜单海报推送借鉴](https://github.com/Nolovenodie/EmbyTools)
- [罗宝 • 提供的代码援助](https://github.com/dddddluo)
- [折花 • 日榜周榜推送设计图](https://github.com/U41ovo)<br>


## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=berry8838/Sakura_embyboss&type=Date)](https://star-history.com/#berry8838/Sakura_embyboss)