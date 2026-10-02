from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from .config import settings

SQLALCHEMY_DATABASE_URL = (
    f"mysql+pymysql://{settings.mysql_user}:"
    f"{settings.mysql_password}@{settings.mysql_host}:"
    f"{settings.mysql_port}/{settings.mysql_db}"
)



def configure_lock_wait_timeout(target_engine, seconds: int) -> None:
    """MySQL 연결마다 행 잠금 대기 상한을 건다.

    InnoDB 기본값은 50초다. 이체가 잠금을 50초 기다리는 동안 API 스레드와 DB 연결이 묶이므로,
    짧게 기다리고 실패시킨 뒤(1205 Lock wait timeout) 이체 경로가 재시도하거나 503으로 돌려준다.
    """
    @event.listens_for(target_engine, "connect")
    def _set_timeout(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET SESSION innodb_lock_wait_timeout = {int(seconds)}")
        cursor.close()


engine = create_engine(SQLALCHEMY_DATABASE_URL, pool_pre_ping=True)
configure_lock_wait_timeout(engine, settings.mysql_lock_wait_timeout_seconds)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
