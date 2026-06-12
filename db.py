import sqlite3
import os
import logging
import json
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
            # 스케줄 실행 결과 테이블
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS schedule_runs (
                    id TEXT PRIMARY KEY,
                    schedule_id TEXT,
                    schedule_name TEXT,
                    group_id TEXT,
                    group_name TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    status TEXT,
                    url_count INTEGER,
                    success_count INTEGER,
                    summary TEXT,
                    error TEXT
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

def save_schedule_run(run: dict):
    """스케줄 실행 결과를 DB에 저장합니다."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO schedule_runs
                (id, schedule_id, schedule_name, group_id, group_name, started_at, finished_at, status, url_count, success_count, summary, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run.get("id"),
                run.get("schedule_id"),
                run.get("schedule_name"),
                run.get("group_id"),
                run.get("group_name"),
                run.get("started_at"),
                run.get("finished_at"),
                run.get("status"),
                run.get("url_count", 0),
                run.get("success_count", 0),
                json.dumps(run.get("summary", []), ensure_ascii=False) if run.get("summary") else "[]",
                run.get("error")
            ))
            
            # 용량 관리: 각 schedule_id 별로 최신 2개만 유지하고 과거 이력 삭제 (500MB 서버 용량 제한 대응)
            schedule_id = run.get("schedule_id")
            if schedule_id:
                cursor.execute("""
                    DELETE FROM schedule_runs
                    WHERE schedule_id = ? AND id NOT IN (
                        SELECT id FROM schedule_runs
                        WHERE schedule_id = ?
                        ORDER BY started_at DESC
                        LIMIT 2
                    )
                """, (schedule_id, schedule_id))
            
            # 용량 관리: system_logs는 최신 10000개 유지 (per-URL 로그가 많아져서 1000→10000 상향)
            cursor.execute("""
                DELETE FROM system_logs
                WHERE id NOT IN (
                    SELECT id FROM system_logs
                    ORDER BY id DESC
                    LIMIT 10000
                )
            """)
            conn.commit()
    except Exception as e:
        log.error(f"Failed to save schedule run: {e}")

def get_schedule_run(run_id: str) -> dict:
    """run id로 단건 조회. 없으면 None."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM schedule_runs WHERE id = ?", (run_id,))
            row = cursor.fetchone()
            if not row:
                return None
            run = dict(row)
            try:
                run["summary"] = json.loads(run["summary"]) if run.get("summary") else []
            except Exception:
                run["summary"] = []
            return run
    except Exception as e:
        log.error(f"Failed to fetch schedule run {run_id}: {e}")
        return None

def get_recent_schedule_runs(schedule_id: str = None, limit: int = 20) -> list:
    """최근 스케줄 실행 결과를 조회합니다."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            if schedule_id:
                cursor.execute("SELECT * FROM schedule_runs WHERE schedule_id = ? ORDER BY started_at DESC LIMIT ?", (schedule_id, limit))
            else:
                cursor.execute("SELECT * FROM schedule_runs ORDER BY started_at DESC LIMIT ?", (limit,))
            rows = cursor.fetchall()
            runs = []
            for row in rows:
                run = dict(row)
                if run.get("summary"):
                    try:
                        run["summary"] = json.loads(run["summary"])
                    except Exception:
                        run["summary"] = []
                else:
                    run["summary"] = []
                runs.append(run)
            return runs
    except Exception as e:
        log.error(f"Failed to fetch schedule runs: {e}")
        return []

# 최초 모듈 로드 시 DB 초기화
init_db()
