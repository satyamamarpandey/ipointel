.PHONY: test run worker
run:
	uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
worker:
	python -m app.worker
test:
	pytest -q
