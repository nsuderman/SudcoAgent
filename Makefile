.PHONY: install dev test lint discover sweep enrich analyze build review send followup health

PYTHON_VERSION ?= 3.12

# Uses `uv` (Astral) — handles Python versions, venv creation, and dependency
# installs without depending on system python3-venv apt packages.
install:
	uv venv --python $(PYTHON_VERSION) .venv
	uv pip install --python .venv/bin/python -e ".[dev]"
	.venv/bin/playwright install chromium

# Sanity check — talk to the API and the local LLM
health:
	.venv/bin/agent health

# Find new prospects in an area, store in DB via API
# Usage: make discover AREA="Pasco, WA" QUERY=bakery LIMIT=500
# LIMIT > 50 triggers pagination automatically.
discover:
	.venv/bin/agent discover --area "$(AREA)" $(if $(QUERY),--query "$(QUERY)") --limit $(or $(LIMIT),500)

# Sweep top N cities for a query (defaults: top 200 across US+TX combined, 5000/city)
# Usage: make sweep QUERY=bakery
sweep:
	.venv/bin/agent sweep --query "$(QUERY)" --top $(or $(TOP),200) --region $(or $(REGION),all)

# Find emails on prospects' existing websites
enrich:
	.venv/bin/agent enrich

# Fetch Google Maps rating for prospects (Playwright-based)
analyze:
	.venv/bin/agent analyze

# Build a demo for one prospect (uses Qwen)
build:
	.venv/bin/agent build --prospect-id $(PID)

# Open the human approval TUI
review:
	.venv/bin/agent review

# Send all approved demos
send:
	.venv/bin/agent send

# Send follow-ups to no-replies past the FOLLOWUP_AFTER_DAYS threshold
followup:
	.venv/bin/agent followup

test:
	.venv/bin/pytest

lint:
	.venv/bin/ruff check sudco_agent
