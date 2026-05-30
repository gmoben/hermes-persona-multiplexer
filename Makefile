.PHONY: install test lint fmt check

install:  ## install the package + dev tooling
	pip install -e ".[dev]"

test:     ## run the core test suite (no Hermes/discord needed)
	pytest

lint:     ## lint with ruff
	ruff check .

fmt:      ## auto-fix + format with ruff
	ruff check --fix .
	ruff format .

check: lint test  ## what CI runs
