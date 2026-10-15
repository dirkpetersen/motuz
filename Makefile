# The Docker install (the alternative). The default install has no Docker:
#   sudo bin/systemd/bootstrap.sh --repo=URL --branch=B   (README, "Install (default: without Docker)")
.PHONY: quickstart
quickstart:
	./bin/quickstart.sh

.PHONY: build
build:
	./bin/prod/build.sh --no-cache

.PHONY: start
start:
	./bin/prod/start.sh

