import warnings

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_JWT_SECRET = "changeme-in-production-use-strong-random-key"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    api_host: str = "0.0.0.0"
    api_port: int = 8000

    mysql_host: str = "mysql"
    mysql_port: int = 3306
    mysql_user: str = "finops_user"
    mysql_password: str = "finops_pass"
    mysql_db: str = "finops"

    jwt_secret_key: str = _DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # ML 앙상블은 명시적으로 켠다 (FDS_ML_ENABLED=true). 예전에는 모델 파일이 있으면
    # 자동으로 켜져서, PaySim(USD)으로 학습한 모델이 원화 거래를 채점하는 일이 생길 수 있었다.
    # 학습 데이터와 운영 거래의 통화·분포가 같은지는 켜는 사람이 확인해야 한다.
    fds_ml_enabled: bool = False

    @field_validator("jwt_secret_key")
    @classmethod
    def warn_default_jwt_secret(cls, v: str) -> str:
        if v == _DEFAULT_JWT_SECRET:
            warnings.warn(
                "JWT_SECRET_KEY가 기본값입니다. 프로덕션 환경에서는 반드시 강력한 랜덤 키로 변경하세요.",
                UserWarning,
                stacklevel=2,
            )
        return v


settings = Settings()
