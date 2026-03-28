.PHONY: install uninstall run

install:
	pipx install .

uninstall:
	pipx uninstall squeezy

run:
	squeezy
