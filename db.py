import sqlite3
import os
import logging
from datetime import datetime

log = logging.getLogger("geo_audit.db")

# Render 등에서 사용할 영구 디스크 경로. 기본값은 ./data
DATA_DIR = os.environ.get("DATA_DIR", "data")
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "audit.db")

def get_connection():
    """SQLite DB 연결을 반환합니다."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """데이터베이스 테이블 초기화."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            # 시스템 로그 테이블 (메모리 로거 대체용)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS system_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    message TEXT NOT NULL
                )
            """)
            conn.commit()
            log.info("Database initialized successfully.")
    except Exception as e:
        log.error(f"Database initialization failed: {e}")

def add_system_log(message: str):
    """새로운 시스템 로그를 기록합니다."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO system_logs (timestamp, message) VALUES (?, ?)", (timestamp, message))
            conn.commit()
    except Exception as e:
        log.error(f"Failed to save system log: {e}")

def get_recent_system_logs(limit: int = 100) -> list:
    """최근 시스템 로그를 조회합니다."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            # 최신순으로 가져와서 원래 순서대로 출력하기 위해 서브쿼리 사용
            cursor.execute("""
                SELECT timestamp, message FROM (
                    SELECT * FROM system_logs ORDER BY id DESC LIMIT ?
                ) ORDER BY id ASC
            """, (limit,))
            rows = cursor.fetchall()
            return [f"[{row['timestamp']}] {row['message']}" for row in rows]
    except Exception as e:
        log.error(f"Failed to fetch system logs: {e}")
        return []

# 최초 모듈 로드 시 DB 초기화
init_db()
