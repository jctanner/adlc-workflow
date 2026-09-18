.PHONY: test integration validate lint

test:
	PYTHONPATH=src pytest -q

# Requires Podman, the adlc-claude-task-runner image, the local jira-emulator
# checkout, ADC credentials, and ADLC_RUN_LIVE_AGENT=1 for the Vertex call.
integration:
	PYTHONPATH=src pytest -q tests/integration/test_claude_container.py

validate:
	@echo "TODO: validate an ADLC workflow result bundle"

lint:
	@echo "TODO: configure project linting"
