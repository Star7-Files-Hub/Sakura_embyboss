from bot import LOGGER
from bot.sql_helper import Session
from bot.sql_helper.sql_favorites import EmbyFavorites
from bot.sql_helper.sql_emby import get_all_emby, Emby
from bot.func_helper.emby import emby


async def sync_favorites():
    """
    同步所有用户的Emby收藏记录到数据库

    D-M8：旧实现是"先清库、再逐条写入"，而且每条 SQL 各自开事务提交
    （sql_clear_favorites / sql_add_favorites 内部都自己 commit）。
    一旦中途异常或进程被杀，用户原有记录已经被删掉、新的只写了一半，
    并且外层 except 只记一条日志就结束 → 收藏数据静默丢失。
    现在改为**每个用户一个事务**：
      1. 先在事务内删除该用户的旧记录，再写入新记录，全部成功才 commit；
      2. 任何异常都 rollback，用户原有记录原样保留（不会出现"清空后写入一半"）；
      3. 单个用户失败只影响该用户，不再中断整轮同步；
      4. 远端取不到数据时跳过该用户，绝不"先清空再说"。
    """
    LOGGER.info("开始同步用户Emby收藏记录...")

    # 原写法 `Emby.embyid is not None` 是 Python 身份比较，对 SQLAlchemy 列对象求值为常量 True，
    # 传给 get_all_emby 后等价于不过滤（会把没有 embyid 的行也查出来）。必须用 .isnot(None)。
    users = get_all_emby(Emby.embyid.isnot(None))
    if not users:
        LOGGER.warning("没有找到Emby用户")
        return

    ok_users = 0
    failed_users = 0
    skipped_users = 0

    for user in users:
        try:
            # 获取用户的收藏列表
            favorites = await emby.get_favorite_items(emby_id=user.embyid)
            if not favorites:
                # 远端没数据/请求失败时保留数据库现有记录（旧实现同样是 continue）
                LOGGER.warning(f"用户 {user.name} 未取到收藏数据，跳过（保留数据库现有记录）")
                skipped_users += 1
                continue

            # 先解析好全部待写入行（含 await 的补齐名称），再开事务，
            # 避免把网络请求放进数据库事务里。
            # 用 dict 按 item_id 去重：表上有 (embyid, item_id) 唯一约束，
            # 远端返回重复项会让整个事务回滚。
            rows = {}
            for item in favorites.get("Items", []):
                item_id = item.get("Id")
                if not item_id:
                    continue

                # 获取项目名称
                item_name = item.get("Name", "")
                if not item_name:
                    item_name = await emby.item_id_name(emby_id=user.embyid, item_id=item_id) or "未知"

                rows[item_id] = item_name

            # 单用户单事务：删除 + 写入一起提交，失败整体回滚（D-M8）
            with Session() as session:
                try:
                    session.query(EmbyFavorites).filter(
                        EmbyFavorites.embyname == user.name
                    ).delete(synchronize_session=False)

                    for item_id, item_name in rows.items():
                        session.add(EmbyFavorites(
                            embyid=user.embyid,
                            embyname=user.name,
                            item_id=item_id,
                            item_name=item_name,
                        ))

                    session.commit()
                    ok_users += 1
                except Exception:
                    session.rollback()
                    raise

        except Exception as e:
            failed_users += 1
            LOGGER.error(f"同步用户 {user.name} 的Emby收藏记录失败（已回滚，保留原记录）: {str(e)}")

    LOGGER.info(
        f"Emby收藏记录同步完成：成功 {ok_users} 个用户，失败 {failed_users} 个用户，跳过 {skipped_users} 个用户"
    )
