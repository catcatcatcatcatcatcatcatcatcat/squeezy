.PHONY: install uninstall run dev-run test

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
