PYTHON := venv/bin/python
PIP := venv/bin/pip

install:
	python3 -m venv venv
	$(PIP) install -e .[dev]
	$(PIP) install pre-commit
	venv/bin/pre-commit install

run:
	$(PYTHON) src/main.py

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m flake8 src/ tests/ scripts/

dry-run:
	DRY_RUN=true $(PYTHON) src/main.py

weekly-report:
	$(PYTHON) src/weekly_report.py

task-check:
	$(PYTHON) src/task_auditor.py

owner-track:
	$(PYTHON) src/owner_tracker.py

broker-score:
	@if [ -z "$(BROKER_ID)" ]; then \
		echo "Error: BROKER_ID is required. Usage: make broker-score BROKER_ID=123"; \
		exit 1; \
	fi
	$(PYTHON) src/broker_score.py $(BROKER_ID)

chat-poll:
	$(PYTHON) src/chat_poller.py

exclusive-expiry:
	$(PYTHON) src/exclusive_expiry.py

docker-build:
	docker build -t b24-ai-auditor:latest .
