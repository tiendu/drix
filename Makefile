PYTHON ?= python3
PREFIX ?= /usr/local
BINDIR ?= $(PREFIX)/bin
DESTDIR ?=
INSTALL ?= install

DISTDIR := dist
BINARY := $(DISTDIR)/drix
BUILD_ENV := .build/venv
BUILD_PYTHON := $(BUILD_ENV)/bin/python
DEV_READY := $(BUILD_ENV)/.dev-ready
BINARY_READY := $(BUILD_ENV)/.binary-ready
PY_SOURCES := $(shell find src scripts -type f -name '*.py' 2>/dev/null)
BUILD_INPUTS := $(PY_SOURCES) pyproject.toml

.PHONY: all binary install uninstall test lint typecheck check clean bootstrap-dev bootstrap-binary

all: $(BINARY)

binary: $(BINARY)

$(BUILD_PYTHON):
	@mkdir -p .build
	@$(PYTHON) -m venv "$(BUILD_ENV)" || { \
		echo "drix: could not create the isolated build environment." >&2; \
		echo "drix: install Python venv support for $(PYTHON) (for example python3-venv on Debian/Ubuntu)." >&2; \
		exit 2; \
	}
	@"$(BUILD_PYTHON)" -m pip --version >/dev/null 2>&1 || { \
		echo "drix: the isolated build environment has no pip." >&2; \
		echo "drix: install Python venv/ensurepip support and retry." >&2; \
		exit 2; \
	}

$(DEV_READY): pyproject.toml $(BUILD_PYTHON)
	@echo "drix: preparing isolated check environment..."
	@"$(BUILD_PYTHON)" -m pip install -e '.[dev]'
	@touch "$@"

$(BINARY_READY): pyproject.toml $(BUILD_PYTHON)
	@echo "drix: preparing isolated binary build environment..."
	@"$(BUILD_PYTHON)" -m pip install -e '.[binary]'
	@touch "$@"

bootstrap-dev: $(DEV_READY)

bootstrap-binary: $(BINARY_READY)

$(BINARY): $(BUILD_INPUTS)
	@$(MAKE) --no-print-directory bootstrap-binary
	"$(BUILD_PYTHON)" scripts/build_binary.py

install: $(BINARY)
	$(INSTALL) -d "$(DESTDIR)$(BINDIR)"
	$(INSTALL) -m 0755 "$(BINARY)" "$(DESTDIR)$(BINDIR)/drix"

uninstall:
	rm -f "$(DESTDIR)$(BINDIR)/drix"

test: $(DEV_READY)
	"$(BUILD_PYTHON)" -m pytest -q

lint: $(DEV_READY)
	"$(BUILD_PYTHON)" -m ruff check .

typecheck: $(DEV_READY)
	"$(BUILD_PYTHON)" -m mypy src

check: test lint typecheck
	"$(BUILD_PYTHON)" -m compileall -q src

clean:
	rm -rf .build build dist
