.PHONY: install uninstall run dev-run test release-patch release-minor release-major

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

release-patch:
	./release.sh patch

release-minor:
	./release.sh minor

release-major:
	./release.sh major
