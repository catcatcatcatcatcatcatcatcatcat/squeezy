.PHONY: install uninstall run test

install:
	pipx install .

uninstall:
	pipx uninstall squeezy

run:
	squeezy

test:
	pytest tests/ -v --timeout=60
