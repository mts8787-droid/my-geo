# Virtual Feed Generator

PDP Evidence와 Taxonomy Excel 규칙을 기반으로 Virtual Feed를 생성하는 Python 웹 애플리케이션입니다.

## Local run

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:VF_PORT="8766"
python -m virtual_feed_mvp.app
```

브라우저: `http://127.0.0.1:8766`

## Test

```bash
python -m unittest discover -s virtual_feed_mvp/tests -q
```

## Render

- Build command: `pip install -r requirements.txt`
- Start command: `python -m virtual_feed_mvp.app`
- Health check: `/api/config`
- Render가 제공하는 `PORT` 환경변수를 자동 사용합니다.

## Environment variables

- `PORT`: 호스팅 플랫폼 포트
- `VF_PORT`: 로컬 포트
- `VF_HOST`: 기본값 `0.0.0.0`
- `VF_TAXONOMY_CONFIG`: Taxonomy Excel 경로
- `OPENAI_API_KEY`: 선택 사항. 없으면 Rule 기반 실행
- `OPENAI_MODEL`: 선택 사항

## Repository policy

- Repository는 Private으로 운영합니다.
- `.env`, API Key, 업로드 파일, 생성 결과 파일은 Commit하지 않습니다.
- `main` 브랜치는 배포 가능한 버전만 유지합니다.
