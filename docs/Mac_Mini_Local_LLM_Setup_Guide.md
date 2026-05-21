# Mac Mini (M4) Local LLM 구축 및 연동 가이드

본 문서는 M4 칩셋이 탑재된 맥 미니(기본형 16GB RAM) 환경에서 **Ollama**를 활용하여 로컬 LLM 서버를 구축하고, 이를 현재 운영 중인 `GEO/AEO Audit System`과 연동하는 방법을 상세히 설명합니다.

---

## 1. 시스템 작동 원리 (How it works)

기존에는 파이썬 점검 스크립트가 OpenAI(GPT-4)나 Google(Gemini) 서버로 데이터를 보내고 응답을 받는 **클라우드 API 방식**이었습니다. 이 방식은 외부 네트워크에 의존하며 토큰당 비용이 발생합니다.

로컬 LLM을 도입하면 시스템 아키텍처가 다음과 같이 변경됩니다.

1. **로컬 서버 역할 (Ollama)**: 맥 미니에 설치된 Ollama 프로그램이 365일 24시간 실행되면서, 내부망(`localhost:11434`)에서 API 요청을 대기하는 **미니 OpenAI 서버** 역할을 합니다.
2. **AI 연산 (M4 Unified Memory)**: 파이썬 코드가 프롬프트를 보내면, 맥 미니의 초고속 M4 칩과 16GB 통합 메모리가 자체적으로 Llama 3 등의 AI 모델을 구동하여 답변을 생성합니다. (약 5~8GB의 메모리 점유)
3. **비용 및 보안**: 외부로 데이터가 나가지 않으므로 **사내 보안 유지**에 완벽하며, 1억 개의 URL을 점검해도 **토큰 비용이 전혀 발생하지 않습니다.**

---

## 2. Ollama 설치 및 로컬 LLM 구동 (맥 미니 세팅)

### Step 2.1. Ollama 다운로드 및 설치
1. 맥 미니에서 브라우저를 열고 [Ollama 공식 홈페이지(https://ollama.com)](https://ollama.com/)에 접속합니다.
2. **[Download]** 버튼을 눌러 Mac 버전을 다운로드합니다.
3. 다운로드된 `Ollama-darwin.zip` 파일을 압축 해제하고, `Ollama.app`을 **응용 프로그램(Applications)** 폴더로 이동한 후 실행합니다.

### Step 2.2. 외부 PC 접속 허용 설정 (중요 ⭐️)
기본적으로 Ollama는 보안을 위해 맥 미니 자기 자신(`localhost`)의 접속만 허용합니다. **작업하시는 윈도우 PC나 다른 서버에서 맥 미니의 LLM을 호출하려면 외부 접속을 허용해야 합니다.**

1. 맥 미니의 **터미널(Terminal)** 앱을 실행합니다.
2. 아래 명령어를 복사해서 붙여넣고 엔터를 칩니다.
   ```bash
   launchctl setenv OLLAMA_HOST "0.0.0.0"
   ```
3. 우측 상단 메뉴 막대에서 Ollama 아이콘을 클릭한 뒤 **Quit Ollama**를 눌러 종료합니다.
4. 다시 **응용 프로그램** 폴더에서 `Ollama.app`을 실행하여 켭니다. (이제 외부 PC에서 접속 가능해집니다.)
5. 맥 미니의 **시스템 설정 > 네트워크**에 들어가서 맥 미니의 내부 IP 주소(예: `192.168.0.x` 또는 `10.x.x.x`)를 메모해 둡니다.

### Step 2.3. 터미널에서 AI 모델 다운로드
M4 기본형(16GB)에 가장 최적화된 **Llama 3.1 (8B)** 모델을 설치해 보겠습니다.

1. 터미널(Terminal) 앱에서 아래 명령어를 입력합니다.
   ```bash
   ollama run llama3.1
   ```
2. 모델(약 4.7GB)이 다운로드되고 나면, 프롬프트(`>>>`)가 뜹니다. `Ctrl + D`를 눌러 대화창을 빠져나옵니다. 

---

## 3. GEO/AEO Audit 파이썬 코드 연동 방법 (작업용 PC)

이제 **작업하시는 윈도우 PC(또는 스케줄러 서버)**에 있는 파이썬 스크립트에서, 맥 미니의 IP 주소를 바라보도록 코드를 수정해야 합니다. 
Ollama는 **OpenAI API 라이브러리와 100% 완벽하게 호환**되므로 코드 수정이 매우 간단합니다.

### Step 3.1. 파이썬 라이브러리 확인
작업용 PC 터미널(CMD)에서 라이브러리를 확인/설치합니다.
```bash
pip install openai
```

### Step 3.2. 코드 수정 예시 (기존 코드 ➡️ 변경 코드)

현재 `analyzer.py` 내부의 LLM 호출 로직을 아래와 같이 변경합니다.

**변경 전: OpenAI API 사용 방식**
```python
from openai import AsyncOpenAI
import os

# 외부로 나가는 진짜 OpenAI 서버와 인증키 사용
client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

async def evaluate_seo_content(text_content):
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": f"이 콘텐츠의 SEO를 평가해줘: {text_content}"}]
    )
    return response.choices[0].message.content
```

**변경 후: 맥 미니 로컬 Ollama 연동 방식**
```python
from openai import AsyncOpenAI

# 1. api_key는 아무 텍스트나 넣어도 무방합니다.
# 2. base_url을 '맥 미니의 IP 주소'로 지정합니다. (예: 192.168.0.15)
client = AsyncOpenAI(
    api_key="ollama",
    base_url="http://192.168.0.15:11434/v1"  # <-- 맥 미니의 IP 주소로 변경하세요!
)

async def evaluate_seo_content(text_content):
    response = await client.chat.completions.create(
        model="llama3.1", # 맥 미니에 다운로드 받은 로컬 모델 이름
        messages=[{"role": "user", "content": f"이 콘텐츠의 SEO를 평가해줘: {text_content}"}]
    )
    return response.choices[0].message.content
```

### Step 3.3. 시스템 가동 및 모니터링
코드를 수정한 후 `batch_audit.py`나 `main.py` 스케줄러를 실행합니다.
- 점검이 백그라운드에서 돌아가면, 맥 미니의 **활성 상태 보기(Activity Monitor)**를 열어보세요.
- 메모리 탭에서 약 5~6GB 정도를 모델이 사용하고 있고, CPU 탭에서 `ollama_llama_server` 프로세스가 활발하게 일하고 있는 것을 볼 수 있습니다.

---

## 4. 추천하는 M4 로컬 최적화 팁

1. **동시성(Concurrency) 조절**: 
   - 외부 API는 한 번에 100개씩 던져도 클라우드가 알아서 처리해주지만, 로컬 맥 미니는 혼자서 처리해야 합니다.
   - `batch_audit.py` 실행 시 `--concurrency 2` 또는 `3` 정도로 낮춰서 던지는 것이 맥 미니가 다운되지 않고 꾸준히 처리하는 데 좋습니다. (예: `python batch_audit.py reports/lg_urls_us.csv --concurrency 2`)
2. **다양한 모델 활용**: 
   - Llama 3.1이 영어나 다국어 평가에 좋다면, 코딩이나 문맥 파악에는 Google의 `gemma2`, 한국어 특화로는 `qwen2.5`가 좋습니다.
   - 언제든 `ollama run qwen2.5` 명령어로 새 모델을 추가하고, 파이썬 코드의 `model="qwen2.5"`로 이름만 바꿔주면 즉각 모델 교체가 가능합니다.
