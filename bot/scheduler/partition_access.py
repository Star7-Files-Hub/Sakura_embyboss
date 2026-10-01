from collections import defaultdict
from datetime import datetime
from typing import List, Set

from bot import partition_libs, bot, LOGGER
from bot.func_helper.emby import emby
from bot.sql_helper.sql_emby import sql_get_emby
from bot.sql_helper.sql_partition import (
    sql_get_active_grants_for_users,
    sql_get_expired_grants,
    sql_mark_grants_expired,
)


async def check_partition_access():
    """
    定时检查分区授权是否到期，过期则收回对应库的访问权限。
    """
    now = datetime.now()
    if not partition_libs:
        return

    expired = sql_get_expired_grants(now)
    if not expired:
        return

    by_user = defaultdict(list)
    for grant in expired:
        by_user[grant.tg].append(grant)

    active_map = sql_get_active_grants_for_users(list(by_user.keys()), now)

    processed_ids: List[int] = []
    failed_tgs: List[int] = []
    failed_ids: List[int] = []

    for tg_id, grants in by_user.items():
        # 每个用户独立容错。原实现把 emby.hide_folders_by_names 放在循环里且不在 try 内，
        # 只要**任一用户**的 Emby 调用抛异常，循环就会中断：
        #   - 其后所有用户都不会被收回权限；
        #   - 循环外的 sql_mark_grants_expired 被跳过，已处理的用户也不会被标记；
        # 配合 "清除全部" 会删除授权记录，用户库权限就被永久保留（fail-open）。
        try:
            emby_row = sql_get_emby(tg=tg_id)
            if not emby_row or not emby_row.embyid:
                processed_ids.extend([g.id for g in grants])
                continue

            # 当前仍然有效的分区集合
            active_parts = {g.partition for g in active_map.get(tg_id, [])}
            keep_libs: Set[str] = set()
            for part in active_parts:
                keep_libs.update(partition_libs.get(part, []))

            # 需要撤销的库集合
            revoke_libs: Set[str] = set()
            for grant in grants:
                revoke_libs.update(partition_libs.get(grant.partition, []))

            # 只隐藏那些不再被其它分区授权覆盖的库
            hide_targets = [lib for lib in revoke_libs if lib not in keep_libs]
            if hide_targets:
                await emby.hide_folders_by_names(emby_row.embyid, hide_targets)

            expired_parts = sorted({g.partition for g in grants})
            expired_parts_text = "、".join(expired_parts)
            if hide_targets:
                hide_targets_text = "、".join(hide_targets)
                notice = (
                    "❌ 分区授权到期提醒\n"
                    f"到期分区：{expired_parts_text}\n"
                    f"已禁用媒体库：{hide_targets_text}"
                )
            else:
                notice = (
                    "ℹ️ 分区授权到期提醒\n"
                    f"到期分区：{expired_parts_text}\n"
                    "本次没有媒体库被禁用（可能仍被其他有效分区覆盖）。"
                )

            try:
                await bot.send_message(tg_id, notice)
            except Exception as e:
                LOGGER.warning("分区到期通知发送失败 tg=%s: %s", tg_id, e)
        except Exception as e:
            # 失败的用户不加入 processed_ids：其授权记录保持 active，下一轮自动重试
            LOGGER.error(f"收回分区权限失败 tg={tg_id}，保留记录待下一轮重试: {e}")
            failed_tgs.append(tg_id)
            failed_ids.extend([g.id for g in grants])
            continue

        processed_ids.extend([g.id for g in grants])

    # 无论循环中是否出现异常，都要落库已处理的结果（失败的用户不在此列）
    try:
        sql_mark_grants_expired(processed_ids)
    except Exception as e:
        LOGGER.error(f"标记分区授权到期失败: {e}")

    if failed_tgs:
        LOGGER.warning(
            f"本轮有 {len(failed_tgs)} 个用户（{len(failed_ids)} 条授权）因异常未收回，"
            f"其记录已保留，将在下一轮重试: {failed_tgs}"
        )
        # 已处理的部分已落库；这里显式抛出，让调用方（如「清除全部」）知道
        # 本轮并非全部成功，从而避免在权限尚未收回时就删除授权记录。
        raise RuntimeError(
            f"有 {len(failed_tgs)} 个用户的分区权限未能收回: {failed_tgs}"
        )
