#!/usr/bin/env python3
"""PreToolUse 훅: 로컬 서버/브라우저 설치 명령 자동 실행 차단.

stdin으로 들어오는 Claude Code 훅 페이로드를 읽어, Bash 명령이 차단 패턴에
매칭되면 exit 2로 거부한다 (stderr 메시지가 어시스턴트에게 전달됨).

CLAUDE.md:255 + project memory `feedback_no_local_testing.md` 의 권고를
강제 차단으로 승격.
"""
import json
import re
import sys

BLOCKED = [
    (r'\buvicorn\b', 'uvicorn'),
    (r'\bgunicorn\b', 'gunicorn'),
    (r'\bfastapi\s+run\b', 'fastapi run'),
    (r'\bflask\s+run\b', 'flask run'),
    (r'\b(npm|pnpm|yarn)\s+(run\s+)?(dev|start|serve)\b', 'npm/pnpm/yarn dev/start/serve'),
    (r'\bplaywright\s+install\b', 'playwright install'),
    (r'\bmanage\.py\s+runserver\b', 'django runserver'),
    (r'\bpython3?\s+-m\s+http\.server\b', 'python http.server'),
    (r'\bcaddy\s+(run|start)\b', 'caddy run'),
    (r'\bnginx\b', 'nginx'),
]


def _sanitize(cmd: str) -> str:
    """heredoc 본문과 따옴표 안 문자열을 제거 — 데이터에 포함된 키워드 false-positive 방지.

    예) git commit -m "$(cat <<'EOF' ... uvicorn ... EOF)" 가 차단되지 않도록.
    """
    # heredoc: <<EOF, <<'EOF', <<-EOF 등 (마커 사이 본문 제거)
    cmd = re.sub(
        r"<<-?\s*['\"]?(\w+)['\"]?\s*\n.*?\n\s*\1\s*\n?",
        "",
        cmd,
        flags=re.DOTALL,
    )
    # 큰따옴표 안 (escape는 정확히 처리 안 함 — 충분히 보수적)
    cmd = re.sub(r'"[^"]*"', "", cmd)
    # 작은따옴표 안
    cmd = re.sub(r"'[^']*'", "", cmd)
    return cmd


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # 페이로드 파싱 실패 시는 통과 (안전 측면)

    raw_cmd = (data.get("tool_input") or {}).get("command", "")
    if not raw_cmd:
        return 0
    scan_target = _sanitize(raw_cmd)

    for pattern, label in BLOCKED:
        if re.search(pattern, scan_target):
            sys.stderr.write(
                f"BLOCKED by .claude/hooks/block-local-server.py — '{label}' 패턴 감지.\n"
                f"  사용자가 직접 '! <command>'로 띄워야 합니다 (project CLAUDE.md:255).\n"
                f"  matched cmd (앞 200자): {raw_cmd[:200]}\n"
            )
            return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
