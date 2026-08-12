# O interpretador é o do SISTEMA, não o do PATH.
# `gi` (PyGObject) só existe em /usr/bin/python3, e é ele quem fala com o
# Evolution Data Server. Ver docs/adr/0005-python-do-sistema-por-causa-do-pygobject.md
SYS_PYTHON = /usr/bin/python3
VENV       = .venv
PY         = $(VENV)/bin/python
PIP        = $(VENV)/bin/pip

.PHONY: help venv install check-gi lint test run demo install-service clean

help:
	@echo "make venv            cria a venv no python do sistema"
	@echo "make install         venv + dependencias + o pacote em modo editavel"
	@echo "make check-gi        prova que os bindings da agenda chegaram na venv"
	@echo "make lint            ruff"
	@echo "make test            pytest"
	@echo "make run             sobe o daemon em primeiro plano (dev)"
	@echo "make demo            daemon isolado com dados ficticios, na porta 7778"
	@echo "make install-service instala e habilita o systemd user unit"

# --system-site-packages nao e opcional: e o que faz a venv enxergar o
# python3-gi instalado pelo apt em /usr/lib/python3/dist-packages.
$(VENV):
	$(SYS_PYTHON) -m venv --system-site-packages $(VENV)

venv: $(VENV)

install: venv
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -e ".[dev]"
	@echo "instalado. rode 'make check-gi' para conferir os bindings da agenda."

# Falha alto e cedo, com a instrucao do conserto, em vez de estourar
# ImportError no meio de uma automacao.
check-gi:
	@$(PY) -c "import gi; print('gi        OK')" || \
		{ echo "FALHOU: a venv nao ve o gi. Recrie com: rm -rf $(VENV) && make install"; exit 1; }
	@$(PY) -c "import gi; gi.require_version('EDataServer','1.2'); \
		from gi.repository import EDataServer; print('EDataServer OK')" || \
		{ echo "FALHOU: typelib ausente. Rode: sudo apt install gir1.2-edataserver-1.2"; exit 1; }
	@$(PY) -c "import gi; gi.require_version('ECal','2.0'); \
		from gi.repository import ECal; print('ECal      OK')" || \
		{ echo "FALHOU: typelib ausente. Rode: sudo apt install gir1.2-ecal-2.0"; exit 1; }

lint: venv
	$(VENV)/bin/ruff check src examples tests

test: venv
	$(VENV)/bin/pytest -q

run: venv
	$(PY) -m ta.daemon

# Daemon isolado, com banco proprio e dados ficticios em ingles. NAO toca o banco
# de verdade: e de la que saem as imagens do README, e da para clicar em tudo e
# apagar tudo sem consequencia.
#
# Porta 7778 para conviver com o daemon de verdade na 7777.
DEMO_DB   = /tmp/ta-demo/demo.db
DEMO_PORT = 7778

demo: venv
	@$(PY) examples/demo.py
	@echo "mural de demonstracao em http://127.0.0.1:$(DEMO_PORT)/board  (ctrl-c encerra)"
	@TA_DB=$(DEMO_DB) TA_PORT=$(DEMO_PORT) TA_AUTO_REVIEW=0 $(PY) -m ta.daemon

install-service:
	mkdir -p $(HOME)/.config/systemd/user
	sed "s|@@PROJECT_DIR@@|$(CURDIR)|g" systemd/ta.service \
		> $(HOME)/.config/systemd/user/ta.service
	systemctl --user daemon-reload
	systemctl --user enable --now ta
	@echo "servico de pe. logs: journalctl --user -u ta -f"

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
