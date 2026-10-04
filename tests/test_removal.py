#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证「播放速率限制」功能已被彻底移除，且删除没有破坏其他行为。
用 AST 从真实源文件里抽出目标函数来执行，避免拉起重依赖。
"""
import ast, importlib.util, json, os, sys, types

REPO = os.environ.get("WARN_TEST_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS = FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  [PASS] {name}")
    else:
        FAIL += 1; print(f"  [FAIL] {name}  {detail}")

def extract(path, func_name):
    """从源文件里抽出某个顶层函数的源码并编译。"""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return compile(ast.Module(body=[node], type_ignores=[]), path, "exec")
    raise AssertionError(f"{path} 里找不到 {func_name}")


print("=" * 72)
print("测试 1：config.json 向后兼容（线上配置里还有 3 个旧键，必须不报错）")
print("=" * 72)
try:
    spec = importlib.util.spec_from_file_location("schemas", f"{REPO}/bot/schemas/schemas.py")
    schemas = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(schemas)
except ModuleNotFoundError as e:
    print(f"  [SKIP] 本地缺 {e.name}（容器镜像里有；CI 会先 pip install pydantic）")
    schemas = None

# 必须以一份**完整合法**的配置为底再叠加旧键。
# 原先这里只给了一个残缺的 dict，本地又因为缺 pydantic 走了 SKIP 分支，
# 于是「全绿」是假象；在容器里真正执行时才暴露 ValidationError（缺 bot_name
# 等 20 个必填字段）。现在用仓库里的 config_example.json 作底，本项才真的在测东西。
live_like = json.load(open(f"{REPO}/config_example.json", encoding="utf-8"))
live_like.update({
    "concurrent_play_limit_enabled": True,
    "concurrent_play_limit": 1,
    "concurrent_play_limit_whitelist_enabled": True,
    "concurrent_play_limit_whitelist": 4,
    "concurrent_play_warn_threshold": 3,
    "concurrent_play_check_interval": 60,
    # ↓ 旧版本遗留，删除后必须被忽略而不是抛异常
    "playback_rate_limit_enabled": True,
    "playback_rate_limit": 8,
    "playback_rate_limit_whitelist": 0,
})
cfg = None
if schemas is not None:
    try:
        cfg = schemas.Config(**live_like)
        check("含旧限速键的完整 config.json 仍能加载", True)
    except Exception as e:
        check("含旧限速键的完整 config.json 仍能加载", False, f"{type(e).__name__}: {e}")
        cfg = None
else:
    print("  [SKIP] 本项未执行 —— CI 里会装 pydantic，届时会真正跑起来")

if cfg is not None:
    for attr in ("playback_rate_limit_enabled", "playback_rate_limit", "playback_rate_limit_whitelist"):
        check(f"字段 {attr} 已从模型移除", not hasattr(cfg, attr))
    check("同时播放限制字段仍在", cfg.concurrent_play_limit == 1)
    check("警告阈值字段仍在", cfg.concurrent_play_warn_threshold == 3)
    dumped = cfg.model_dump()
    check("model_dump() 不再输出限速键",
          not any("playback_rate" in k for k in dumped),
          [k for k in dumped if "playback_rate" in k])

print()
print("=" * 72)
print("测试 2：create_policy() 不再写入 RemoteClientBitrateLimit")
print("=" * 72)
ns = {"extra_emby_libs": ["成人", "福利"]}
exec(extract(f"{REPO}/bot/func_helper/emby.py", "create_policy"), ns)
create_policy = ns["create_policy"]

p = create_policy(admin=False, disable=False)
check("策略里没有 RemoteClientBitrateLimit", "RemoteClientBitrateLimit" not in p,
      [k for k in p if "Bitrate" in k])
check("同时播放流上限仍在", p.get("SimultaneousStreamLimit") == 2, p.get("SimultaneousStreamLimit"))
check("视频转码仍禁用（本次不动这个策略）", p.get("EnableVideoPlaybackTranscoding") is False)
check("媒体库屏蔽仍在", isinstance(p.get("BlockedMediaFolders"), list) and len(p["BlockedMediaFolders"]) > 0)
try:
    p2 = create_policy(admin=False, disable=False, limit=5, block=["x"])
    check("create_policy 的 limit/block 参数仍可用", p2["SimultaneousStreamLimit"] == 5 and p2["BlockedMediaFolders"] == ["x"])
except Exception as e:
    check("create_policy 的 limit/block 参数仍可用", False, f"{type(e).__name__}: {e}")
try:
    create_policy(admin=False, disable=False, whitelist=True, tg=123)
    check("whitelist/tg 参数已移除（传了应该报错）", False, "居然没报错")
except TypeError:
    check("whitelist/tg 参数已移除（传了应该报错）", True)

print()
print("=" * 72)
print("测试 3：面板键盘 —— 同时播放限制与客户端过滤同排，无播放速率按钮")
print("=" * 72)

# 桩：config_panel 提供 _concurrent_toggle_label
cp = types.ModuleType("bot.modules.panel.config_panel")
cp._concurrent_toggle_label = lambda: "✅ 同时播放限制"
sys.modules.setdefault("bot", types.ModuleType("bot"))
sys.modules.setdefault("bot.modules", types.ModuleType("bot.modules"))
sys.modules.setdefault("bot.modules.panel", types.ModuleType("bot.modules.panel"))
sys.modules["bot.modules.panel.config_panel"] = cp

class O: pass
_open = O(); _open.leave_ban = True; _open.uplays = False; _open.checkin_lv = "b"
moviepilot = O(); moviepilot.status = True
auto_update = O(); auto_update.status = False
red_envelope = O(); red_envelope.status = True; red_envelope.allow_private = False
config = O(); config.kk_gift_days = 30; config.activity_check_days = 7; config.freeze_days = 90

ns2 = {
    "ikb": lambda rows: rows,          # 直接返回二维列表，便于断言
    "InlineKeyboardMarkup": list,      # 仅用于函数签名的类型注解
    "_open": _open, "moviepilot": moviepilot, "auto_update": auto_update,
    "fuxx_pitao": True, "red_envelope": red_envelope, "config": config,
}
exec(extract(f"{REPO}/bot/func_helper/fix_bottons.py", "config_preparation"), ns2)
rows = ns2["config_preparation"]()

flat = [cb for row in rows for _, cb in row]
check("键盘里没有 set_playback_rate_limit 回调", "set_playback_rate_limit" not in flat)
check("没有任何回调含 playback_rate", not any("playback_rate" in c for c in flat))

target = [r for r in rows if any(cb == "set_concurrent_play_limit" for _, cb in r)]
check("同时播放限制所在行存在且只有一行", len(target) == 1, f"找到 {len(target)} 行")
if target:
    row = target[0]
    cbs = [cb for _, cb in row]
    check("该行同时包含客户端过滤", "set_client_filter" in cbs, cbs)
    check("该行正好两个按钮", len(row) == 2, row)
    check("该行标签正确",
          row[0][0] == "✅ 同时播放限制" and row[1][0] == "📡 客户端过滤", row)
    check("客户端过滤不再单独占一行",
          sum(1 for r in rows if any(cb == "set_client_filter" for _, cb in r)) == 1)

check("键盘行数为 12", len(rows) == 12, f"实际 {len(rows)}")
check("返回按钮仍在最后一行", rows[-1] == [("🔙 返回", "manage")], rows[-1])

print()
print("  ── 面板实际布局 ──")
for i, r in enumerate(rows, 1):
    print(f"   {i:2d}. " + " | ".join(lbl for lbl, _ in r))

print()
print("=" * 72)
print(f"结果：PASS={PASS}  FAIL={FAIL}")
print("=" * 72)
sys.exit(0 if (FAIL == 0 and PASS > 0) else 1)
