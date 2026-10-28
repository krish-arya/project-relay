# Every target is a thin wrapper around `uv run relay ...`, so the same steps
# work without make (see README, "Without make").
INBOX ?= data/northwind/tickets.jsonl
RUNS ?= 5
RELAY = uv run relay

.PHONY: demo up down run worker eval test list report clean-db

up:
	docker compose up -d --wait

down:
	docker compose down

demo: up
	uv sync --quiet
	$(RELAY) demo

run: up
	$(RELAY) migrate
	$(RELAY) run --inbox $(INBOX)

worker:
	$(RELAY) worker

eval: up
	$(RELAY) migrate
	$(RELAY) seed
	$(RELAY) eval --runs $(RUNS)

test: up
	uv run --group dev pytest

list:
	$(RELAY) list

report:
	$(RELAY) report --view internal

# Drops the database volume: every ticket, decision and audit row is lost.
clean-db:
	docker compose down -v
