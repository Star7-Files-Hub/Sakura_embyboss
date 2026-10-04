# -*- coding: utf-8 -*-
"""
备份收件人硬绑定 owner 私聊（task-34）。

用户要求：config.json 以及数据库备份**只能**发给 owner，哪怕把 bot 拉进群、
或 owner 本人在群里发 /backup_db，也绝不能发到群里。

本测试验证的安全不变量：
  I1. 收件人只来自 config 的 owner，且必须是私聊（正数）。
  I2. owner 是群组/频道 id（负数）、0、None、非数字字符串、布尔、非整数浮点、
      容器等非法值时 —— **一次都不发送**，只记 error（备份文件仍留在本地 db_backup 目录）。
  I3. 源码里不存在任何「用消息上下文（msg.chat.id / call.message.chat.id 之类）
      作为备份收件人」的写法。
  I4. 既有行为不变：.sql 与 config.json 分开发送、各自捕获异常，一个失败不拖累另一个。

做法与 tests/test_backup_sends_config.py 一致：用 AST 从真实源文件抽出
auto_backup_db 与 _backup_chat_id，在受控命名空间里执行，只桩掉
bot / DbBackupUtils / LOGGER —— 测的是真代码，不是副本。
"""
import ast
import asyncio
import os
import sys
import textwrap

REPO = os.environ.get("WARN_TEST_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = f"{REPO}/bot/scheduler/backup_db.py"
PASS, FAIL = 0, 0

OWNER = 5608153118                      # 正常：owner 私聊（正数）
GROUP = -1001234567890                  # 群组/频道 id（Telegram 里一律为负）
SQL = "/app/db_backup/embyboss-2026-10-04-17-45-53.sql"


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


def extract(path, names):
    """从文件里抽出指定函数/方法的源码（含装饰器行）。"""
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


FUNCS = extract(SRC, {"auto_backup_db", "_backup_chat_id"})
SRC_TEXT = open(SRC, encoding="utf-8").read()


class FakeBot:
    def __init__(self, fail_on=()):
        self.calls = []
        self._fail_on = tuple(fail_on)

    async def send_document(self, **kw):
        self.calls.append(kw)
        doc = str(kw.get("document", ""))
        if any(doc.endswith(s) for s in self._fail_on):
            raise RuntimeError("send failed")


def make_ns(backup_file, owner_value=OWNER, fail_on=(), with_validator=True):
    """搭一个受控命名空间。

    with_validator=True  → 与生产一致：模块级 _backup_chat_id() 在命名空间里。
    with_validator=False → 模拟 tests/test_backup_sends_config.py 的「只抽出
                           auto_backup_db 单函数执行」环境，验证兜底校验是
                           fail-closed 的（只会更保守，绝不会多发）。
    """
    ns = {}
    if with_validator:
        exec(compile(FUNCS["_backup_chat_id"], "backup_db.py", "exec"), ns)
    exec(compile(FUNCS["auto_backup_db"], "backup_db.py", "exec"), ns)

    bot = FakeBot(fail_on)
    logs = []
    ns["bot"] = bot
    ns["owner"] = owner_value
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
    而不是抛 IndexError 把后面所有断言一起带崩。"""
    return calls[i] if i < len(calls) else {}


print("════════ 1. owner 为正数：两个文件都发到该私聊 id ════════")
fn, bot, logs = make_ns(SQL, OWNER)
run(fn)
print(f"  send_document 调用: {[c.get('chat_id') for c in bot.calls]}")
check("send_document 被调用 2 次（数据库 + 配置）", len(bot.calls) == 2, str(len(bot.calls)))
check("两次的 chat_id 都恰等于 config 里的 owner",
      all(c.get("chat_id") == OWNER for c in bot.calls), str([c.get("chat_id") for c in bot.calls]))
check("chat_id 是 int 类型（不是字符串/其它对象）",
      all(isinstance(c.get("chat_id"), int) and not isinstance(c.get("chat_id"), bool)
          for c in bot.calls), str([type(c.get("chat_id")) for c in bot.calls]))
check("第一次发的是数据库 .sql",
      str(at(bot.calls, 0).get("document", "")).endswith(".sql"), str(at(bot.calls, 0)))
check("第二次发的是 config.json",
      str(at(bot.calls, 1).get("document", "")).endswith("config.json"), str(at(bot.calls, 1)))
check("两个文件都是静默发送（勿打扰）",
      all(c.get("disable_notification") is True for c in bot.calls), str(bot.calls))
check("正常发送时不记 error", not any(lvl == "error" for lvl, _ in logs), str(logs))


print()
print("════════ 2. owner 为群组 id（负数）：一次都不发送 ════════")
fn, bot, logs = make_ns(SQL, GROUP)
run(fn)
print(f"  owner = {GROUP}（群组/频道 id）")
print(f"  send_document 调用次数 = {len(bot.calls)}   （期望 0）")
for lvl, m in logs:
    print(f"  LOGGER.{lvl}: {m}")
check("一次都不发送（send_document 调用数为 0）", bot.calls == [], str(bot.calls))
check("记了 error", any(lvl == "error" for lvl, _ in logs), str(logs))
check("error 文案点明是群组/频道",
      any("群组" in m or "频道" in m for _, m in logs), str(logs))
check("error 文案点明「绝不能发到群里」", any("绝不能发到群里" in m for _, m in logs), str(logs))
check("error 文案点明「已拒绝发送」", any("已拒绝发送" in m for _, m in logs), str(logs))
check("error 文案给出修法（把 owner 改成用户 id / 正数）",
      any("用户 id" in m or "正数" in m for _, m in logs), str(logs))


print()
print("════════ 3. owner 为 0 / None / 非数字 / 布尔 / 非整数浮点 / 容器：一次都不发送 ════════")
for bad in (0, None, "abc", "", "  ", 3.5, True, False, -0.5, [OWNER], {"a": 1}, "1e3", "0x10"):
    fn, bot, logs = make_ns(SQL, bad)
    run(fn)
    has_err = any(lvl == "error" for lvl, _ in logs)
    print(f"  owner={bad!r:12} → 发送 {len(bot.calls)} 次，error={has_err}")
    check(f"owner={bad!r} 时一次都不发送", bot.calls == [], str(bot.calls))
    check(f"owner={bad!r} 时记了 error", has_err, str(logs))


print()
print("════════ 3b. 合法写法被正确归一化（数字串 / 整数值浮点）════════")
# int(3.5)==3 这种静默截断会把密钥发给**另一个用户 id**，所以必须区分：
# 整数值的浮点与十进制数字串可以接受并归一化成 int，非整数浮点一律拒绝。
for good, expect_id in (("5608153118", OWNER), (f"  {OWNER}  ", OWNER), (5608153118.0, OWNER)):
    fn, bot, logs = make_ns(SQL, good)
    run(fn)
    ids = [c.get("chat_id") for c in bot.calls]
    print(f"  owner={good!r:16} → 发送 {len(bot.calls)} 次，chat_id={ids}")
    check(f"owner={good!r} 发送 2 次", len(bot.calls) == 2, str(bot.calls))
    check(f"owner={good!r} 归一化为 int {expect_id}",
          all(i == expect_id and isinstance(i, int) and not isinstance(i, bool) for i in ids), str(ids))


print()
print("════════ 3c. 字符串分支必须只认 ASCII 数字（否则会把密钥发给另一个用户 id）════════")
# 这一段是补的：复核用变异测试证明，把生产代码里的
#     int(s) if (s.isascii() and s.isdigit()) else None
# 弱化回朴素的 try: int(s) except ValueError: None
# 之后，本套件与 test_backup_sends_config 仍然全绿 —— 也就是说这条安全校验
# 当时**一行覆盖都没有**。而朴素 int() 会接受下面这些输入：
#   int("１２３") == 123   全角数字（中文输入法手改 config.json 极易误打）
#   int("١٢٣")  == 123   阿拉伯-印度数字
#   int("1_000") == 1000  Python 数字分隔符
# 任何一条被接受，都会把**含全部密钥的数据库备份 + config.json 静默发给
# 另一个真实用户 id**。所以这里逐条断言：一律拒发、且必须记 error。
for bad in ("１２３", "١٢٣", "1_000", "+123", "5_608_153_118", "0x10", "1e3",
            str(GROUP), "-1", "abc", "", "  ", "12.3", "１２３４５６７８９０"):
    fn, bot, logs = make_ns(SQL, bad)
    run(fn)
    has_err = any(lvl == "error" for lvl, _ in logs)
    print(f"  owner={bad!r:16} → 发送 {len(bot.calls)} 次，error={has_err}")
    check(f"owner={bad!r} 时一次都不发送", bot.calls == [], str(bot.calls))
    check(f"owner={bad!r} 时记了 error", has_err, str(logs))

# 反过来，纯 ASCII 数字串（含首尾空白）必须仍然放行 —— 否则上面可能靠
# 「一律拒绝」蒙混过关，把正常配置也堵死。
for good in ("5608153118", " 5608153118 ", "\t5608153118\n", "0" + str(OWNER)):
    fn, bot, logs = make_ns(SQL, good)
    run(fn)
    ids = [c.get("chat_id") for c in bot.calls]
    expect = int(good.strip())
    print(f"  owner={good!r:20} → 发送 {len(bot.calls)} 次，chat_id={ids}")
    check(f"owner={good!r} 发送 2 次", len(bot.calls) == 2, str(bot.calls))
    check(f"owner={good!r} 归一化为 {expect}",
          all(i == expect for i in ids), str(ids))


print()
print("════════ 4. 源码不变量：收件人只来自 owner，绝不用消息上下文 ════════")
tree = ast.parse(SRC_TEXT)
fn_node = next(n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "auto_backup_db")
val_node = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_backup_chat_id")


def send_calls(node):
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "send_document"]


def chat_id_kwargs(call):
    return [k for k in call.keywords if k.arg == "chat_id"]


def msg_context_hits(node):
    """消息上下文写法：xxx.chat.id 或直接引用 msg/message/call/update 对象。"""
    hits = []
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr == "id" \
                and isinstance(n.value, ast.Attribute) and n.value.attr == "chat":
            hits.append(f"第 {n.lineno} 行 {ast.unparse(n)}")
        if isinstance(n, ast.Name) and n.id in ("msg", "message", "call", "update", "event"):
            hits.append(f"第 {n.lineno} 行 名字 {n.id}")
    return hits


file_sends = send_calls(tree)
check("整个 backup_db.py 里只有 2 处 send_document（未新增其它发送目标）",
      len(file_sends) == 2, f"{len(file_sends)} 处")

fn_sends = send_calls(fn_node)
check("auto_backup_db 内 2 处 send_document", len(fn_sends) == 2, f"{len(fn_sends)} 处")
check("每处 send_document 的 chat_id 都是同一个校验后的变量 chat_id",
      all(len(chat_id_kwargs(c)) == 1 and isinstance(chat_id_kwargs(c)[0].value, ast.Name)
          and chat_id_kwargs(c)[0].value.id == "chat_id" for c in fn_sends),
      str([ast.unparse(chat_id_kwargs(c)[0].value) if chat_id_kwargs(c) else None for c in fn_sends]))
check("源码里不存在 chat_id=owner 直接下发（必须经过校验）",
      "chat_id=owner" not in FUNCS["auto_backup_db"], "")

assigns = [n for n in ast.walk(fn_node) if isinstance(n, ast.Assign)
           and any(isinstance(t, ast.Name) and t.id == "chat_id" for t in n.targets)]
assign_srcs = [ast.unparse(n.value) for n in assigns]
check("chat_id 确实由模块级 _backup_chat_id() 的结果赋值",
      any("_backup_chat_id()" in s for s in assign_srcs), str(assign_srcs))
check("直接调用 _backup_chat_id()，生产代码里不存在 globals().get 兜底分支",
      "_backup_chat_id()" in FUNCS["auto_backup_db"]
      and "globals().get" not in FUNCS["auto_backup_db"], "")
check("chat_id 的所有赋值来源都只有「校验函数 / None」，没有第三方来源",
      all(("_backup_chat_id()" in s) or s == "None" for s in assign_srcs), str(assign_srcs))
check("auto_backup_db 里没有任何消息上下文写法（.chat.id / msg / call / message）",
      not msg_context_hits(fn_node), str(msg_context_hits(fn_node)))
check("_backup_chat_id 里也没有任何消息上下文写法",
      not msg_context_hits(val_node), str(msg_context_hits(val_node)))
check("_backup_chat_id 只从 owner 取收件人",
      "owner" in ast.unparse(val_node) and not msg_context_hits(val_node), "")
check("_backup_chat_id 有 docstring 写明设计意图（含「消息上下文」不变量）",
      bool(ast.get_docstring(val_node)) and "消息上下文" in ast.get_docstring(val_node), "")


print()
print("════════ 5. 既有行为不变：一个发送失败不拖累另一个 ════════")
fn, bot, logs = make_ns(SQL, OWNER, fail_on=(".sql",))
run(fn)
check("数据库发送失败后，config.json 仍然被发送",
      len(bot.calls) == 2 and str(at(bot.calls, 1).get("document", "")).endswith("config.json"),
      str(bot.calls))
check("发送失败被记录（不静默）", any("失败" in m for _, m in logs), str(logs))

fn, bot, logs = make_ns(SQL, OWNER, fail_on=("config.json",))
run(fn)
check("config.json 发送失败不影响数据库备份已发出",
      len(bot.calls) == 2 and str(at(bot.calls, 0).get("document", "")).endswith(".sql"),
      str(bot.calls))
check("config 发送失败也被记录", any("config.json" in m for _, m in logs), str(logs))

fn, bot, logs = make_ns(None, OWNER)
run(fn)
check("backup_db 返回 None 时什么都不发", bot.calls == [], str(bot.calls))
check("backup_db 返回 None 时记 error", any(lvl == "error" for lvl, _ in logs), str(logs))


print()
print("════════ 6. 校验函数缺失时必须「响亮地失败」，不得静默降级 ════════")
# auto_backup_db 里刻意**没有**「取不到校验函数就退化成宽松判断」的兜底分支。
# 那种兜底会让 _backup_chat_id 被改名/误删时静默降级成较弱的检查，而不是当场炸出来；
# 对一个「绝不能发错人」的安全校验来说，静默降级比直接失败危险得多。
# 所以这里断言：隔离命名空间里缺 _backup_chat_id 时，调用必须抛 NameError（而不是照发）。
# （配套改动在 tests/test_backup_sends_config.py：它现在同时抽出 _backup_chat_id，
#   所以正常测试环境里两个函数都在，不会走到这里。）
fn, bot, logs = make_ns(SQL, OWNER, with_validator=False)
raised = None
try:
    run(fn)
except NameError as e:
    raised = e
check("缺少 _backup_chat_id 时抛 NameError（fail loud，不静默降级）",
      isinstance(raised, NameError), repr(raised))
check("抛异常时一次都没发送", bot.calls == [], str(bot.calls))


print()
print("════════════════════════════════════════")
print(f"  结果：PASS={PASS}  FAIL={FAIL}")
print("════════════════════════════════════════")
sys.exit(1 if FAIL else 0)
