# Gilead Demo Chatbot

FastAPI + static frontend chatbot for the Gilead field inquiry playbook.

## Local Run (Windows)
1. Create `config/.env` and set:
   - `AZURE_OPENAI_KEY`
   - `AZURE_OPENAI_ENDPOINT`
   - `EMBEDDING_DEPLOYMENT`
   - `CHAT_DEPLOYMENT`
   - `API_VERSION` (optional, defaults to `2024-02-15-preview`)
2. Run `config\run_demo.bat`.
3. Open `http://127.0.0.1:8000`.

## Render Deployment
This repo includes a Render blueprint file: `render.yaml`.

1. Push this repo to GitHub.
2. In Render, click **New +** -> **Blueprint** and connect this repository.
3. Set the required environment variables in Render:
   - `AZURE_OPENAI_KEY`
   - `AZURE_OPENAI_ENDPOINT`
   - `EMBEDDING_DEPLOYMENT`
   - `CHAT_DEPLOYMENT`
   - `API_VERSION` (optional)
4. Deploy.

### Notes
- The app starts with: `python -m uvicorn src.main:app --host 0.0.0.0 --port $PORT`.
- `DATA_DIR` defaults to `/opt/render/project/data` via `render.yaml`.
- If Azure environment variables are missing, the app still runs but falls back to non-Azure behavior.

## Validation
```powershell
.venv\Scripts\python -m src.validate_demo
```
