.PHONY: init run test smoke docker-api ntfy ui

init:
	python3 -m venv .venv
	. .venv/bin/activate && pip install -r requirements.txt && pip install -e .
	. .venv/bin/activate && python -m mongoose.cli init-db

run:
	. .venv/bin/activate && uvicorn mongoose.app:app --host 127.0.0.1 --port 8765 --reload

test:
	PYTHONPATH=src python3 -m pytest -q

smoke:
	./scripts/smoke_test.sh

ntfy:
	docker compose up -d ntfy

docker-api:
	docker compose --profile api up -d --build mongoose-api

ui:
	docker compose --profile ui up -d open-webui
