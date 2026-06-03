.PHONY: install lint test run dry-run docker-build

install:
	python -m venv venv
	venv\Scripts\pip install -r requirements.txt
	venv\Scripts\pip install -r requirements-dev.txt

lint:
	venv\Scripts\flake8 src/ tests/

test:
	venv\Scripts\pytest tests/ -v

run:
	venv\Scripts\python src/main.py

dry-run:
	set DRY_RUN=true && venv\Scripts\python src/main.py

docker-build:
	docker build -t b24-ai-auditor:latest .
