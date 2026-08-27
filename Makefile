.PHONY: install format lint typecheck security test test-cov check run migrate seed compose-up compose-down

install:
	python -m pip install -e ".[dev]"

format:
	ruff format .
	ruff check . --fix

lint:
	ruff format --check .
	ruff check .

typecheck:
	mypy app

security:
	bandit -c pyproject.toml -r app
	pip-audit

test:
	pytest

test-cov:
	pytest --cov=app --cov-report=term-missing --cov-report=xml

check: lint typecheck test-cov
	bandit -c pyproject.toml -r app

run:
	uvicorn app.main:app --reload

migrate:
	alembic upgrade head

seed:
	pulsewatch seed

compose-up:
	docker compose up --build

compose-down:
	docker compose down
