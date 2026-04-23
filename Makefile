# Pulse — local development helpers

SHELL := /usr/bin/env bash
PROJECT ?= $(CURDIR)
SCRIPTS := $(PROJECT)/scripts

.PHONY: setup setup-pg start start-pg start-backend ps health

setup:
	bash "$(SCRIPTS)/setup.sh"

setup-pg:
	bash "$(SCRIPTS)/setup-pg.sh"

start:
	bash "$(SCRIPTS)/start.sh"

start-pg:
	bash "$(SCRIPTS)/start.sh" pg

start-backend:
	bash "$(SCRIPTS)/start.sh" backend

ps:
	@echo "backend: $$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8010/health || echo DOWN)"

health:
	@curl -s http://127.0.0.1:8010/health >/dev/null && echo "Backend: OK" || echo "Backend: DOWN"
