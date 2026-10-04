# -*- coding: utf-8 -*-
"""
「➕ 警告+1」按钮功能测试。

做法与 tests/test_kk_warn_panel.py 对齐：从真实源文件里用 AST 抽出函数体，
在受控命名空间里执行，只桩掉外部依赖（DB / Emby / pyrogram / ikb）。
测的是真代码，不是复制的副本。

覆盖任务书要求的 5 条既有约束：
  1. 每次回调只能 call.answer 一次
  2. 必须先刷新面板、再应答（三态 EDITED / UNCHANGED / FAILED 文案各异）
  3. 手动 +1 不自动封禁，但达到阈值要提示
  4. 只写 concurrent_warn_count，不碰 lv / Emby policy
  5. 无 Emby 账户的用户不显示警告行与按钮
"""
import ast
import asyncio
import os
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
            # 装饰器在 decorator_list 里，必须单独取最小行号，
            # 否则 @bot.on_callback_query(...) 会被丢掉，回调就不会注册。
            if getattr(node, "decorator_list", None):
                start = min(d.lineno for d in node.decorator_list)
            out[nm] = "\n".join(lines[start - 1:node.end_lineno])
    missing = set(names) - set(out)
    if missing:
        raise RuntimeError(f"{path} 里找不到: {missing}")
    return out


def load_constants(path, names):
    """抽出模块级常量赋值，避免测试里硬编码副本。"""
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
    def __init__(self, tg, warn, lv="b", embyid="abc", ex="2026-12-31"):
        self.tg = tg
        self.concurrent_warn_count = warn
        self.lv = lv
        self.embyid = embyid
        self.ex = ex


class BadRequest(Exception):
    """模拟 pyrogram.errors.BadRequest：带 .ID 属性。"""
    def __init__(self, msg="bad request", ID="UNKNOWN"):
        super().__init__(msg)
        self.ID = ID


class FloodWait(Exception):
    """模拟 pyrogram.errors.FloodWait：带 .value 秒数。不是 BadRequest 子类。"""
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


def make_kk_ns(db, edits, is_admin=True, send_fails=False, threshold=3, updates=None):
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
        if send_fails:
            raise RuntimeError("发送失败（测试桩）")
        panels_sent.append(kw)
        return type("M", (), {"id": 9000 + len(panels_sent)})()

    ns["bot"] = type("B", (), {"on_callback_query": staticmethod(on_callback_query),
                               "get_chat": staticmethod(_get_chat),
                               "delete_messages": staticmethod(_delete_messages),
                               "send_message": staticmethod(_send_message)})
    ns["filters"] = type("F", (), {"regex": staticmethod(lambda p: p)})

    # _warn_button_precheck 必须在名单里：三个 handler 都调它，漏了就是 NameError。
    for fn in extract(f"{REPO}/bot/modules/panel/kk.py",
                      {"_refresh_kk_panel", "_load_warn_state", "_apply_warn_change",
                       "_warn_button_precheck", "kk_warn_plus", "kk_warn_minus",
                       "kk_warn_reset", "_kk_send_with_floodwait", "_send_kk_panel"}).values():
        exec(compile(fn, "kk.py", "exec"), ns)

    ns["_kk_panel_ids"] = {}
    ns["FloodWait"] = FloodWait
    ns["sleep"] = _fake_sleep
    ns["BadRequest"] = BadRequest
    ns.update(KK_CONSTS)
    ns["config"] = type("Cfg", (), {"concurrent_play_warn_threshold": threshold})
    ns["LOGGER"] = type("L", (), {
        "info": staticmethod(lambda *a, **k: None),
        "debug": staticmethod(lambda *a, **k: None),
        "error": staticmethod(lambda *a, **k: None),
        "warning": staticmethod(lambda *a, **k: None),
    })
    ns["judge_admins"] = lambda uid: is_admin

    def sql_get_emby(uid):
        return db.get(uid)

    def sql_update_emby(cond, **kwargs):
        # 还原 sql_update_emby 的真实语义：只更新匹配到的那一行
        uid = getattr(cond, "right", None)
        if updates is not None:
            updates.append((uid, dict(kwargs)))
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

    async def fake_cr_kk_ikb(uid, first, warn_count=None):
        edits.append(("RENDER", warn_count))
        return "面板文本", [["行1"], ["行2"]]

    ns["cr_kk_ikb"] = fake_cr_kk_ikb
    return ns, registry


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ══════════════════════════════════════════════════════════════════
print("════════ 1. cr_kk_ikb 布局：重置警告并入「额外媒体库」行，+1/-1 单独一行 ════════")

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


text, kb = render_panel(libs=["额外库A"])   # 有「额外媒体库」按钮
text_no, kb_no = render_panel(libs=[])      # 无「额外媒体库」按钮


def flat_of(kb_):
    return [b for row in kb_ for b in row]


def rows_with(kb_, needle):
    return [i for i, row in enumerate(kb_) if any(needle in b[1] for b in row)]


flat = flat_of(kb)
labels = [b[0] for b in flat]
datas = [b[1] for b in flat]

check("有「➕ 警告+1」按钮", any("警告+1" in l for l in labels), str(labels))
check("回调数据 warn_plus-<tgid>", "warn_plus-5608153118" in datas, str(datas))
check("原有的 -1 / 重置 按钮都还在",
      "warn_minus-5608153118" in datas and "warn_reset-5608153118" in datas, str(datas))

# ── 布局（有额外媒体库）：重置警告与「关闭/开启 额外媒体库」同一行，+1/-1 单独一行 ──
ext_l = rows_with(kb, "embyextralib_")
res_l = rows_with(kb, "warn_reset-")
pm_l = rows_with(kb, "warn_plus-")
print(f"  有额外媒体库：额外库行={ext_l} 重置行={res_l} +1/-1 行={pm_l}")
check("有额外媒体库时，重置警告与额外媒体库同一行",
      len(ext_l) == 1 and ext_l == res_l, f"额外库={ext_l} 重置={res_l}")
check("该行顺序为 [额外媒体库, 重置警告]",
      len(res_l) == 1 and [b[1] for b in kb[res_l[0]]] ==
      ["embyextralib_block-5608153118", "warn_reset-5608153118"],
      str(kb[res_l[0]] if res_l else None))
check("有额外媒体库时，+1/-1 单独一行且不含重置",
      len(pm_l) == 1 and [b[1] for b in kb[pm_l[0]]] ==
      ["warn_plus-5608153118", "warn_minus-5608153118"],
      str(kb[pm_l[0]] if pm_l else None))
check("+1/-1 行的顺序为 ➕ / ➖",
      len(pm_l) == 1 and kb[pm_l[0]][0][0].startswith("➕")
      and kb[pm_l[0]][1][0].startswith("➖"),
      str([b[0] for b in kb[pm_l[0]]] if pm_l else None))

# ── 布局（无额外媒体库）：重置警告单独占一行，位置稳定，不挤进警告行 ──
ext_n = rows_with(kb_no, "embyextralib_")
res_n = rows_with(kb_no, "warn_reset-")
pm_n = rows_with(kb_no, "warn_plus-")
print(f"  无额外媒体库：额外库行={ext_n} 重置行={res_n} +1/-1 行={pm_n}")
check("无额外媒体库时确实没有额外媒体库按钮", ext_n == [], str(ext_n))
check("无额外媒体库时，重置警告单独占一行",
      len(res_n) == 1 and len(kb_no[res_n[0]]) == 1,
      f"重置={res_n} 行={kb_no[res_n[0]] if res_n else None}")
check("无额外媒体库时，重置警告不与 +1/-1 同行", res_n != pm_n, f"重置={res_n} +1={pm_n}")
check("无额外媒体库时，+1/-1 仍是同一行",
      len(pm_n) == 1 and [b[1] for b in kb_no[pm_n[0]]] ==
      ["warn_plus-5608153118", "warn_minus-5608153118"],
      str(kb_no[pm_n[0]] if pm_n else None))
check("两种布局下重置警告都恰好出现一次（既不重复也不消失）",
      len(res_l) == 1 and len(res_n) == 1, f"有库={res_l} 无库={res_n}")

# 静态确认：警告行是在 array_chunk 之后 append 的，不可能被分块
_src_fb = open(f"{REPO}/bot/func_helper/fix_bottons.py", encoding="utf-8").read()
check("静态：警告行 append 发生在 array_chunk 之后（不参与分块）",
      _src_fb.index("lines = array_chunk(keyboard, 2)")
      < _src_fb.index("['➕ 警告+1'"))
check("静态：保留了「不参与 array_chunk」的注释说明",
      "不参与上面的 array_chunk" in _src_fb)
check("静态：重置警告的两种落位都由 has_extralib 决定",
      "if has_extralib:" in _src_fb and "lines.append([reset_btn])" in _src_fb,
      "缺少 has_extralib 分支")
check("静态：has_extralib 由长度差判断（不硬编码按钮下标）",
      "has_extralib = len(keyboard) > 2" in _src_fb)

# 约束 5：无 Emby 账户不显示警告行与按钮
text_n, kb_n = render_panel(account=False)
flat_n = [b for row in kb_n for b in row]
check("无 Emby 账户时不显示警告计数行", "并发警告" not in text_n, repr(text_n[-80:]))
check("无 Emby 账户时不显示警告按钮（含新的 +1）",
      not any("warn_" in b[1] for b in flat_n), str([b[1] for b in flat_n]))


# ══════════════════════════════════════════════════════════════════
print()
print("════════ 2. 「➕ 警告+1」正常加一 ════════")

db1 = {5608153118: FakeEmbyRow(5608153118, 1)}
updates1 = []
ns1, reg1 = make_kk_ns(db1, [], updates=updates1)
call1 = FakeCall("warn_plus-5608153118")
run(reg1["^warn_plus-"](None, call1))

check("计数 1 -> 2", db1[5608153118].concurrent_warn_count == 2,
      str(db1[5608153118].concurrent_warn_count))
check("面板被刷新（发生了 EDIT）", any(e[0] == "EDIT" for e in call1.edits), str(call1.edits))
check("只 answer 了一次", len(call1.answers) == 1, str(call1.answers))
check("应答文案写明了 1 → 2",
      call1.answers and "1 → 2" in call1.answers[0][0], str(call1.answers))
check("应答不是告警弹窗", call1.answers and call1.answers[0][1] is False, str(call1.answers))

# 约束 4：只写 concurrent_warn_count 一个字段
check("只写 concurrent_warn_count 这一个字段",
      len(updates1) == 1 and set(updates1[0][1]) == {"concurrent_warn_count"},
      str(updates1))

# 约束 4：不碰 lv / embyid / ex
row1 = db1[5608153118]
check("lv 未被改动", row1.lv == "b", str(row1.lv))
check("embyid 未被改动", row1.embyid == "abc", str(row1.embyid))
check("ex 未被改动", row1.ex == "2026-12-31", str(row1.ex))

# 约束 3：手动 +1 不自动封禁
# 注意：必须用 AST 看「实际调用了什么」，不能用 "ban_user" in 源码做字符串判断 ——
# 那段解释「为什么手动不封禁」的注释里本来就写着 ban_user，字符串判断会误报。
def calls_in(fn_src):
    """返回这段函数源码里实际被调用的名字集合（只看 Call，不看注释/字符串）。"""
    found = set()
    for node in ast.walk(ast.parse(fn_src)):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                found.add(f.id)
            elif isinstance(f, ast.Attribute):
                found.add(f.attr)
    return found


_src_plus = extract(f"{REPO}/bot/modules/panel/kk.py", {"kk_warn_plus"})["kk_warn_plus"]
_plus_calls = calls_in(_src_plus)
check("静态：kk_warn_plus 里没有 ban_user 调用（手动调整不自动封禁）",
      "ban_user" not in _plus_calls, str(sorted(_plus_calls)))
check("静态：kk_warn_plus 里没有封禁/改策略相关调用",
      not ({"ban_user", "emby_block", "create_policy"} & _plus_calls), str(sorted(_plus_calls)))
check("静态：kk_warn_plus 里保留了「不自动封禁」的说明（给后人看）",
      "不自动封禁" in _src_plus)


# ══════════════════════════════════════════════════════════════════
print()
print("════════ 3. 达到阈值时的提示（约束 3）════════")

# 阈值 3，当前 2 -> 新值 3，正好达到
db3 = {5608153118: FakeEmbyRow(5608153118, 2)}
ns3, reg3 = make_kk_ns(db3, [], threshold=3)
call3 = FakeCall("warn_plus-5608153118")
run(reg3["^warn_plus-"](None, call3))
check("阈值 3、当前 2 -> 3：计数写入 3", db3[5608153118].concurrent_warn_count == 3)
check("达到阈值时只 answer 一次", len(call3.answers) == 1, str(call3.answers))
check("提示里写明已达/超过阈值 3",
      any("已达/超过阈值 3" in a[0] for a in call3.answers), str(call3.answers))
check("提示里指明去点『💢 禁用账户』",
      any("禁用账户" in a[0] for a in call3.answers), str(call3.answers))
check("提示里说明手动调整不会自动封禁",
      any("手动调整不会自动封禁" in a[0] for a in call3.answers), str(call3.answers))

# 超过阈值
db4 = {5608153118: FakeEmbyRow(5608153118, 5)}
ns4, reg4 = make_kk_ns(db4, [], threshold=3)
call4 = FakeCall("warn_plus-5608153118")
run(reg4["^warn_plus-"](None, call4))
check("超过阈值（5 -> 6）也提示", any("已达/超过阈值 3" in a[0] for a in call4.answers),
      str(call4.answers))

# 未达阈值：不该出现封禁提示
db5 = {5608153118: FakeEmbyRow(5608153118, 0)}
ns5, reg5 = make_kk_ns(db5, [], threshold=3)
call5 = FakeCall("warn_plus-5608153118")
run(reg5["^warn_plus-"](None, call5))
check("未达阈值（0 -> 1）不出现封禁提示",
      not any("禁用账户" in a[0] for a in call5.answers), str(call5.answers))


# ══════════════════════════════════════════════════════════════════
print()
print("════════ 4. 前置校验：非管理员 / 用户不存在 / 坏数据 ════════")

# 非管理员
db6 = {5608153118: FakeEmbyRow(5608153118, 1)}
ns6, reg6 = make_kk_ns(db6, [], is_admin=False)
call6 = FakeCall("warn_plus-5608153118", uid=111)
run(reg6["^warn_plus-"](None, call6))
check("非管理员被拒绝", any("以下犯上" in a[0] for a in call6.answers), str(call6.answers))
check("非管理员：只 answer 一次", len(call6.answers) == 1, str(call6.answers))
check("非管理员：计数未被改动", db6[5608153118].concurrent_warn_count == 1,
      str(db6[5608153118].concurrent_warn_count))
check("非管理员：面板未被刷新", not any(e[0] == "EDIT" for e in call6.edits), str(call6.edits))

# 用户不存在
ns7, reg7 = make_kk_ns({}, [])
call7 = FakeCall("warn_plus-999")
run(reg7["^warn_plus-"](None, call7))
check("用户不存在被拒绝", any("没有注册账户" in a[0] for a in call7.answers), str(call7.answers))
check("用户不存在：只 answer 一次", len(call7.answers) == 1, str(call7.answers))

# 坏数据
ns8, reg8 = make_kk_ns({}, [])
call8 = FakeCall("warn_plus-abc")
run(reg8["^warn_plus-"](None, call8))
check("uid 非数字时给出格式错误", any("格式错误" in a[0] for a in call8.answers),
      str(call8.answers))
check("坏数据：只 answer 一次", len(call8.answers) == 1, str(call8.answers))


# ══════════════════════════════════════════════════════════════════
print()
print("════════ 5. 三态刷新：EDITED / UNCHANGED / FAILED ════════")

# 约束 2：UNCHANGED（面板此前已显示该值）—— 必须解释，且仍只 answer 一次
# 当前 2、阈值 3 -> 新值 3，正好达到阈值，所以提示也应同时出现。
db9 = {5608153118: FakeEmbyRow(5608153118, 2)}
ns9, reg9 = make_kk_ns(db9, [], threshold=3)
call9 = FakeCall("warn_plus-5608153118",
                 edit_fail_with=BadRequest("not modified", ID="MESSAGE_NOT_MODIFIED"))
run(reg9["^warn_plus-"](None, call9))
check("UNCHANGED：计数仍写入 3", db9[5608153118].concurrent_warn_count == 3)
check("UNCHANGED：只 answer 一次", len(call9.answers) == 1, str(call9.answers))
check("UNCHANGED：解释「面板此前已显示该值，故外观未变」",
      any("面板此前已显示该值，故外观未变" in a[0] for a in call9.answers), str(call9.answers))
check("UNCHANGED：仍带上阈值提示（达到阈值时）",
      any("已达/超过阈值 3" in a[0] for a in call9.answers), str(call9.answers))

# 约束 2：FAILED —— 必须 show_alert 让管理员去重新 /kk，且仍只 answer 一次
db10 = {5608153118: FakeEmbyRow(5608153118, 1)}
ns10, reg10 = make_kk_ns(db10, [], send_fails=True)
call10 = FakeCall("warn_plus-5608153118",
                  edit_fail_with=BadRequest("cannot edit", ID="MESSAGE_ID_INVALID"))
run(reg10["^warn_plus-"](None, call10))
check("FAILED：计数仍写入 2（DB 写入在刷新之前）",
      db10[5608153118].concurrent_warn_count == 2)
check("FAILED：只 answer 一次", len(call10.answers) == 1, str(call10.answers))
check("FAILED：用 show_alert 提示重新 /kk",
      call10.answers and call10.answers[0][1] is True
      and "面板刷新失败" in call10.answers[0][0], str(call10.answers))
check("FAILED：提示里让管理员重新 /kk",
      call10.answers and "/kk" in call10.answers[0][0], str(call10.answers))

# 约束 1 的核心：任何路径下都只能 answer 一次
print()
_all_calls = [("正常 +1", call1), ("达阈值", call3), ("超阈值", call4), ("未达阈值", call5),
              ("非管理员", call6), ("用户不存在", call7), ("坏数据", call8),
              ("UNCHANGED", call9), ("FAILED", call10)]
for _name, _c in _all_calls:
    check(f"约束1：{_name} 路径下 call.answer 恰好一次", len(_c.answers) == 1,
          f"实际 {len(_c.answers)} 次: {_c.answers}")

# 约束 2 的静态确认：应答发生在刷新之后
src_kk = open(f"{REPO}/bot/modules/panel/kk.py", encoding="utf-8").read()
check("静态：_apply_warn_change 先刷新后应答",
      src_kk.index("result = await _refresh_kk_panel(call, uid, new_value)")
      < src_kk.index("await call.answer(action_desc)"))
check("静态：成功路径只有一条 answer（不会二次应答）",
      src_kk.count("await call.answer(action_desc)") == 1)


# ══════════════════════════════════════════════════════════════════
print()
print("════════ 6. 共用前置校验函数被三个 handler 复用 ════════")

_src_all = open(f"{REPO}/bot/modules/panel/kk.py", encoding="utf-8").read()
_tree_kk = ast.parse(_src_all)
_fn_src = {}
for _n in _tree_kk.body:
    if isinstance(_n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        _fn_src[_n.name] = ast.get_source_segment(_src_all, _n)

check("_warn_button_precheck 存在", "_warn_button_precheck" in _fn_src)
for _h in ("kk_warn_plus", "kk_warn_minus", "kk_warn_reset"):
    check(f"{_h} 调用了 _warn_button_precheck",
          _h in _fn_src and "_warn_button_precheck(call)" in _fn_src[_h],
          _fn_src.get(_h, "")[:120])
    check(f"{_h} 里没有重复的管理员判断（已收敛到 precheck）",
          "judge_admins" not in _fn_src.get(_h, ""))
    check(f"{_h} 里没有重复的 uid 解析（已收敛到 precheck）",
          'call.data.split("-")' not in _fn_src.get(_h, ""))
    check(f"{_h} 里没有重复的 sql_get_emby 查询（已收敛到 precheck）",
          "sql_get_emby" not in _fn_src.get(_h, ""))

# 校验失败时 precheck 自己 answer 并返回 None，调用方直接 return
_src_pre = _fn_src.get("_warn_button_precheck", "")
check("precheck 失败路径自己 call.answer", "_src_pre" != "" and
      _src_pre.count("await call.answer(") == 3, str(_src_pre.count("await call.answer(")))
check("precheck 失败路径返回 None", _src_pre.count("return None") == 3,
      str(_src_pre.count("return None")))
check("precheck 成功返回四元组 (uid, e, cur, threshold)",
      "return uid, e, cur, threshold" in _src_pre)


# ══════════════════════════════════════════════════════════════════
print()
print("════════ 7. 既有 -1 / 重置 走同一套校验（未被改坏）════════")

db11 = {5608153118: FakeEmbyRow(5608153118, 3)}
ns11, reg11 = make_kk_ns(db11, [])
call11 = FakeCall("warn_minus-5608153118")
run(reg11["^warn_minus-"](None, call11))
check("-1 仍然可用：3 -> 2", db11[5608153118].concurrent_warn_count == 2)
check("-1 仍然只 answer 一次", len(call11.answers) == 1, str(call11.answers))

call12 = FakeCall("warn_reset-5608153118")
run(reg11["^warn_reset-"](None, call12))
check("重置仍然可用：2 -> 0", db11[5608153118].concurrent_warn_count == 0)
check("重置仍然只 answer 一次", len(call12.answers) == 1, str(call12.answers))
check("重置文案仍写明原值 2", any("原 2" in a[0] for a in call12.answers), str(call12.answers))

# 边界：已经是 0 时 -1 不变负数
call13 = FakeCall("warn_minus-5608153118")
run(reg11["^warn_minus-"](None, call13))
check("已是 0 时 -1 不会变负数", db11[5608153118].concurrent_warn_count == 0)
check("已是 0 时给出提示且只 answer 一次",
      len(call13.answers) == 1 and "已经是 0" in call13.answers[0][0], str(call13.answers))

# 非管理员也不能通过 -1 / 重置
ns12, reg12 = make_kk_ns({5608153118: FakeEmbyRow(5608153118, 3)}, [], is_admin=False)
call14 = FakeCall("warn_minus-5608153118", uid=111)
run(reg12["^warn_minus-"](None, call14))
check("非管理员不能 -1", any("以下犯上" in a[0] for a in call14.answers), str(call14.answers))
call15 = FakeCall("warn_reset-5608153118", uid=111)
run(reg12["^warn_reset-"](None, call15))
check("非管理员不能重置", any("以下犯上" in a[0] for a in call15.answers), str(call15.answers))


print()
print("════════════════════════════════════════")
print(f"  结果：PASS={PASS}  FAIL={FAIL}")
print("════════════════════════════════════════")
sys.exit(1 if FAIL else 0)
