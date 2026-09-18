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

shared-lead-qualify-reminder:
	$(PYTHON) src/shared_lead_qualify_reminder.py

broker-rating-daily:
	$(PYTHON) src/broker_rating_collectors.py --daily

broker-rating-report:
	$(PYTHON) src/broker_rating_report.py

lead-quality:
	$(PYTHON) src/lead_quality_audit.py

import-kc-owners:
	$(PYTHON) scripts/import_kc_owners.py

# --- Аналитическая витрина и дашборд ---

etl:
	$(PYTHON) src/analytics/etl.py --incremental

etl-full:
	$(PYTHON) src/analytics/etl.py --full

etl-backfill:
	$(PYTHON) src/analytics/etl.py --backfill

etl-probe:
	$(PYTHON) src/analytics/etl.py --probe

dashboard:
	$(PYTHON) src/web/server.py

dashboard-adduser:
	@if [ -z "$(USER_LOGIN)" ]; then \
		echo "Error: USER_LOGIN is required. Usage: make dashboard-adduser USER_LOGIN=ivanov"; \
		exit 1; \
	fi
	$(PYTHON) src/web/manage.py adduser $(USER_LOGIN)

dashboard-secret:
	$(PYTHON) src/web/manage.py gen-secret

docker-build:
	docker build -t b24-ai-auditor:latest .
