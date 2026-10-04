# -*- coding: utf-8 -*-
"""
单用户「警告 -1 / 重置」功能测试。

做法：从真实源文件里用 AST 抽出函数体，在受控命名空间里执行，
      只桩掉外部依赖（DB / Emby / pyrogram / ikb）。
      这样测的是真代码，不是复制的副本。
"""
import ast
import asyncio
import os
import re
import sys

REPO = os.environ.get("WARN_TEST_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS, FAIL = 0, 0


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
        nm = getattr(node, "name", None)
        if nm in names:
            start = node.lineno
            # FunctionDef.lineno 指向 def 行，装饰器在 decorator_list 里，
            # 必须单独取最小行号，否则 @bot.on_callback_query(...) 会被丢掉，
            # 回调就不会注册。
            if getattr(node, "decorator_list", None):
                start = min(d.lineno for d in node.decorator_list)
            out[nm] = "\n".join(lines[start - 1:node.end_lineno])
    missing = set(names) - set(out)
    if missing:
        raise RuntimeError(f"{path} 里找不到: {missing}")
    return out


def load_constants(path, names):
    """抽出模块级常量赋值（如 _REFRESH_EDITED = "edited"），避免测试里硬编码副本。"""
    tree = ast.parse(open(path, encoding="utf-8").read())
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in names:
                    out[t.id] = ast.literal_eval(node.value)
    missing = set(names) - set(out)
    if missing:
        raise RuntimeError(f"{path} 里找不到常量: {missing}")
    return out


KK_CONSTS = load_constants(f"{REPO}/bot/modules/panel/kk.py",
                           {"_REFRESH_EDITED", "_REFRESH_UNCHANGED", "_REFRESH_FAILED",
                            "_FLOODWAIT_MAX_WAIT"})


# ───────────────────────── 桩 ─────────────────────────

def array_chunk(lst, n):
    return [lst[i:i + n] for i in range(0, len(lst), n)]


def ikb(rows):
    """真实 pyromod.ikb 的语义近似：返回可直接断言的二维结构。"""
    return rows


class FakeConfig:
    concurrent_play_warn_threshold = 3


class FakeEmbyRow:
    def __init__(self, tg, warn):
        self.tg = tg
        self.concurrent_warn_count = warn


class BadRequest(Exception):
    """模拟 pyrogram.errors.BadRequest：带 .ID 属性。"""
    def __init__(self, msg="bad request", ID="UNKNOWN"):
        super().__init__(msg)
        self.ID = ID


class FloodWait(Exception):
    """模拟 pyrogram.errors.FloodWait：带 .value 秒数。注意它不是 BadRequest 子类。"""
    def __init__(self, seconds=1):
        super().__init__(f"flood wait {seconds}s")
        self.value = seconds


_sleeps = []


async def _fake_sleep(seconds):
    _sleeps.append(seconds)


class FakeMessage:
    def __init__(self, sink, fail_with=None, delete_fail_with=None):
        self._sink = sink
        self._fail_with = fail_with
        self._delete_fail_with = delete_fail_with
        self.chat = type("Chat", (), {"id": -1001234567890})()
        self.id = 4242
        self.deleted = False

    async def edit(self, text=None, disable_web_page_preview=None, reply_markup=None, **kw):
        if self._fail_with is not None:
            raise self._fail_with
        self._sink.append(("EDIT", text))
        return self

    async def delete(self):
        if self._delete_fail_with is not None:
            raise self._delete_fail_with
        self.deleted = True
        self._sink.append(("DELETE", None))


class FakeCall:
    def __init__(self, data, uid=5608153118, edit_fail_with=None, delete_fail_with=None):
        self.data = data
        self.from_user = type("U", (), {"id": uid, "first_name": "测试管理员"})()
        self.answers = []
        self.edits = []
        self.message = FakeMessage(self.edits, edit_fail_with, delete_fail_with)

    async def answer(self, text, show_alert=False):
        self.answers.append((text, show_alert))


def make_kk_ns(db, edits, is_admin=True):
    """构造 kk.py 中警告相关函数的执行命名空间。"""
    ns = {}

    # bot / filters 必须在 exec 之前就位：函数源码带 @bot.on_callback_query 装饰器，
    # 装饰器在 exec 时立刻求值。
    registry = {}

    def on_callback_query(pattern):
        def deco(fn):
            registry[pattern] = fn
            return fn
        return deco

    async def _get_chat(uid):
        return type("C", (), {"first_name": "测试用户"})()

    panels_sent = []

    async def _delete_messages(chat_id, message_ids, **kw):
        return True

    async def _send_message(**kw):
        panels_sent.append(kw)
        return type("M", (), {"id": 9000 + len(panels_sent)})()

    ns["bot"] = type("B", (), {"on_callback_query": staticmethod(on_callback_query),
                               "get_chat": staticmethod(_get_chat),
                               "delete_messages": staticmethod(_delete_messages),
                               "send_message": staticmethod(_send_message)})
    ns["filters"] = type("F", (), {"regex": staticmethod(lambda p: p)})

    for fn in extract(f"{REPO}/bot/modules/panel/kk.py",
                      {"_refresh_kk_panel", "_load_warn_state", "_apply_warn_change",
                       "kk_warn_minus", "kk_warn_reset",
                       "_kk_send_with_floodwait", "_send_kk_panel"}).values():
        exec(compile(fn, "kk.py", "exec"), ns)

    ns["_kk_panel_ids"] = {}
    ns["FloodWait"] = FloodWait
    ns["sleep"] = _fake_sleep
    ns["BadRequest"] = BadRequest
    ns.update(KK_CONSTS)
    ns["config"] = FakeConfig
    ns["LOGGER"] = type("L", (), {
        "info": staticmethod(lambda *a, **k: None),
        "error": staticmethod(lambda *a, **k: None),
        "warning": staticmethod(lambda *a, **k: None),
    })
    ns["judge_admins"] = lambda uid: is_admin

    def sql_get_emby(uid):
        return db.get(uid)

    def sql_update_emby(cond, **kwargs):
        # 还原 sql_update_emby 的真实语义：只更新匹配到的那一行
        uid = getattr(cond, "right", None)
        row = db.get(uid)
        if row is None:
            return False
        for k, v in kwargs.items():
            setattr(row, k, v)
        return True

    class _Col:
        def __init__(self, name):
            self.name = name

        def __eq__(self, other):
            return type("Cond", (), {"right": other, "col": self.name})()

    class _Emby:
        tg = _Col("tg")

    ns["sql_get_emby"] = sql_get_emby
    ns["sql_update_emby"] = sql_update_emby
    ns["Emby"] = _Emby

    async def fake_edit(call, text, buttons=None, **kw):
        edits.append((text, buttons))

    async def fake_cr_kk_ikb(uid, first, warn_count=None):
        edits.append(("RENDER", warn_count))
        return "面板文本", [["行1"], ["行2"]]

    ns["editMessage"] = fake_edit
    ns["cr_kk_ikb"] = fake_cr_kk_ikb
    return ns, registry


def _raise_async(exc):
    async def _c():
        raise exc
    return _c()


def _async(v):
    async def _c():
        return v() if callable(v) else v
    return _c()


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ───────────────────────── 测试 ─────────────────────────

print("════════ 1. _warn_count_of 的健壮性 ════════")
fb = extract(f"{REPO}/bot/func_helper/fix_bottons.py", {"_warn_count_of"})
ns1 = dict(fb)
ns1["LOGGER"] = type("L", (), {"warning": staticmethod(lambda *a, **k: None),
                               "error": staticmethod(lambda *a, **k: None)})


def _load_warn_count_of(getter):
    mod = type(sys)("bot.sql_helper.sql_emby")
    mod.sql_get_emby = getter
    sys.modules["bot.sql_helper.sql_emby"] = mod
    sys.modules.setdefault("bot", type(sys)("bot"))
    sys.modules.setdefault("bot.sql_helper", type(sys)("bot.sql_helper"))
    exec(compile(fb["_warn_count_of"], "fb", "exec"), ns1)
    return ns1["_warn_count_of"]


check("记录存在且计数为 5 -> 5", _load_warn_count_of(lambda u: FakeEmbyRow(u, 5))(1) == 5)
check("计数为 NULL -> 0（这是真实的 0，不是读失败）",
      _load_warn_count_of(lambda u: FakeEmbyRow(u, None))(1) == 0)
check("记录不存在 -> None（读不到，必须与真实 0 区分）",
      _load_warn_count_of(lambda u: None)(1) is None)


def _boom(u):
    raise RuntimeError("db down")


check("DB 抛异常 -> None（读不到就显示 ?，绝不能假装是 0）",
      _load_warn_count_of(_boom)(1) is None)


print()
print("════════ 2. /kk 面板布局：警告行与按钮 ════════")
ns2 = {}
fb2 = extract(f"{REPO}/bot/func_helper/fix_bottons.py", {"_warn_count_of", "cr_kk_ikb"})
ns2["array_chunk"] = array_chunk
ns2["ikb"] = ikb
ns2["config"] = FakeConfig
ns2["sakura_b"] = " Sakura"
ns2["LOGGER"] = type("L", (), {"warning": staticmethod(lambda *a, **k: None),
                               "error": staticmethod(lambda *a, **k: None)})
ns2["_warn_count_of"] = lambda uid: 2
exec(compile(fb2["cr_kk_ikb"], "fb", "exec"), ns2)


class FakeEmbyClient:
    def __init__(self, folders=None):
        self.folders = folders or []

    async def user(self, emby_id=None):
        return True, {"Policy": {"EnabledFolders": self.folders, "EnableAllFolders": False}}

    async def get_folder_ids_by_names(self, names):
        return ["fid1"]

    async def emby_cust_commit(self, emby_id=None, days=30):
        return [["2026-10-04 12:00:00.123", 120]]


def render_panel(warn=2, libs=None, account=True):
    ns2["_warn_count_of"] = lambda uid: warn
    ns2["extra_emby_libs"] = libs or []
    ns2["emby"] = FakeEmbyClient(folders=libs and ["fid1"] or [])

    async def mi(uid):
        if not account:
            return ("无账户信息", "未注册", "无账户信息", 0, None, None)
        return ("测试账户", "**正常**", "2026-12-31", 100, "embyid123", "pw")
    ns2["members_info"] = mi
    return run(ns2["cr_kk_ikb"](5608153118, "测试用户"))


text, kb = render_panel()
flat = [b for row in kb for b in row]
labels = [b[0] for b in flat]
datas = [b[1] for b in flat]

check("文本含「并发警告」行", "并发警告" in text, repr(text[-120:]))
check("文本含当前计数 2 与阈值 3", "**2** / 3 次" in text, repr(text))
check("有「➖ 警告-1」按钮", any("警告-1" in l for l in labels), str(labels))
check("有「🔄 重置警告」按钮", any("重置警告" in l for l in labels), str(labels))
check("回调数据 warn_minus-<tgid>", f"warn_minus-5608153118" in datas, str(datas))
check("回调数据 warn_reset-<tgid>", f"warn_reset-5608153118" in datas, str(datas))

# 警告按钮必须自己占一行（不受「额外媒体库」是否存在影响）
warn_rows = [i for i, row in enumerate(kb) if any("warn_minus-" in b[1] for b in row)]
check("警告按钮独占一行", len(warn_rows) == 1, f"出现在行 {warn_rows}")
if warn_rows:
    row = kb[warn_rows[0]]
    check("该行同时含 -1 与 重置 两个按钮",
          len(row) == 2 and "warn_reset-" in row[1][1], str(row))
check("最后一行是「踢出并封禁 / 删除消息」",
      "fuckoff-" in kb[-1][0][1] and kb[-1][1][1] == "closeit", str(kb[-1]))

text_l, kb_l = render_panel(libs=["额外库A"])
warn_rows_l = [i for i, row in enumerate(kb_l) if any("warn_minus-" in b[1] for b in row)]
check("有额外媒体库时，警告按钮仍独占一行", len(warn_rows_l) == 1, f"出现在行 {warn_rows_l}")
check("有额外媒体库时，最后一行仍是踢出/删除消息",
      "fuckoff-" in kb_l[-1][0][1], str(kb_l[-1]))

text_n, kb_n = render_panel(account=False)
flat_n = [b for row in kb_n for b in row]
check("无 Emby 账户时不显示警告计数行", "并发警告" not in text_n, repr(text_n[-80:]))
check("无 Emby 账户时不显示警告按钮",
      not any("warn_" in b[1] for b in flat_n), str([b[1] for b in flat_n]))
check("无 Emby 账户时仍有赠送资格按钮",
      any("gift-" in b[1] for b in flat_n), str([b[1] for b in flat_n]))


print()
print("════════ 3. 「警告 -1」回调 ════════")
db = {5608153118: FakeEmbyRow(5608153118, 2)}
edits = []
ns3, reg = make_kk_ns(db, edits)
fn = reg["^warn_minus-"]

call = FakeCall("warn_minus-5608153118")
run(fn(None, call))
check("计数 2 -> 1", db[5608153118].concurrent_warn_count == 1, str(db[5608153118].concurrent_warn_count))
check("提示写明 2 → 1", any("2 → 1" in a[0] for a in call.answers), str(call.answers))
check("操作后重渲染了面板", len(edits) == 1, f"edits={len(edits)}")

call2 = FakeCall("warn_minus-5608153118")
run(fn(None, call2))
check("再点一次 1 -> 0", db[5608153118].concurrent_warn_count == 0)

# 已经是 0：不能变成负数，也不能写库
call3 = FakeCall("warn_minus-5608153118")
before = db[5608153118].concurrent_warn_count
run(fn(None, call3))
check("计数为 0 时再减不会变负", db[5608153118].concurrent_warn_count == 0)
check("计数为 0 时给出提示", any("已经是 0" in a[0] for a in call3.answers), str(call3.answers))

# 用户不存在
db2 = {}
ns4, reg4 = make_kk_ns(db2, [])
call4 = FakeCall("warn_minus-999")
run(reg4["^warn_minus-"](None, call4))
check("用户不存在时提示未注册", any("没有注册账户" in a[0] for a in call4.answers), str(call4.answers))

# 非管理员
_db_guard = {1: FakeEmbyRow(1, 2)}
ns5, reg5 = make_kk_ns(_db_guard, [], is_admin=False)
call5 = FakeCall("warn_minus-1", uid=111)
run(reg5["^warn_minus-"](None, call5))
check("非管理员被拒绝", any("以下犯上" in a[0] for a in call5.answers), str(call5.answers))
check("非管理员点击后计数未变", _db_guard[1].concurrent_warn_count == 2,
      str(_db_guard[1].concurrent_warn_count))

# 坏数据
ns6, reg6 = make_kk_ns({}, [])
call6 = FakeCall("warn_minus-abc")
run(reg6["^warn_minus-"](None, call6))
check("uid 非数字时给出格式错误", any("格式错误" in a[0] for a in call6.answers), str(call6.answers))


print()
print("════════ 4. 「重置警告」回调 ════════")
db7 = {5608153118: FakeEmbyRow(5608153118, 3)}
ns7, reg7 = make_kk_ns(db7, [])
call7 = FakeCall("warn_reset-5608153118")
run(reg7["^warn_reset-"](None, call7))
check("计数 3 -> 0", db7[5608153118].concurrent_warn_count == 0)
check("提示写明原值 3", any("原 3" in a[0] for a in call7.answers), str(call7.answers))

call8 = FakeCall("warn_reset-5608153118")
run(reg7["^warn_reset-"](None, call8))
check("已是 0 时提示无需重置", any("已经是 0" in a[0] for a in call8.answers), str(call8.answers))

ns8, reg8 = make_kk_ns({}, [])
call9 = FakeCall("warn_reset-999")
run(reg8["^warn_reset-"](None, call9))
check("重置不存在的用户 -> 提示未注册", any("没有注册账户" in a[0] for a in call9.answers))

ns9, reg9 = make_kk_ns({5608153118: FakeEmbyRow(5608153118, 3)}, [], is_admin=False)
call10 = FakeCall("warn_reset-5608153118", uid=111)
run(reg9["^warn_reset-"](None, call10))
check("非管理员不能重置", any("以下犯上" in a[0] for a in call10.answers), str(call10.answers))


print()
print("════════ 4b. 刷新面板必须用「刚写入的值」，不能二次读库 ════════")
# 模拟：库里还是旧值 1，但我们已经把它改成了 0 —— 渲染必须显示 0
ns_r = {}
fb_r = extract(f"{REPO}/bot/func_helper/fix_bottons.py", {"_warn_count_of", "cr_kk_ikb"})
ns_r["array_chunk"] = array_chunk
ns_r["ikb"] = ikb
ns_r["config"] = FakeConfig
ns_r["sakura_b"] = " Sakura"
ns_r["LOGGER"] = type("L", (), {"warning": staticmethod(lambda *a, **k: None),
                               "error": staticmethod(lambda *a, **k: None)})
ns_r["_warn_count_of"] = lambda uid: 1          # 数据库里的旧值
exec(compile(fb_r["cr_kk_ikb"], "fb", "exec"), ns_r)
ns_r["extra_emby_libs"] = []
ns_r["emby"] = FakeEmbyClient()

async def _mi(uid):
    return ("测试账户", "**正常**", "2026-12-31", 100, "embyid123", "pw")
ns_r["members_info"] = _mi

t_auto, _ = run(ns_r["cr_kk_ikb"](1, "u"))          # 不传 -> 读库得到 1
t_expl, _ = run(ns_r["cr_kk_ikb"](1, "u", 0))       # 显式传 0
check("不传时读库得到 1", "**1** / 3 次" in t_auto, repr(t_auto[-60:]))
check("显式传 0 时渲染 0（不理会库里的 1）", "**0** / 3 次" in t_expl, repr(t_expl[-60:]))
check("两者文本确实不同（否则 Telegram 会 MESSAGE_NOT_MODIFIED）", t_auto != t_expl)

# 回调链路：_apply_warn_change 必须把新值传下去
db_r = {7: FakeEmbyRow(7, 2)}
edits_r = []
ns_r2, reg_r2 = make_kk_ns(db_r, edits_r)
run(reg_r2["^warn_minus-"](None, FakeCall("warn_minus-7")))
renders = [e[1] for e in edits_r if isinstance(e, tuple) and e and e[0] == "RENDER"]
check("刷新时传入的是新值 1 而不是读库结果", renders == [1], f"实际传入 {renders}")

db_r2 = {8: FakeEmbyRow(8, 3)}
edits_r2 = []
ns_r3, reg_r3 = make_kk_ns(db_r2, edits_r2)
run(reg_r3["^warn_reset-"](None, FakeCall("warn_reset-8")))
renders2 = [e[1] for e in edits_r2 if isinstance(e, tuple) and e and e[0] == "RENDER"]
check("重置时传入 0", renders2 == [0], f"实际传入 {renders2}")

# 已是 0 的 no-op 分支也要刷新面板
db_r3 = {9: FakeEmbyRow(9, 0)}
edits_r3 = []
ns_r4, reg_r4 = make_kk_ns(db_r3, edits_r3)
run(reg_r4["^warn_minus-"](None, FakeCall("warn_minus-9")))
renders3 = [e[1] for e in edits_r3 if isinstance(e, tuple) and e and e[0] == "RENDER"]
check("计数已是 0 时也刷新面板（传 0）", renders3 == [0], f"实际传入 {renders3}")

print()
print("════════ 5. 只改警告数，不碰其他字段 ════════")
row = FakeEmbyRow(5608153118, 2)
row.lv = "b"
row.embyid = "abc"
row.ex = "2026-12-31"
db10 = {5608153118: row}
ns10, reg10 = make_kk_ns(db10, [])
run(reg10["^warn_minus-"](None, FakeCall("warn_minus-5608153118")))
check("警告数已改", row.concurrent_warn_count == 1)
check("lv 未被改动", row.lv == "b")
check("embyid 未被改动", row.embyid == "abc")
check("ex 未被改动", row.ex == "2026-12-31")


print()
print("════════ 4c. 编辑失败的处理 ════════")
logs = []
sent = []
ns_f = {}
for fn in extract(f"{REPO}/bot/modules/panel/kk.py",
                  {"_refresh_kk_panel", "_kk_send_with_floodwait", "_send_kk_panel"}).values():
    exec(compile(fn, "kk.py", "exec"), ns_f)
ns_f["BadRequest"] = BadRequest
ns_f["FloodWait"] = FloodWait
ns_f["sleep"] = _fake_sleep
ns_f["_kk_panel_ids"] = {}
ns_f.update(KK_CONSTS)
ns_f["LOGGER"] = type("L", (), {"info": staticmethod(lambda m, *a, **k: logs.append(("info", m))),
                                "warning": staticmethod(lambda m, *a, **k: logs.append(("warning", m))),
                                "error": staticmethod(lambda m, *a, **k: logs.append(("error", m)))})
ns_f["cr_kk_ikb"] = lambda uid, first, wc=None: _async(lambda: ("t", [["r"]]))


def _bot_with(send_factory):
    """send_factory(**kw) 返回一个协程；包一层，让它像真 bot 一样返回带 .id 的消息。"""
    def _send(**kw):
        async def _c():
            await send_factory(**kw)
            return type("M", (), {"id": 9999})()
        return _c()

    return type("B", (), {
        "get_chat": staticmethod(lambda uid: _async(lambda: type("C", (), {"first_name": "u"})())),
        "send_message": staticmethod(_send),
        "delete_messages": staticmethod(lambda chat_id, message_ids, **kw: _async(True)),
    })


ns_f["bot"] = _bot_with(lambda **kw: _async(lambda: sent.append(kw)))

# 正常编辑：记 info、返回 True、不走兜底
call_ok = FakeCall("x")
r2 = run(ns_f["_refresh_kk_panel"](call_ok, 1, 2))
check("正常编辑返回 EDITED", r2 == "edited")
check("正常编辑记 info 日志", any("面板已刷新" in m for _, m in logs), str(logs))
check("编辑用的文本来自 cr_kk_ikb", call_ok.edits == [("EDIT", "t")], str(call_ok.edits))
check("正常编辑不改发新面板", len(sent) == 0, str(sent))
check("info 日志里带上了警告数（便于定位）", any("警告数=2" in m for _, m in logs), str(logs))

# MESSAGE_NOT_MODIFIED：内容本来就一致，属于正常情况，不该改发新面板
logs.clear(); sent.clear()
call_same = FakeCall("x", edit_fail_with=BadRequest("message is not modified", "MESSAGE_NOT_MODIFIED"))
r4 = run(ns_f["_refresh_kk_panel"](call_same, 1, 0))
check("MESSAGE_NOT_MODIFIED 返回 UNCHANGED（必须与「改成功」区分开）", r4 == "unchanged")
check("MESSAGE_NOT_MODIFIED 记 info 而不是 error", not any(lvl == "error" for lvl, _ in logs), str(logs))
check("MESSAGE_NOT_MODIFIED 不改发新面板（避免刷屏）", len(sent) == 0, str(sent))

# MESSAGE_ID_INVALID：编辑不了 -> 必须改发新面板，并删掉旧面板
logs.clear(); sent.clear()
call_bad = FakeCall("x", edit_fail_with=BadRequest("message id invalid", "MESSAGE_ID_INVALID"))
r3 = run(ns_f["_refresh_kk_panel"](call_bad, 1, 0))
check("编辑被拒后兜底成功，返回 EDITED", r3 == "edited")
check("编辑被拒后确实改发了新面板", len(sent) == 1, f"sent={sent}")
check("新面板发到同一个会话", bool(sent) and sent[0].get("chat_id") == -1001234567890, str(sent))
check("新面板带上了键盘", bool(sent) and sent[0].get("reply_markup") == [["r"]], str(sent))
check("旧面板被删除", call_bad.message.deleted is True)
check("编辑被拒记的是 error 日志", any(lvl == "error" and "MESSAGE_ID_INVALID" in m for lvl, m in logs), str(logs))
check("改发新面板记的是 warning 日志", any(lvl == "warning" for lvl, _ in logs), str(logs))

# 旧面板删不掉也不影响正确性
logs.clear(); sent.clear()
call_nd = FakeCall("x", edit_fail_with=BadRequest("bad", "MESSAGE_ID_INVALID"),
                   delete_fail_with=RuntimeError("delete failed"))
r6 = run(ns_f["_refresh_kk_panel"](call_nd, 1, 0))
check("旧面板删不掉时依然返回 EDITED", r6 == "edited")
check("旧面板删不掉不额外记 error（编辑被拒那条是预期的）",
      not any(lvl == "error" and "delete" in m.lower() for lvl, m in logs), str(logs))

# 连改发都失败：返回 False，不抛异常
logs.clear(); sent.clear()
ns_f["bot"] = _bot_with(lambda **kw: _raise_async(RuntimeError("send failed")))
call_both = FakeCall("x", edit_fail_with=BadRequest("bad", "MESSAGE_ID_INVALID"))
r5 = run(ns_f["_refresh_kk_panel"](call_both, 1, 0))
check("改发也失败时返回 FAILED 且不抛异常", r5 == "failed")
check("改发失败被记进日志", any("改发新面板也失败" in m for _, m in logs), str(logs))

print()
print("════════ 6. 与旧代码的兼容性 ════════")
src_kk = open(f"{REPO}/bot/modules/panel/kk.py", encoding="utf-8").read()
# 原来这里是一条「文件和自己比」的恒真断言，没有任何判别力，换成真检查：
# kk.py 顶层必须显式导入 FloodWait 与 sleep（刷新重试要用），
# 且 sync_kk_panel 必须仍然存在并可从模块外导入。
check("kk.py 顶层导入了 FloodWait", re.search(r"^from pyrogram\.errors import .*FloodWait", src_kk, re.M) is not None)
check("kk.py 顶层导入了 sleep", re.search(r"^from asyncio import sleep", src_kk, re.M) is not None)
check("kk.py 定义了 sync_kk_panel（供并发检测回调）", "async def sync_kk_panel(" in src_kk)

src_fb = open(f"{REPO}/bot/func_helper/fix_bottons.py", encoding="utf-8").read()
check("fix_bottons 的 sql_get_emby 是函数内延迟导入（避免循环导入）",
      "from bot.sql_helper.sql_emby import sql_get_emby" in src_fb and
      src_fb.index("def _warn_count_of") < src_fb.index("from bot.sql_helper.sql_emby import sql_get_emby"))

print()
print("════════ 7. 编辑被限流时必须自动重试（上一版把这能力弄丢了）════════")


def _mk_logger(sink):
    return type("L", (), {
        "info": staticmethod(lambda m, *a, **k: sink.append(("info", m))),
        "warning": staticmethod(lambda m, *a, **k: sink.append(("warning", m))),
        "error": staticmethod(lambda m, *a, **k: sink.append(("error", m))),
        "debug": staticmethod(lambda m, *a, **k: sink.append(("debug", m))),
    })


class FakeMessageSeq:
    """edit() 按顺序抛出给定的异常，抛完就成功；用来测重试。"""

    def __init__(self, excs):
        self._excs = list(excs)
        self.id = 4242
        self.chat = type("Chat", (), {"id": -1001234567890})()
        self.edits = []
        self.deleted = False

    async def edit(self, text=None, **kw):
        if self._excs:
            raise self._excs.pop(0)
        self.edits.append(text)
        return self

    async def delete(self):
        self.deleted = True


class FakeCallWith:
    def __init__(self, message):
        self.data = "x"
        self.id = "cbq-1"
        self.from_user = type("U", (), {"id": 1, "first_name": "管理员"})()
        self.answers = []
        self.message = message

    async def answer(self, text, show_alert=False):
        self.answers.append((text, show_alert))


logs = []
sent = []
ns_w = {}
for fn in extract(f"{REPO}/bot/modules/panel/kk.py",
                  {"_refresh_kk_panel", "_kk_send_with_floodwait", "_send_kk_panel"}).values():
    exec(compile(fn, "kk.py", "exec"), ns_w)
ns_w["BadRequest"] = BadRequest
ns_w["FloodWait"] = FloodWait
ns_w["sleep"] = _fake_sleep
ns_w["_kk_panel_ids"] = {}
ns_w.update(KK_CONSTS)
ns_w["LOGGER"] = _mk_logger(logs)
ns_w["cr_kk_ikb"] = lambda uid, first, wc=None: _async(lambda: ("t", [["r"]]))
ns_w["bot"] = _bot_with(lambda **kw: _async(lambda: sent.append(kw)))

# 前两次 FloodWait，第三次成功
logs.clear(); sent.clear(); _sleeps.clear()
msg_fw = FakeMessageSeq([FloodWait(3)])
r_fw = run(ns_w["_refresh_kk_panel"](FakeCallWith(msg_fw), 1, 2))
check("FloodWait 后会自动重试，最终编辑成功", r_fw == "edited" and msg_fw.edits == ["t"],
      f"r={r_fw} edits={msg_fw.edits}")
check("重试前按 1.2 倍等待", _sleeps == [3 * 1.2], str(_sleeps))
check("限流重试期间没有误发新面板", len(sent) == 0, str(sent))
check("限流重试被记入日志", any("被限流" in m for _, m in logs), str(logs))

# 连续三次都 FloodWait -> 放弃编辑，但必须走兜底
logs.clear(); sent.clear(); _sleeps.clear()
msg_fw3 = FakeMessageSeq([FloodWait(1), FloodWait(1), FloodWait(1)])
r_fw3 = run(ns_w["_refresh_kk_panel"](FakeCallWith(msg_fw3), 1, 2))
check("FloodWait 反复失败后放弃重试", any("放弃重试" in m for _, m in logs), str(logs))
check("放弃编辑后仍然改发新面板（管理员仍能看到最新值）",
      r_fw3 == "edited" and len(sent) == 1, f"r={r_fw3} sent={len(sent)}")

# FloodWait 秒数过大时必须立刻放弃，不能把回调协程挂住几百秒
logs.clear(); sent.clear(); _sleeps.clear()
msg_big = FakeMessageSeq([FloodWait(120), FloodWait(120)])
r_big = run(ns_w["_refresh_kk_panel"](FakeCallWith(msg_big), 1, 2))
check("FloodWait 秒数超过上限时完全不等待（回调不能被挂住）", _sleeps == [], str(_sleeps))
check("超长 FloodWait 后直接走兜底，仍然发出新面板",
      r_big == "edited" and len(sent) == 1, f"r={r_big} sent={len(sent)}")


print()
print("════════ 8. /kk 面板去重：同一用户只保留最新一条 ════════")
logs.clear()
ns_d = {}
for fn in extract(f"{REPO}/bot/modules/panel/kk.py",
                  {"_kk_send_with_floodwait", "_send_kk_panel"}).values():
    exec(compile(fn, "kk.py", "exec"), ns_d)
ns_d["_kk_panel_ids"] = {}
ns_d["FloodWait"] = FloodWait
ns_d["sleep"] = _fake_sleep
ns_d["LOGGER"] = _mk_logger(logs)

deleted = []
_ids = []


async def _del(chat_id, message_ids, **kw):
    deleted.append((chat_id, message_ids))


async def _send(**kw):
    _ids.append(kw)
    return type("M", (), {"id": 7000 + len(_ids)})()


ns_d["bot"] = type("B", (), {"delete_messages": staticmethod(_del),
                             "send_message": staticmethod(_send)})

run(ns_d["_send_kk_panel"](-100, 555, "t1", [["a"]]))
check("第一次发面板不删任何东西", deleted == [], str(deleted))
check("面板 message_id 被记录下来", ns_d["_kk_panel_ids"] == {(-100, 555): 7001},
      str(ns_d["_kk_panel_ids"]))

run(ns_d["_send_kk_panel"](-100, 555, "t2", [["a"]]))
check("第二次发面板前删掉上一条（消除「对着陈旧面板操作」）",
      deleted == [(-100, 7001)], str(deleted))
check("只跟踪最新的那一条", ns_d["_kk_panel_ids"] == {(-100, 555): 7002},
      str(ns_d["_kk_panel_ids"]))

run(ns_d["_send_kk_panel"](-100, 777, "t3", [["a"]]))
check("不同用户互不影响（不会删掉别人的面板）", deleted == [(-100, 7001)], str(deleted))

# 发送失败时绝不能先把旧面板删掉 —— 先删后发会让面板凭空消失，比停在旧值更难查
deleted.clear(); logs.clear()
ns_d["_kk_panel_ids"] = {(-100, 555): 7001}
ns_d["bot"] = type("B", (), {
    "delete_messages": staticmethod(_del),
    "send_message": staticmethod(lambda **kw: _raise_async(RuntimeError("send failed"))),
})
_r_none = run(ns_d["_send_kk_panel"](-100, 555, "t9", [["a"]]))
check("发送失败时返回 None", _r_none is None, repr(_r_none))
check("发送失败时不会删掉旧面板（面板不能凭空消失）", deleted == [], str(deleted))
check("发送失败时保留原有跟踪记录", ns_d["_kk_panel_ids"] == {(-100, 555): 7001},
      str(ns_d["_kk_panel_ids"]))

# 发送成功才删旧面板
deleted.clear()
ns_d["bot"] = type("B", (), {"delete_messages": staticmethod(_del),
                             "send_message": staticmethod(_send)})
run(ns_d["_send_kk_panel"](-100, 555, "t10", [["a"]]))
check("发送成功后才删旧面板", deleted == [(-100, 7001)], str(deleted))


print()
print("════════ 9. 计数被别处改动后，已打开的面板要同步 ════════")
logs.clear()
edits = []
ns_s = {}
for fn in extract(f"{REPO}/bot/modules/panel/kk.py", {"sync_kk_panel"}).values():
    exec(compile(fn, "kk.py", "exec"), ns_s)
ns_s["BadRequest"] = BadRequest
ns_s["FloodWait"] = FloodWait
ns_s["LOGGER"] = _mk_logger(logs)
ns_s["cr_kk_ikb"] = lambda uid, first, wc=None: _async(lambda: ("面板文本", [["k"]]))


def _bot_sync(edit_factory):
    return type("B", (), {
        "get_chat": staticmethod(lambda uid: _async(lambda: type("C", (), {"first_name": "u"})())),
        "edit_message_text": staticmethod(edit_factory),
    })


async def _edit_ok(chat_id, message_id, text=None, **kw):
    edits.append((chat_id, message_id, text))


ns_s["bot"] = _bot_sync(_edit_ok)
ns_s["_kk_panel_ids"] = {(-100, 555): 7001, (-100, 777): 7002}

run(ns_s["sync_kk_panel"](555))
check("只同步目标用户的面板", edits == [(-100, 7001, "面板文本")], str(edits))

edits.clear()
run(ns_s["sync_kk_panel"]())
check("uid=None 时同步全部已跟踪面板", len(edits) == 2, str(edits))

# 面板已被删除 -> 停止跟踪，别每 60 秒白试一次
edits.clear(); logs.clear()
ns_s["bot"] = _bot_sync(lambda chat_id, message_id, text=None, **kw:
                        _raise_async(BadRequest("message id invalid", "MESSAGE_ID_INVALID")))
run(ns_s["sync_kk_panel"](555))
check("面板不可编辑时停止跟踪", (-100, 555) not in ns_s["_kk_panel_ids"],
      str(ns_s["_kk_panel_ids"]))
check("停止跟踪有日志可查", any("停止跟踪" in m for _, m in logs), str(logs))

# MESSAGE_NOT_MODIFIED 属于正常，不能因此丢掉跟踪
ns_s["_kk_panel_ids"] = {(-100, 555): 7001}
logs.clear()
ns_s["bot"] = _bot_sync(lambda chat_id, message_id, text=None, **kw:
                        _raise_async(BadRequest("not modified", "MESSAGE_NOT_MODIFIED")))
run(ns_s["sync_kk_panel"](555))
check("内容一致（NOT_MODIFIED）时不丢弃跟踪", (-100, 555) in ns_s["_kk_panel_ids"],
      str(ns_s["_kk_panel_ids"]))

# 任何异常都不能冒泡：调用方是每 60 秒跑一次的定时任务
logs.clear()
ns_s["bot"] = _bot_sync(lambda chat_id, message_id, text=None, **kw:
                        _raise_async(RuntimeError("boom")))
try:
    run(ns_s["sync_kk_panel"](555))
    _no_raise = True
except Exception:
    _no_raise = False
check("同步面板抛异常时不会冒泡到定时任务", _no_raise)
check("同步失败被记入日志", any("同步 /kk 面板失败" in m for _, m in logs), str(logs))


print()
print("════════ 10. 面板刷新失败时，管理员必须收到告警 ════════")
db_a = {555: FakeEmbyRow(555, 2)}
ns_a, _reg_a = make_kk_ns(db_a, [])
# 让刷新必然失败：直接桩掉，模拟「编辑失败 + 兜底也失败」
ns_a["_refresh_kk_panel"] = lambda call, uid, wc=None: _async("failed")

call_a = FakeCall("warn_minus-555")
run(ns_a["kk_warn_minus"](None, call_a))
check("计数仍然写入了数据库", db_a[555].concurrent_warn_count == 1,
      str(db_a[555].concurrent_warn_count))
check("失败时不再先弹「✅ 已改」（应答只能有一次，先答成功就再也补不了告警）",
      not any("2 → 1" in a[0] for a in call_a.answers), str(call_a.answers))
check("刷新失败时改为 show_alert 告警（不再让管理员以为一切正常）",
      any(a[1] is True and "刷新失败" in a[0] for a in call_a.answers), str(call_a.answers))
check("失败时不再先弹「✅ 已改」——应答只发生一次",
      len(call_a.answers) == 1, str(call_a.answers))

db_b = {555: FakeEmbyRow(555, 2)}
ns_b, _reg_b = make_kk_ns(db_b, [])
call_b = FakeCall("warn_minus-555")
run(ns_b["kk_warn_minus"](None, call_b))
check("刷新成功时不出现告警", not any("刷新失败" in a[0] for a in call_b.answers),
      str(call_b.answers))
check("成功时也只应答一次", len(call_b.answers) == 1, str(call_b.answers))

# 命中 NOT_MODIFIED：数据是对的，但必须解释「为什么数字没动」
db_c = {555: FakeEmbyRow(555, 1)}
ns_c, _reg_c = make_kk_ns(db_c, [])
ns_c["_refresh_kk_panel"] = lambda call, uid, wc=None: _async("unchanged")
call_c = FakeCall("warn_minus-555")
run(ns_c["kk_warn_minus"](None, call_c))
check("UNCHANGED 时计数仍然写入了", db_c[555].concurrent_warn_count == 0)
check("UNCHANGED 时提示解释了「外观未变」",
      any("外观未变" in a[0] for a in call_c.answers), str(call_c.answers))
check("UNCHANGED 时也仍然只应答一次", len(call_c.answers) == 1, str(call_c.answers))


print()
print("════════ 11. editMessage 的三条静默分支必须留下日志 ════════")
from typing import Optional  # noqa: E402

logs.clear()
ns_e = {
    "BadRequest": BadRequest,
    "FloodWait": FloodWait,
    "LOGGER": _mk_logger(logs),
    "sleep": _fake_sleep,
    # Optional 必须就位：签名里的 Optional["enums.ParseMode"] 在 def 时就会求值
    "Optional": Optional,
    "CallbackQuery": type("CallbackQuery", (), {}),
    "deleteMessage": lambda *a, **k: _async(True),
}
for fn in extract(f"{REPO}/bot/func_helper/msg_utils.py",
                  {"editMessage", "_edit_target_of"}).values():
    exec(compile(fn, "msg_utils.py", "exec"), ns_e)


class _MsgRaising:
    def __init__(self, exc):
        self._exc = exc
        self.id = 4242
        self.chat = type("C", (), {"id": -100})()
        self.deleted = False

    async def edit(self, **kw):
        raise self._exc

    async def delete(self):
        self.deleted = True


for _branch in ("BUTTON_URL_INVALID", "MESSAGE_NOT_MODIFIED", "MESSAGE_ID_INVALID"):
    logs.clear()
    _r = run(ns_e["editMessage"](_MsgRaising(BadRequest("x", _branch)), "文本"))
    check(f"{_branch}: 返回值语义不变（仍然 False）", _r is False, repr(_r))
    check(f"{_branch}: 留下了日志（改动前完全静默）", len(logs) > 0, str(logs))
    check(f"{_branch}: 日志带上了 msg id 与 chat id",
          any("msg=4242" in m and "chat=-100" in m for _, m in logs), str(logs))

# 未知错误码也不能吞掉
logs.clear()
_r_unk = run(ns_e["editMessage"](_MsgRaising(BadRequest("weird", "SOMETHING_ELSE")), "文本"))
check("未知 BadRequest 仍然记 warning", any(lvl == "warning" for lvl, _ in logs), str(logs))

# FloodWait 重试能力必须保留
logs.clear(); _sleeps.clear()
_msg_fw = FakeMessageSeq([FloodWait(2)])
_r_fw = run(ns_e["editMessage"](_msg_fw, "文本"))
check("editMessage 遇到 FloodWait 仍然 sleep 后重试",
      _r_fw is True and _msg_fw.edits == ["文本"], f"r={_r_fw} edits={_msg_fw.edits}")
check("editMessage 重试前按 1.2 倍等待", _sleeps == [2 * 1.2], str(_sleeps))


print()
print("════════ 12. 并发检测写入计数后必须回调面板同步 ════════")
src_mon = open(f"{REPO}/bot/modules/extra/concurrent_play_monitor.py", encoding="utf-8").read()
check("定义了 _sync_kk_panels", "async def _sync_kk_panels(" in src_mon)
check("警告 +1 之后立刻调用同步",
      "sql_update_emby(Emby.tg == tg_id, concurrent_warn_count=new_warn_count)"
      "\n        if tg_id:\n            await _sync_kk_panels(tg_id)" in src_mon)
check("全局清零之后调用同步",
      'LOGGER.info("已重置所有用户的同时播放警告计数")\n            await _sync_kk_panels()' in src_mon)
check("同步用函数内延迟导入（避免循环导入）",
      "from bot.modules.panel.kk import sync_kk_panel" in src_mon)
check("同步被 try/except 包住（定时任务不能被拖垮）",
      "await sync_kk_panel(uid)\n    except Exception as e:" in src_mon)

logs.clear()
ns_m = {}
for fn in extract(f"{REPO}/bot/modules/extra/concurrent_play_monitor.py",
                  {"_sync_kk_panels"}).values():
    exec(compile(fn, "mon.py", "exec"), ns_m)
ns_m["LOGGER"] = _mk_logger(logs)
try:
    run(ns_m["_sync_kk_panels"](555))
    _m_ok = True
except Exception:
    _m_ok = False
check("导入/执行失败时 _sync_kk_panels 不抛异常", _m_ok)


print()
print("════════ 12b. 读不到计数时不能假装是 0 ════════")
_src_fb = open(f"{REPO}/bot/func_helper/fix_bottons.py", encoding="utf-8").read()
check("读失败用自解释文案，而不是孤零零的 ?", "'读取失败' if warn_count is None" in _src_fb)
check("不再有 shown = '?' 这种普通用户看不懂的写法", "shown = '?'" not in _src_fb)
check("_warn_count_of 的读失败路径全部返回 None",
      _src_fb.count("        return None") >= 3, str(_src_fb.count("        return None")))


print()
print("════════ 12c. 三态返回值常量 ════════")
check("_REFRESH_EDITED == 'edited'", KK_CONSTS["_REFRESH_EDITED"] == "edited")
check("_REFRESH_UNCHANGED == 'unchanged'", KK_CONSTS["_REFRESH_UNCHANGED"] == "unchanged")
check("_REFRESH_FAILED == 'failed'", KK_CONSTS["_REFRESH_FAILED"] == "failed")
check("FloodWait 等待上限是个有限值（防止回调被挂住几百秒）",
      isinstance(KK_CONSTS["_FLOODWAIT_MAX_WAIT"], int)
      and 0 < KK_CONSTS["_FLOODWAIT_MAX_WAIT"] <= 60,
      str(KK_CONSTS["_FLOODWAIT_MAX_WAIT"]))


print()
print("════════ 13. 修复必须真的针对用户可见故障 ════════")
src_kk2 = open(f"{REPO}/bot/modules/panel/kk.py", encoding="utf-8").read()
check("刷新用的是「刚写入的值」，不再二次读库",
      "await cr_kk_ikb(uid, first.first_name, warn_count)" in src_kk2)
check("_apply_warn_change 消费了刷新返回值",
      "result = await _refresh_kk_panel(call, uid, new_value)" in src_kk2)
check("应答发生在刷新之后（先答成功就再也补不了告警）",
      src_kk2.index("result = await _refresh_kk_panel(call, uid, new_value)")
      < src_kk2.index("await call.answer(action_desc)"))
check("_apply_warn_change 里只有一条成功路径的 answer（不会二次应答）",
      src_kk2.count("await call.answer(action_desc)") == 1)
check("_refresh_kk_panel 的日志带上了 msg/chat/call id",
      "msg={getattr(call.message, 'id', None)}" in src_kk2 and
      "call={getattr(call, 'id', None)}" in src_kk2)
check("不再用 getattr(e,'ID','?') 这种永远拿不到 '?' 的写法",
      "getattr(e, 'ID', '?')" not in src_kk2)

print()
print("════════════════════════════════════════")
print(f"  结果：PASS={PASS}  FAIL={FAIL}")
print("════════════════════════════════════════")
sys.exit(1 if FAIL else 0)
