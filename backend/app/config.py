"""应用配置：从环境变量 / .env 读取。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置。敏感信息通过 .env 注入，不写入代码或 git。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "古玩·钱币竞品分析系统"
    debug: bool = True

    # 数据库连接（本地 Docker PostgreSQL，端口 5433 避开宿主机 5432）
    database_url: str = (
        "postgresql+asyncpg://competitor:competitor@localhost:5433/competitor"
    )


settings = Settings()
