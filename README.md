# tvs-ai-gateway

운영 배포용 AI Gateway 독립 프로젝트입니다. 판매 데이터 요청을 `tv-sales-agent`로 전달하고,
요청 이력 Projection과 결과 파일 다운로드를 제공합니다.

## 포함 범위

- `POST /api/assistant/message`
- `POST /api/assistant/requests/{requestId}/execute`
- `GET /api/requests`, `GET /api/requests/{requestId}`
- `GET /api/artifacts/{artifactId}/download`
- Agent/MinIO 상태 확인 API
- API Key, 사용자/테넌트 헤더, CORS, JSON 로그

RAG, Chroma, LLM Provider, CrewAI, DB 직접 조회, 배치 및 샘플 파일은 포함하지 않습니다.

## 로컬 실행

```powershell
Copy-Item .env.example .env
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

보호 API 호출 시 `X-API-Key`, `X-Tenant-Id`, `X-User-Id` 헤더를 함께 전송합니다.

## 테스트

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Docker

`.env.example`을 `.env`로 복사한 뒤 실제 운영 비밀값을 설정합니다.

```powershell
docker compose up -d --build
```

`.env`와 `data/`는 이미지에 포함되지 않습니다.
