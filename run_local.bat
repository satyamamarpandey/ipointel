@echo off
setlocal
if not exist .env copy .env.example .env >nul
python -m pip install -r requirements.txt
start "IPO worker" python -m app.worker
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
