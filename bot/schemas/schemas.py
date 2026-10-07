import json
import os
from pydantic import BaseModel, Field
from typing import Dict, List, Optional, Union

# 嵌套式的数据设计，规范数据 config.json

MAX_INT_VALUE = 2147483647  # 2^31 - 1
MIN_INT_VALUE = -2147483648  # -2^31

class ExDate(BaseModel):
    mon: int = 30
    sea: int = 90
    half: int = 180
    year: int = 365
    used: int = 0
    unused: int = -1
    code: str = 'code'
    link: str = 'link'


# class UserBuy(BaseModel):
#     stat: StrictBool
#
#     # 转换 字符串为布尔
#     @field_validator('stat', mode='before')
#     def convert_to_bool(cls, v):
#         if isinstance(v, str):
#             return v.lower() == 'y'
#         return v
#
#     text: bool
#     button: List[str]


class Open(BaseModel):
    stat: bool
    open_us: int = 30
    all_user: int
    register_worker_count: int = 5
    # 等待队列长度：默认拉长到 300，让"排队"而不是"被拒"成为默认体验。
    # 注意队列还有第二道上限：不能超过剩余席位（all_user - tem - 占位），
    # 即不会超卖。见 register_queue.py 的 _max_waiting_queue_size_locked()。
    register_queue_limit: int = 300
    # ── 排队体验（bot/func_helper/register_queue.py）─────────────────────────
    # ETA 用最近 N 个已完成注册任务的真实耗时做滑动平均，样本不足时文案给"约"。
    register_queue_eta_window: int = 10             # 滑动平均窗口(个)
    register_queue_eta_min_samples: int = 3         # 少于该样本数时不报精确 ETA
    register_queue_warn_after_seconds: int = 120    # 等待超过该秒数补一条"仍在排队"提示
    # ── 安全批量建号限流闸门（bot/func_helper/register_throttle.py）──────────
    # 全部可选，缺失即用默认值。**必须在这里声明**：Open 没开 extra="allow"，
    # pydantic v2 默认 extra="ignore"，未声明的键不但读不到，下次 save_config()
    # 还会把它从 config.json 里静默抹掉。
    #
    # 默认值的取值思路（"开号又快 + 不会 OOM"）：
    #   并发压到 1、请求之间留 400ms —— 建 1 个号 3 次请求 ≈ 1.2s，约 40~50 个/分钟；
    #   分片 25 个 + 批间隔 10s —— 每号额外摊 0.4s，Emby 每 10 秒能喘一口气；
    #   Emby 单次请求健康时只要 5~30ms，这个节奏对它几乎没有压力。
    register_throttle_enabled: bool = True          # 总开关
    register_batch_concurrency: int = 1             # 建号/批量 lane 并发上限
    register_interactive_concurrency: int = 6       # 交互/巡检 lane 并发上限
    register_max_workers: int = 2                   # register_worker_count 的上限
    register_min_interval_ms: int = 400             # 批量 lane 请求最小间隔(ms)
    register_interactive_min_interval_ms: int = 0   # 交互 lane 最小间隔(ms)
    register_shard_size: int = 25                   # 每批账号数
    register_batch_gap: float = 10.0                # 批间隔(秒)
    register_breaker_failures: int = 5              # 连续失败熔断阈值
    register_breaker_cooldown: float = 60.0         # 熔断暂停(秒)
    register_request_timeout: float = 10.0          # 单请求超时(秒)，与原实现一致
    register_retries: int = 2                       # 幂等请求重试次数(含首次)，原为 3
    register_probe_timeout: float = 3.0             # 熔断期快速探测超时(秒)
    register_alert_owner: bool = True               # 熔断时私聊 owner 告警
    register_virtualfolders_ttl: float = 300.0      # 媒体库列表缓存 TTL(秒)
    register_item_retries: int = 1                  # 批量单项失败后的指数退避重试次数
    register_abort_on_breaker: bool = False         # 熔断时直接中止(而非等待冷却)
    # GET /emby/Sessions 的 ActiveWithinSeconds：线上不带该参数要传 2.97MB/4037 条
    # 僵尸 session（真正在播只有 7~8 条），带 300 只要 90KB/75 条。0=不加参数。
    register_session_active_seconds: int = 300
    timing: int = 0
    tem: Optional[int] = 0
    # allow_code: StrictBool
    # @field_validator('allow_code', mode='before')
    # def convert_to_bool(cls, v):
    #     if isinstance(v, str):
    #         return v.lower() == 'y'
    #     return v

    checkin: bool
    checkin_lv: Optional[str] = 'd'
    exchange: bool
    whitelist: bool
    use_whitelist_code: bool = False
    invite: bool
    invite_lv: Optional[str] = 'b'
    leave_ban: bool
    uplays: bool = True
    checkin_reward: Optional[List[int]] = [1, 10]
    exchange_cost: int = 300
    whitelist_cost: int = 9999
    invite_cost: int = 1000
    srank_cost: int = 5

    # 每次创建 Open 对象时被重置为 0
    def __init__(self, **data):
        super().__init__(**data)
        self.timing = 0


class Ranks(BaseModel):
    logo: str = "SAKURA"
    backdrop: bool = False


class Schedall(BaseModel):
    dayrank: bool = True
    weekrank: bool = True
    dayplayrank: bool = False
    weekplayrank: bool = True
    check_ex: bool = True
    low_activity: bool = False
    partition_check: bool = True
    day_ranks_message_id: int = 0
    week_ranks_message_id: int = 0
    restart_chat_id: int = 0
    restart_msg_id: int = 0
    backup_db: bool = True

    def __init__(self, **data):
        super().__init__(**data)
        if self.day_ranks_message_id == 0 or self.week_ranks_message_id == 0:
            if os.path.exists("log/rank.json"):
                with open("log/rank.json", "r") as f:
                    i = json.load(f)
                    self.day_ranks_message_id = i.get("day_ranks_message_id", 0)
                    self.week_ranks_message_id = i.get("week_ranks_message_id", 0)


class Proxy(BaseModel):
    scheme: Optional[str] = ""  # "socks4", "socks5" and "http" are supported
    hostname: Optional[str] = ""
    port: Optional[int] = None
    username: Optional[str] = ""
    password: Optional[str] = ""


class MP(BaseModel):
    status: bool = False
    url: Optional[str] = ""
    username: Optional[str] = ""
    password: Optional[str] = ""
    access_token: Optional[str] = ""
    price: int = 1
    download_log_chatid: Optional[int] = None
    lv: Optional[str] = "b"

class AutoUpdate(BaseModel):
    # 默认关闭：开启后每天 12:30 会自动 `git pull` + `pip install` + 重启进程。
    # 容器部署下 .git 已被 .dockerignore 排除，自动更新必然失败并在日志里刷错误；
    # 非容器部署下这也是一条无人值守的远程代码执行路径，因此改为显式开启。
    status: bool = False
    git_repo: Optional[str] = "berry8838/Sakura_embyboss"  # github仓库名/魔改的请填自己的仓库
    commit_sha: Optional[str] = None  # 最近一次commit
    up_description: Optional[str] = None  # 更新描述


class API(BaseModel):
    status: bool = False  # 默认关闭
    # 默认只监听本机回环：反代（nginx/caddy）与 bot 同机时仍可正常访问，
    # 但避免 API 被直接暴露到公网。确需外部直连时再显式改为 "0.0.0.0"。
    http_url: Optional[str] = "127.0.0.1"
    http_port: Optional[int] = 8838
    # 允许的跨域来源白名单。未设置 = 不放行任何跨域请求（同源访问不受影响）。
    allow_origins: Optional[List[Union[str, int]]] = None
    # 内部端点令牌：反代访问 /emby/ban_playlist、/emby/line_report 时
    # 必须通过 X-Internal-Token 请求头携带该值。
    # 留空则仅允许回环地址（127.0.0.1 / ::1）访问内部端点。
    internal_token: Optional[str] = None
    # 是否开放 /docs、/redoc、/openapi.json。默认关闭，避免无鉴权泄露端点清单。
    expose_docs: bool = False

    def __init__(self, **data):
        super().__init__(**data)
        if self.allow_origins is None:
            self.allow_origins = []
            # 未设置时不放行任何跨域来源；
            # 若前端与 API 不同源，请在此列出其域名（不要使用 "*"）。
class RedEnvelope(BaseModel):
    status: bool = True  # 是否开启红包
    allow_private: bool = True # 是否允许专属红包

class Config(BaseModel):
    bot_name: str
    bot_token: str
    owner_api: int
    owner_hash: str
    owner: int
    group: List[int]
    main_group: str
    chanel: str
    bot_photo: str
    open: Open
    admins: Optional[List[int]] = []
    money: str
    emby_api: str
    emby_url: str
    emby_block: Optional[List[str]] = []
    emby_line: str
    extra_emby_libs: Optional[List[str]] = []
    db_host: str
    db_user: str
    db_pwd: str
    db_name: str
    db_port: int = 3306
    tz_ad: Optional[str] = None
    tz_api: Optional[str] = None
    tz_id: Optional[List[Union[int, str]]] = []  # int for Nezha, str (UUID) for Komari
    tz_version: Optional[str] = "v0"  # "v0" for Nezha V0, "v1" for Nezha V1, "komari" for Komari
    tz_username: Optional[str] = None  # V1 API only
    tz_password: Optional[str] = None  # V1 API only
    ranks: Ranks
    schedall: Schedall
    db_is_docker: bool = False
    db_docker_name: str = "mysql"
    db_backup_dir: str = "./db_backup"
    db_backup_maxcount: int = 7
    # another_line: Optional[List[str]] = []
    # 如果使用的是 Python 3.10+ ，|运算符能用
    # w_anti_channel_ids: Optional[List[str | int]] = []
    w_anti_channel_ids: Optional[List[Union[str, int]]] = []
    proxy: Optional[Proxy] = Proxy()
    # kk指令中赠送资格的天数
    kk_gift_days: int = 30
    # 是否狙杀皮套人
    fuxx_pitao: bool = True
    # 活跃检测天数，默认21天
    activity_check_days: int = 21
    # 封存账号天数，默认5天
    freeze_days: int = 5
    # 白名单用户专属的emby线路
    emby_whitelist_line: Optional[str] = None
    # 客户端过滤总开关
    client_filter_enabled: bool = False
    # 被拦截的user-agent模式列表
    blocked_clients: Optional[List[str]] = None
    # 客户端过滤模式：blacklist=命中黑名单拦截，whitelist=未命中白名单拦截
    client_filter_mode: str = "blacklist"
    # 被允许的user-agent模式列表，仅client_filter_mode为whitelist时生效
    allowed_clients: Optional[List[str]] = None
    # 是否在检测到可疑客户端时终止会话
    client_filter_terminate_session: bool = True
    # 是否在检测到可疑客户端时封禁用户
    client_filter_block_user: bool = False
    # 是否在检测到线路权限违规时终止会话
    line_filter_terminate_session: bool = True
    # 是否在检测到线路权限违规时封禁用户
    line_filter_block_user: bool = False
    # 分区名 -> 库名列表
    partition_libs: Dict[str, List[str]] = Field(default_factory=dict)
    # 同时播放限制检测
    concurrent_play_limit_enabled: bool = False
    concurrent_play_limit: int = 2
    concurrent_play_warn_threshold: int = 3
    concurrent_play_check_interval: int = 60
    # 白名单（lv='a'）是否也纳入并发限制。bot 管理员**始终**豁免，不受此项影响。
    concurrent_play_limit_whitelist_enabled: bool = False
    # 白名单用户适用的并发上限，仅在上项为 True 时生效
    concurrent_play_limit_whitelist: int = 4
    moviepilot: MP = Field(default_factory=MP)
    auto_update: AutoUpdate = Field(default_factory=AutoUpdate)
    red_envelope: RedEnvelope = Field(default_factory=RedEnvelope)
    api: API = Field(default_factory=API)

    def __init__(self, **data):
        super().__init__(**data)
        if self.owner in self.admins:
            self.admins.remove(self.owner)

    @classmethod
    def load_config(cls):
        with open("config.json", "r", encoding="utf-8") as f:
            config = json.load(f)
            return cls(**config)

    def save_config(self):
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(self.model_dump(), f, indent=4, ensure_ascii=False)


class Yulv(BaseModel):
    wh_msg: List[str]
    red_bag: List[str]

    @classmethod
    def load_yulv(cls):
        with open("bot/func_helper/yvlu.json", "r", encoding="utf-8") as f:
            yulv = json.load(f)
            return cls(**yulv)
