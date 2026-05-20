# GEO/AEO Audit System - 기능 정의서 (Agent Functional Specification)

## 1. 개요
본 문서는 GEO(Generative Engine Optimization) 및 AEO(Answer Engine Optimization) 분석을 위한 자동화 감사(Audit) 시스템의 주요 에이전트와 모듈별 기능을 정의합니다. 이 시스템은 대규모 URL에 대한 백그라운드 일괄 분석, 사이트맵 자동 추적, 핵심 SEO/AEO 지표 수집 및 알림 기능을 통합적으로 수행합니다.

---

## 2. 주요 에이전트(Agent) 및 핵심 모듈 정의

### 2.1. Batch Audit Agent (`batch_audit.py`)
- **기능**: 대량의 대상 URL 목록(CSV 등)을 입력받아 일괄적으로 감사를 수행하는 메인 처리 엔진입니다.
- **주요 역할**:
  - `concurrency` (동시성) 매개변수를 기반으로 병렬 처리를 수행하여 분석 속도 최적화.
  - 처리 중 발생한 분석 결과를 `ndjson` 형태로 실시간 기록.
  - 메모리 사용량 최적화 및 중단 시 재개 기능(복원력) 지원.

### 2.2. Sitemap Audit Agent (`sitemap_agent.py`, `lg_sitemap_collector.py`)
- **기능**: 특정 도메인의 사이트맵(Sitemap.xml)을 크롤링하여 등록된 모든 URL을 수집하고 자동 점검을 수행하는 서버사이드 에이전트.
- **주요 역할**:
  - 정기적인 사이트맵 파싱 및 새로운 URL 감지.
  - 백그라운드 작업 환경에서 크롤링 봇 차단(WAF 등) 우회 및 점검 데이터 확보.
  - 작업 완료 시 이메일이나 서버 알림을 통해 분석 결과 리포트(CSV 등) 발송.

### 2.3. Core Analyzer & Rule Engine (`analyzer.py`, `rule_engine.py`)
- **기능**: URL의 HTML 렌더링 결과, 메타데이터, 텍스트를 추출하여 GEO/AEO 최적화 적합성을 평가하는 핵심 논리 모듈.
- **주요 역할**:
  - Semantic Matching 및 AI 모델 학습 적합성 분석.
  - `llms.txt`, `robots.txt` 등 도메인 수준의 AI 봇 접근성 검증 (불필요한 중복 네트워크 체크 방지).
  - 사전 정의된 룰(`scoring_config.json`)을 기반으로 항목별 점수(Score) 산출.

### 2.4. Progress Monitor (`monitor_progress.ps1`)
- **기능**: 백그라운드로 도는 대규모 분석 작업의 진행 상황을 모니터링하는 PowerShell 기반 워치독 스크립트.
- **주요 역할**:
  - 결과 파일(`.ndjson`)의 라인 수를 실시간으로 계산하여 전체 타겟 URL 대비 진행률(%) 산출.
  - 일정 주기 또는 퍼센트(%) 도달 시 Windows 시스템 팝업을 통해 사용자에게 진행 상황 알림 (예: 10% 단위 알림, 100% 완료 알림).

### 2.5. Job Schedulers & App Core (`main.py`, `scheduler.py`)
- **기능**: FastAPI 프레임워크 기반의 웹 대시보드 백엔드 및 정기 스케줄링 에이전트.
- **주요 역할**:
  - 외부 관리자(Admin) 요청을 처리하기 위한 REST API 엔드포인트 제공.
  - `scheduler.py`를 통한 정기 분석 스케줄 등록 및 비동기 작업 실행.

### 2.6. Data Store & BigQuery 연동 (`audit_store.py`, `db.py`)
- **기능**: 점검 상태 및 결과 데이터를 로컬 DB 및 클라우드로 이관하는 영구 스토리지 관리 에이전트.
- **주요 역할**:
  - 로컬 SQLite를 이용한 시스템 로그, 작업 기록(Run histories)의 안전한 보관.
  - 최종 완료된 일괄 분석 데이터를 `bq_schema.json`에 맞추어 Google BigQuery로 업로드하기 위한 파이프라인.

### 2.7. Batch Runners (`run_uk.bat`, `run_jp.bat`, `run_batch.bat`)
- **기능**: 각 마켓(UK, JP 등)의 CSV 목록을 기반으로 배치 작업을 즉시 구동하기 위한 엔트리 포인트 스크립트.
- **주요 역할**:
  - 백그라운드 환경에서 `python batch_audit.py` 구동 명령어 래핑 및 초기 변수(Concurrency 등) 세팅.
  - 국가별 특성에 맞춘 분석 스크립트 구동 보조.

---

## 3. 요약 및 구동 흐름 (Workflow)
1. 사용자가 `run_uk.bat` 등 배치 스크립트 실행 (Background 구동)
2. `batch_audit.py`가 동시성을 가지고 대상 URL을 `analyzer.py`에 넘겨 분석 요청.
3. 동시에 `monitor_progress.ps1`이 실행되어 실시간으로 결과 로그(`/results`)를 읽고 화면에 진행 상황 팝업 리포트 제공.
4. 분석된 모든 결과는 `audit_store.py`를 거쳐 NDJSON 등으로 기록되고, 최종적으로 클라우드(BigQuery 등)로 이관.
