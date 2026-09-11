"""应用配置：从环境变量 / .env 读取。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置。敏感信息通过 .env 注入，不写入代码或 git。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "二手交易平台分析系统"
    debug: bool = False

    # ---- 日志与观测 ----
    # 日志级别（DEBUG/INFO/WARNING/ERROR），控制根 logger
    log_level: str = "INFO"
    # 是否输出 JSON 行日志（便于采集/检索）；false 时输出人类可读单行文本
    log_json: bool = False
    # 访问日志慢请求阈值（毫秒）：超过则提升为 WARNING，便于快速定位性能问题
    slow_request_ms: int = 1000

    # 数据库连接（本地 Docker PostgreSQL，端口 5433 避开宿主机 5432）。
    # 无默认账号密码：必须经 .env 的 DATABASE_URL 注入，避免无 .env 部署时
    # 误用弱凭证（见 .env.example）。缺失时启动报错，强制显式配置。
    database_url: str

    # Redis（RAG 检索缓存）。默认本地 Docker Redis 端口 6380 避开宿主机 6379。
    redis_url: str = "redis://localhost:6380/0"
    # 检索缓存 TTL（秒），默认 1 小时
    rag_cache_ttl: int = 3600
    # 检索缓存总开关：测试与故障排查时可置 false 完全关闭（不建连、不走缓存）
    rag_cache_enabled: bool = True

    # LLM 配置（OpenAI 兼容接口，通过 .env 注入）
    llm_api_base: str = "https://www.rightapi.ai/codex/v1"
    llm_model: str = "gpt-5.6-luna"
    llm_api_key: str = ""

    # ---- JWT 认证 ----
    # 签名密钥：必须经 .env 的 JWT_SECRET 注入，缺失时启动报错，避免弱默认密钥
    jwt_secret: str
    # access token 有效期（分钟），默认 24h
    jwt_expire_minutes: int = 60 * 24

    # ---- 内置管理员 ----
    # 首次启动自动 seed 的管理员账号（仅当 users 表为空时创建）
    admin_username: str = "admin"
    admin_password: str = "123456"

    # ---- 登录防爆破 ----
    # 同一用户名连续失败达到该次数即锁定（默认 5 次）
    login_max_failures: int = 5
    # 锁定时长（分钟），到期自动解锁；期间即使密码正确也拒绝
    login_lockout_minutes: int = 15

    # ---- 定时监控 ----
    # 监控任务总开关。默认关闭：无人值守抓取会产生真实外网请求与 LLM 费用，
    # 必须显式打开（.env 中设 MONITOR_ENABLED=true）后才运行。
    monitor_enabled: bool = False
    # 监控轮询间隔（小时）
    monitor_interval_hours: int = 6
    # 单轮监控最多处理的竞品数（防止一次抓取过多，控制外网压力与成本）
    monitor_max_per_cycle: int = 20

    # ---- LLM 成本熔断 ----
    # 会话累计消费超过该阈值（元）即中断并征询。默认 1 元；按公开单价单次抓取约 0.01 元，
    # 手动试用难以触发，可下调（如 0.05）让批量/试用也能触发。
    llm_cost_limit_cny: float = 1.0
    # 单价：每百万 token 美元。注意 rightapi 网关会把 gpt-5.6-luna 实际路由到 gpt-5.6-terra
    # （实测请求 luna 返回 model=terra），故按 terra 公开价（输入 2.0 / 输出 12.0）保守估算，
    # 避免成本熔断低估。以实际账单为准。
    llm_input_price_usd_per_mtok: float = 2.0
    llm_output_price_usd_per_mtok: float = 12.0
    # 汇率（美元→人民币），仅用于费用预估展示
    usd_cny_rate: float = 7.1


settings = Settings()
