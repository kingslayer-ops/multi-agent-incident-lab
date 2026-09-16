.PHONY: e2e e2e-smoke production-config test-postgres

e2e:
	sh scripts/run-e2e.sh

e2e-smoke:
	E2E_MODE=smoke sh scripts/run-e2e.sh

production-config:
	docker compose -f docker-compose.production.yml config -q

test-postgres:
	test -n "$$INCIDENT_LAB_TEST_POSTGRES_URL"
	pytest tests/test_postgres_integration.py
