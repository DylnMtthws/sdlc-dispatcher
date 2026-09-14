PYTHON ?= .venv/bin/python
BIN := $(dir $(PYTHON))

.PHONY: help install check test format docker-test demo build
help:
	@echo "install  Install editable package and developer tools"
	@echo "check    Run lint, formatting checks, and offline tests"
	@echo "test     Run offline tests with branch coverage"
	@echo "format   Apply Ruff import fixes and Black formatting"
	@echo "docker-test  Build image and run real container boundary tests"
	@echo "demo     Run the deterministic Docker demo in a fresh state directory"
	@echo "build    Build wheel and source distribution"

install:
	$(PYTHON) -m pip install -e '.[server,dev]'

check:
	$(BIN)ruff check src tests integrations/deck-lab/*.py
	$(BIN)black --check src tests integrations/deck-lab/*.py
	$(MAKE) test

test:
	$(PYTHON) -m coverage run -m unittest discover -s tests -v
	$(PYTHON) -m coverage report

format:
	$(BIN)ruff check --select I --fix src tests integrations/deck-lab/*.py
	$(BIN)black src tests integrations/deck-lab/*.py

docker-test:
	docker build -f containers/test.Dockerfile -t sdlc-dispatcher-test:local .
	DISPATCHER_DOCKER_TESTS=1 $(PYTHON) -m unittest discover -s tests -p test_docker.py -v

demo:
	docker build -f containers/test.Dockerfile -t sdlc-dispatcher-test:local .
	$(BIN)sdlc-dispatcher --state .dispatcher/demo-$$(date +%Y%m%d-%H%M%S) demo

build:
	$(PYTHON) -m build
