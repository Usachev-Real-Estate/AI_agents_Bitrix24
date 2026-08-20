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

contact-source-lock:
	$(PYTHON) src/contact_source_lock.py

deal-source-lock:
	$(PYTHON) src/deal_source_lock.py

buyer-base-rate-lock:
	$(PYTHON) src/buyer_base_rate_lock.py

buyer-commission-reminder:
	$(PYTHON) src/buyer_commission_reminder.py

broker-rating-daily:
	$(PYTHON) src/broker_rating_collectors.py --daily

broker-rating-report:
	$(PYTHON) src/broker_rating_report.py

lead-quality:
	$(PYTHON) src/lead_quality_audit.py

import-kc-owners:
	$(PYTHON) scripts/import_kc_owners.py

docker-build:
	docker build -t b24-ai-auditor:latest .
