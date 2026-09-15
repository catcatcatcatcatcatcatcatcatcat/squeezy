.PHONY: install uninstall run dev-run test local-network-prompt release-patch release-minor release-major

install:
	pipx install .

uninstall:
	pipx uninstall squeezy

run:
	squeezy

dev-run:
	PYTHONPATH=src python -m squeezy

test:
	PYTHONPATH=src pytest tests/ -v --timeout=60

# macOS: re-trigger the Local Network permission prompt for Python
# (needed again after `brew upgrade python@3.x` — the grant is per binary)
local-network-prompt:
	./run.sh --request-local-network

release-patch:
	./release.sh patch

release-minor:
	./release.sh minor

release-major:
	./release.sh major
