.PHONY: help smoke run run-observability down clean

.DEFAULT_GOAL := help

help:
	@echo "Targets:"
	@echo "  make smoke                quick end-to-end check (N=2 + pprof, ~15-20 min)"
	@echo "  make run                  run measurement (3 pairs, ~25 min after first build)"
	@echo "                            env: PAIRS, CAPTURE_PPROF, OBSERVABILITY, POOL_SIZE, ..."
	@echo "                            e.g.: PAIRS=20 CAPTURE_PPROF=1 make run"
	@echo "  make run-observability    same as run, also brings up prometheus + grafana"
	@echo "  make down                 stop docker-compose stack"
	@echo "  make clean                stop + wipe volumes and stand/runs/"

smoke:
	cd stand && PAIRS=2 CAPTURE_PPROF=1 ./scripts/run.sh

run:
	cd stand && ./scripts/run.sh

run-observability:
	cd stand && OBSERVABILITY=1 ./scripts/run.sh

down:
	cd stand && docker compose --profile control --profile observability down

clean:
	cd stand && docker compose --profile control --profile observability down -v
	rm -rf stand/runs
