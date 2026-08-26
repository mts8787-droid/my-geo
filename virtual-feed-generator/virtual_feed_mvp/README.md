# UK Virtual Feed Discovery MVP

PDP URL 하나를 입력해 Virtual Feed 초안과 근거를 만들고 Excel로 내려받는 로컬 테스트 페이지다. 여러 SKU와 PIM/CMS 입력은 배치 기능으로 보존되어 있다.

## 현재 가능한 것

- LG UK PDP URL 하나를 붙여넣어 즉시 생성
- 원본 SKU를 함께 입력하면 `원본SKU_1`, `원본SKU_2` 형식의 Virtual SKU 생성
- 여러 URL은 한 줄에 하나씩 또는 쉼표로 구분해 배치 입력
- 이미지와 같은 `REF / W/M / LTV / MNT` 40개 SKU 표 붙여넣기
- 일반 CSV 또는 XLSX 입력
- PDP URL에서 Key Features 자동 추출
- PIM/CMS export의 `key_feature_1`, `key_feature_2` 등 구조화 필드 입력
- 전체 표를 넣은 뒤 `REF / W/M / LTV / MNT`를 선택해 제품군별 10개씩 실행
- SKU별 최대 6개 Feed 생성하되, 근거가 부족하면 더 적게 생성
- SKU 1개당 한 요청, 여러 SKU는 최대 8개 병렬 처리
- `Feeds / Evidence / Issues / Run Summary` Excel 생성
- 결과마다 사용한 Claim ID와 글자 수 표시

## 중요한 현재 제한

- SKU 문자열만으로 제품 주장을 생성하지 않는다. SKU만 입력한 행은 `SOURCE_REQUIRED`로 표시된다.
- LG PDP URL을 SKU에서 자동 검색하는 기능은 아직 연결하지 않았다. 1차 파일럿은 정확한 PDP URL을 함께 제공하는 방식을 권장한다.
- PDP 파서는 현재 LG UK 페이지의 `Key Features`를 중심으로 한다. Feature 본문, Key Specs, 각주 교차 검증은 다음 단계다.
- API 키가 없으면 Key Feature 원문을 축약한 규칙 기반 초안만 생성하며 모두 `Needs Review`다.
- `Supported`는 생성 형식과 Evidence ID가 연결됐다는 의미다. 제품 담당자의 최종 광고 승인 상태를 뜻하지 않는다.

## 실행

PowerShell에서 다음 파일을 실행한다.

```powershell
.\virtual_feed_mvp\run_mvp.ps1
```

그다음 브라우저에서 `http://127.0.0.1:8765`를 연다.

일반 Python을 사용할 경우 먼저 의존성을 설치한다.

```powershell
python -m pip install -r .\virtual_feed_mvp\requirements.txt
python .\virtual_feed_mvp\app.py
```

## AI 생성 선택 설정

API 키는 파일이나 화면에 저장하지 않고 실행 환경에만 둔다.

```powershell
$env:OPENAI_API_KEY='YOUR_KEY'
$env:OPENAI_MODEL='gpt-5.6-luna'
.\virtual_feed_mvp\run_mvp.ps1
```

`OPENAI_MODEL`은 변경 가능하다. 모델 선택은 동일한 10~20개 SKU 평가 세트로 품질·비용·처리 시간을 비교한 후 확정한다.

## 입력 형식

### 0. 단일 URL

기본 화면에서 국가와 모델명만 입력할 수 있다. UK에서는 공식 사이트맵에서 PDP를 찾은 뒤 PDP 내장 상품 JSON의 전체 `sku`를 읽어 `GSXV91MCAE.AMCQLGU.EEUK.UK.C_1`처럼 번호를 붙인다. PDP URL, 모델+suffix, 전체 Product ID 입력도 같은 칸에서 허용한다. 조회 결과가 없거나 여러 후보가 동률이면 임의 선택하지 않고 Issue로 남긴다.

여러 URL을 시험할 때는 접힌 배치 메뉴에 한 줄에 하나씩 입력하는 방식을 권장한다. 쉼표 구분도 지원한다. URL 10개를 넣어도 동시 처리 수는 4 정도로 시작하고, 나머지는 자동으로 이어서 처리한다.

### 1. 이미지와 같은 40개 SKU 표

`sample_uk_40.csv` 형식을 사용한다. 이 형식은 SKU 목록 확인용이며 URL이나 PIM 기능이 없으면 Feed를 생성하지 않는다.

### 2. PDP URL 입력

필수 권장 열:

- `request_id`
- `sku`
- `category`
- `country`
- `language`
- `pdp url`

예시는 `sample_pdp_url.csv`에 있다.

### 3. PIM/CMS export 입력

필수 권장 열:

- `sku`
- `category`
- `country`
- `language`
- `source_record_id`
- `product_name`
- `key_feature_1` ... `key_feature_6`

여러 기능을 한 필드에 넣을 때는 `key_features` 열에서 `|`로 구분할 수 있다. 예시는 `sample_pim_export.csv`에 있다.

## 40개 파일럿 권장 순서

1. `sample_pdp_url.csv`의 단일 PDP로 수집과 Excel 출력을 확인한다.
2. 제품군마다 2개씩 총 8개 URL로 파서 실패 유형을 확인한다.
3. 40개 정확한 UK PDP URL을 입력하되 `REF → W/M → LTV → MNT` 순서로 10개씩 실행한다.
4. 각 제품군 실행 후 `Issues`의 실패와 `Evidence`의 추출 정확도를 사람이 검토한다.
5. 앞 제품군의 오류를 수정한 뒤 다음 제품군으로 넘어간다.
6. 같은 40개에 PIM/CMS export를 연결해 PDP-only와 PIM+PDP 결과를 비교한다.

## PIM/CMS 확장 원칙

PDP, PIM, CMS는 각각 별도 입력 변환기를 가지지만 다음 공통 Evidence 필드로 합쳐진다.

- SKU, 국가, 언어, 제품군
- Source type, Source record ID, Source section/field
- Claim ID, 원문, Intent 후보
- 적용 범위, 제한 조건, 검증 상태

Feed 생성기는 이 공통 Evidence만 사용한다. 따라서 PIM API가 연결되어도 생성 및 Excel 출력 부분은 교체하지 않는다.


## v0.4.0: SKU 자동 PDP 탐색 보강

모델명만 입력하면 먼저 국가별 LG XML 사이트맵에서 공식 PDP를 찾는다. LG.com이 일반 HTTP 요청에 401/403/429를 반환하는 경우에는 Windows에 기본 설치된 Microsoft Edge(또는 Chrome)를 headless 모드로 사용해 다음 순서로 재시도한다.

1. 검색엔진에서 `site:lg.com/{country} {model}` 후보 수집
2. 공식 LG 도메인, 국가 경로, 모델명 일치 여부 검증
3. Support/Business/Promotion 경로 제외
4. 제품군 경로와 정확한 SKU slug를 우선해 PDP 선택
5. PDP HTML도 403이면 브라우저 렌더링 DOM으로 재수집

별도 브라우저 패키지 설치는 필요 없다. Edge/Chrome을 자동 탐지하지 못하면 환경변수 `VF_BROWSER_PATH`에 실행 파일 경로를 지정하거나 PDP URL을 직접 입력한다. 검색 결과가 없거나 신뢰할 수 있는 후보가 없으면 임의 PDP를 선택하지 않고 Issue로 기록한다.


## v0.5.1: Title Label Mapping

Title은 `LG {Product Category}: {Title Intent Label}` 규칙을 사용한다. 내부 분류명과 고객 노출용 Title 표현은 `intent_title_mapping.csv`에서 분리 관리한다. 이 CSV는 Excel에서 열고 수정할 수 있으며 앱 재시작 후 변경값이 적용된다.

관리 열:

- `category`: 내부 제품군 코드 (`REF`, `W/M`, `LTV`, `MNT`)
- `category_label`: Title에 노출할 제품군명
- `internal_intent`: 내부 분류 로직의 Intent명 — 코드와 정확히 일치해야 함
- `title_intent_label`: Title에 사용할 짧은 표현
- `enabled`: `Y`이면 사용
- `notes`: 운영 메모

Delivery Feed Excel은 기존 6열 구조를 유지하며 Mapping 표를 섞지 않는다. Mapping CSV는 별도 운영 설정 파일로 Git에서 변경 이력을 관리한다.


## v0.5.2: 실시간 Taxonomy Excel 연결

`feed_taxonomy_config.xlsx`를 앱 폴더에 두면 Feed 생성 버튼을 누를 때마다 저장된 최신 값을 다시 읽는다. Excel에서 값을 수정한 뒤 저장하면 서버를 재시작하지 않아도 다음 생성 건부터 반영된다. 운영 파일을 SharePoint/OneDrive 동기화 폴더 등 다른 위치에 둘 경우 `VF_TAXONOMY_CONFIG` 환경변수에 전체 파일 경로를 지정할 수 있다.

- `Market_Category`: 국가별 제품군 노출명, 축약명, 활성화 여부 및 승인 상태
- `Intent_Label`: 국가/제품군별 내부 Intent와 고객 노출용 표현
- `Column_Guide`: 각 열의 역할과 편집 규칙
- `GNB_Discovery`: 향후 GNB 자동 탐색 결과를 검토용으로 적재할 영역

우선순위는 `정확한 국가+제품군 Override` → `글로벌(*) 기본값`이며, 같은 구체성에서는 priority 숫자가 낮은 행이 우선한다. `enabled=N` 또는 `validation_status=Rejected` 행은 사용하지 않는다.
