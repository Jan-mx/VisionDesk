from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_env: str = "development"
    api_port: int = 7302

    database_url: str = "postgresql+asyncpg://retouch:retouch_dev@localhost:7311/retouch"
    redis_url: str = "redis://localhost:7312"

    s3_endpoint: str = "http://localhost:7313"
    # 浏览器打开签名 URL 的地址；空则与 s3_endpoint 相同
    s3_public_endpoint: str = ""
    s3_access_key: str = "retouch"
    s3_secret_key: str = "retouch_dev"
    s3_bucket: str = "retouch"
    # 签名 URL 有效期，秒
    s3_url_ttl: int = 900

    # HS256 要求密钥不短于 32 字节
    jwt_secret: str = "dev-only-secret-please-change-in-production"
    jwt_ttl_hours: int = 24

    # image provider: mock | dashscope
    image_provider: str = "mock"
    dashscope_api_key: str = ""
    # 业务空间专属域名为 https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com
    dashscope_base_url: str = "https://dashscope.aliyuncs.com"
    text_to_image_model: str = "qwen-image-3.0-pro"
    image_edit_model: str = "qwen-image-2.0-pro"
    planner_model: str = "qwen3.8-flash"
    # 去背景与点选分割分开配置，避免 rembg 默认下载 BRIA 权重
    background_removal_provider: str = "bria"
    bria_api_token: str = ""
    bria_base_url: str = "https://engine.prod.bria-api.com/v2"
    bria_timeout_seconds: float = 180
    selection_provider: str = "sam"
    run_stale_seconds: int = 360
    # auto：有 rapidocr 则识别文字层；none 跳过；测试强制 none
    ocr_provider: str = "auto"

    # 账号永久清理开关（FR-027）：默认关闭；关闭时 deletion-preview 与 DELETE 均
    # 503 deletion_unavailable、零写入零审计，重试与操作详情不受开关限制
    admin_account_deletion_enabled: bool = False

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def frontend_dist(self) -> Path:
        return ROOT_DIR / "frontend" / "dist"


@lru_cache
def get_settings() -> Settings:
    return Settings()
