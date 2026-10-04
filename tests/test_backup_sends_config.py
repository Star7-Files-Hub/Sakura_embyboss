# -*- coding: utf-8 -*-
"""
备份必须把数据库 .sql 和 config.json **一起发给 owner**（定时任务与手动 /backup_db
共用同一个函数，所以这里验证一次即可覆盖两者）。

做法：用 AST 从真实源文件里抽出 auto_backup_db，在受控命名空间里执行，
只桩掉 bot / DbBackupUtils / LOGGER —— 测的是真代码，不是副本。
"""
import ast
import asyncio
import os
import sys
import textwrap

REPO = os.environ.get("WARN_TEST_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS, FAIL = 0, 0
OWNER = 5608153118


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


def extract(path, names):
    """从文件里抽出指定函数的源码（含装饰器行）。"""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.splitlines()
    out = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                nm = getattr(sub, "name", None)
                if nm in names:
                    start = sub.lineno
                    if getattr(sub, "decorator_list", None):
                        start = min(d.lineno for d in sub.decorator_list)
                    out[nm] = textwrap.dedent("\n".join(lines[start - 1:sub.end_lineno]))
        else:
            nm = getattr(node, "name", None)
            if nm in names:
                start = node.lineno
                if getattr(node, "decorator_list", None):
                    start = min(d.lineno for d in node.decorator_list)
                out[nm] = textwrap.dedent("\n".join(lines[start - 1:node.end_lineno]))
    missing = set(names) - set(out)
    if missing:
        raise RuntimeError(f"{path} 里找不到: {missing}")
    return out


SRC = f"{REPO}/bot/scheduler/backup_db.py"
FUNC_SRC = extract(SRC, {"auto_backup_db"})["auto_backup_db"]


class FakeBot:
    def __init__(self, fail_on=()):
        self.calls = []
        self._fail_on = tuple(fail_on)

    async def send_document(self, **kw):
        self.calls.append(kw)
        doc = str(kw.get("document", ""))
        if any(doc.endswith(s) for s in self._fail_on):
            raise RuntimeError("send failed")


def make_ns(backup_file, fail_on=()):
    ns = {}
    exec(compile(FUNC_SRC, "backup_db.py", "exec"), ns)
    bot = FakeBot(fail_on)
    logs = []
    ns["bot"] = bot
    ns["owner"] = OWNER
    ns["LOGGER"] = type("L", (), {
        "info": staticmethod(lambda m, *a, **k: logs.append(("info", m))),
        "warning": staticmethod(lambda m, *a, **k: logs.append(("warning", m))),
        "error": staticmethod(lambda m, *a, **k: logs.append(("error", m))),
    })

    class FakeUtils:
        @staticmethod
        async def backup_db():
            return backup_file

    ns["DbBackupUtils"] = FakeUtils
    fn = ns["auto_backup_db"]
    # 源码里带 @staticmethod 装饰器，exec 后拿到的是 staticmethod 对象
    return getattr(fn, "__func__", fn), bot, logs


def run(fn, *a):
    return asyncio.new_event_loop().run_until_complete(fn(*a))


def at(calls, i):
    """安全取第 i 次调用。缺条目时返回空 dict —— 让 check() 记 FAIL 并继续，
    而不是抛 IndexError 把后面所有断言一起带崩（在旧镜像上实测踩到过）。"""
    return calls[i] if i < len(calls) else {}


print("════════ 1. 正常备份：.sql 与 config.json 一起发给 owner ════════")
SQL = "/app/db_backup/embyboss-2026-10-04-17-45-53.sql"
fn, bot, logs = make_ns(SQL)
run(fn)

check("send_document 被调用 2 次（数据库 + 配置）", len(bot.calls) == 2, str(len(bot.calls)))
check("两次都发往 owner 本人",
      all(c.get("chat_id") == OWNER for c in bot.calls), str([c.get("chat_id") for c in bot.calls]))
check("第一次发的是数据库 .sql",
      str(at(bot.calls, 0).get("document", "")).endswith(".sql"), str(at(bot.calls, 0)))
check("第二次发的是 config.json",
      str(at(bot.calls, 1).get("document", "")).endswith("config.json"), str(at(bot.calls, 1)))
check("配置备份的 caption 标明是 config",
      "config" in str(at(bot.calls, 1).get("caption", "")), str(at(bot.calls, 1).get("caption")))
check("两个文件都是静默发送（勿打扰）",
      all(c.get("disable_notification") is True for c in bot.calls), str(bot.calls))
check("备份成功时不记 error", not any(lvl == "error" for lvl, _ in logs), str(logs))


print()
print("════════ 2. 数据库备份失败时什么都不发 ════════")
fn, bot, logs = make_ns(None)
run(fn)
check("backup_db 返回 None 时不发送任何文件", bot.calls == [], str(bot.calls))
check("备份失败记 error", any(lvl == "error" for lvl, _ in logs), str(logs))


print()
print("════════ 3. 一个发送失败不能把另一个也拖掉 ════════")
# 上游原来用 asyncio.gather 并发发两个文件，任一失败就整体抛异常；
# 现在分开发送、各自捕获，数据库备份发不出去时配置备份仍然要发。
fn, bot, logs = make_ns(SQL, fail_on=(".sql",))
run(fn)
check("数据库发送失败后，config.json 仍然被发送",
      len(bot.calls) == 2 and str(at(bot.calls, 1).get("document", "")).endswith("config.json"),
      str(bot.calls))
check("发送失败被记录（不静默）", any("失败" in m for _, m in logs), str(logs))

fn, bot, logs = make_ns(SQL, fail_on=("config.json",))
run(fn)
check("config.json 发送失败不影响数据库备份已发出",
      len(bot.calls) == 2 and str(at(bot.calls, 0).get("document", "")).endswith(".sql"),
      str(bot.calls))
check("config 发送失败也被记录", any("config.json" in m for _, m in logs), str(logs))


print()
print("════════ 4. 定时任务与手动命令共用同一个函数 ════════")
src = open(SRC, encoding="utf-8").read()
sched = open(f"{REPO}/bot/modules/panel/sched_panel.py", encoding="utf-8").read()
check("手动 /backup_db 调用 auto_backup_db",
      "async def manual_backup_db" in sched and "auto_backup_db()" in sched)
check("定时任务 action_dict 里 backup_db 指向 auto_backup_db",
      '"backup_db": auto_backup_db' in sched)
check("定时任务被注册到调度器", '"backup_db": {' in sched and "'id': 'backup_db'" in sched)
check("不再有「跳过 config.json 外发」的逻辑（用户要求发回 owner）",
      "已跳过 config.json 外发" not in src)
check("源码里保留密钥外发风险的提醒",
      "请勿转发" in src or "勿拉入群组" in src)


print()
print("════════════════════════════════════════")
print(f"  结果：PASS={PASS}  FAIL={FAIL}")
print("════════════════════════════════════════")
sys.exit(1 if FAIL else 0)
