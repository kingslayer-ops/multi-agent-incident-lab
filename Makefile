.PHONY: e2e e2e-smoke

e2e:
	sh scripts/run-e2e.sh

e2e-smoke:
	E2E_MODE=smoke sh scripts/run-e2e.sh
