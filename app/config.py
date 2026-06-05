from pydantic import BaseSettings


class Settings(BaseSettings):
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    mysql_host: str = "mysql"
    mysql_port: int = 3306
    mysql_user: str = "finops_user"
    mysql_password: str = "finops_pass"
    mysql_db: str = "finops"

    jwt_secret_key: str = "changeme-in-production-use-strong-random-key"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        fields = {
            "mysql_db": {"env": "MYSQL_DATABASE"},
            "jwt_secret_key": {"env": "JWT_SECRET_KEY"},
        }


settings = Settings()
