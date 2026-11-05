.PHONY: help install run test lint db campaign down

help:
	@echo "install   - create venv and install dependencies"
	@echo "run       - start the API with autoreload"
	@echo "test      - run the test suite"
	@echo "lint      - ruff (optional)"
	@echo "db        - create database tables"
	@echo "campaign  - dry-run campaign against the sample numbers"
	@echo "down      - stop docker-compose services"

install:
	python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -r requirements.txt

run:
	uvicorn app.main:app --reload --port 8000

test:
	python3 -m pytest

lint:
	ruff check app tests scripts

db:
	python3 -m scripts.init_db

campaign:
	python3 -m scripts.campaign --numbers 01020645167,01131043304,01032547715

down:
	docker compose down
