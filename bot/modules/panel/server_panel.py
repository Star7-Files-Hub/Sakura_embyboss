"""
服务器讯息打印

2026-10-14 改造：文案模板换成「线路公告」紧凑版式
  · 线路地址仍由 config.json 的 emby_line / emby_whitelist_line 驱动（面板「设置线路」照旧可用）
  · 去掉原 cr_page_server 生成的服务器监控状态文本块（键盘按钮保留）
  · 白名单两条线路仍只对 lv='a' 用户展示，普通用户 lv='b' 只看主/备用线路
  · 纯文本排版（不含 Markdown 标记），避免解析歧义
"""
from datetime import datetime, timezone, timedelta
from pyrogram import filters
from bot import bot, emby_line, emby_whitelist_line
from bot.func_helper.emby import emby
from bot.func_helper.filters import user_in_group_on_filter
from bot.sql_helper.sql_emby import sql_get_emby
from bot.func_helper.fix_bottons import cr_page_server
from bot.func_helper.msg_utils import callAnswer, editMessage
from bot.func_helper.copy_text import editMessageWithCopy

SEP = '━━━━━━━━━━━━━━'


def _line_text(*blocks: str) -> str:
    """把若干段线路文案拼成一块，空段自动忽略"""
    return '\n'.join(x.strip() for x in blocks if x and x.strip())


@bot.on_callback_query(filters.regex('^server$|^server:') & user_in_group_on_filter)
async def server(_, call):
    data = sql_get_emby(tg=call.from_user.id)
    if not data:
        return await editMessage(call, '⚠️ 数据库没有你，请重新 /start录入')
    await callAnswer(call, '🌐查询中...')
    try:
        # 支持数字ID (Nezha) 和 UUID (Komari)
        server_id = call.data.split(':')[1]
        try:
            server_id = int(server_id)  # 尝试转为整数 (Nezha)
        except ValueError:
            pass  # 保持为字符串 UUID (Komari)
    except IndexError:
        # 第一次查看
        send = await editMessage(call, "**▎🌐查询中...\n\nο(=•ω＜=)ρ⌒☆ 发送bibo电波~bibo~ \n⚡ 点击按钮查看相应服务器状态**")
        if send is False:
            return

    keyboard, sever = await cr_page_server()

    pwd = '空' if not data.pwd else data.pwd
    if data.lv == 'b':
        line = _line_text(emby_line)
    elif data.lv == 'a':
        line = _line_text(emby_line, emby_whitelist_line)
    else:
        line = '**无权查看**'
    try:
        online = await emby.get_current_playing_count()
        if online == -1:
            online = 'Emby服务器断连 ·0'
    except Exception:
        online = 'Emby服务器断连 ·0'
    now = (datetime.now(timezone(timedelta(hours=8)))).strftime('%Y-%m-%d %H:%M')
    text = f'**🎬 ChaPanda｜线路公告**\n' \
           f'🔑 密码：**{pwd}**\n' \
           f'{SEP}\n' \
           f'**⛔️ 禁用客户端**\n' \
           f'❌ 网易爆米花、Emby 官方客户端 / Web\n' \
           f'❌ 第三方魔改客户端、Infuse / VidHub\n' \
           f'❌ 自定义或伪造 UA 的客户端\n' \
           f'💡 请使用服务允许的合规播放器。\n' \
           f'{SEP}\n' \
           f'**🌐 连接方式**\n' \
           f'✅ 直连｜代理｜自行配置反代\n' \
           f'⚠️ 网络效果因地区及运营商而异。\n' \
           f'{SEP}\n' \
           f'**🚀 线路地址**\n' \
           f'{line}\n' \
           f'{SEP}\n' \
           f'📊 在线：**{online}** 人｜更新：{now}\n' \
           f'🔒 严禁泄露线路及账号信息，违规即删号！\n' \
           f'**🍵 ChaPanda｜好片相伴，观影愉快**'
    # iOS 上等宽/加粗文字点一下不复制（只有 Android/桌面版会），额外挂一个 copy_text 按钮；
    # pwd 为「空」时不挂。失败会自动回退 editMessage，行为与改造前一致。
    await editMessageWithCopy(call, text, buttons=keyboard, copy_text=None if pwd == '空' else pwd)
