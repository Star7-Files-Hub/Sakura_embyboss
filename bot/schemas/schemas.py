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
    register_queue_limit: int = 100
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
