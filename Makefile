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


# Documentation site (zensical.toml): README.md split into pages, built to site/
.PHONY: docs
docs:
	python3 bin/docs/build_site.py && zensical build -f zensical.toml --clean --strict

.PHONY: docs-serve
docs-serve:
	python3 bin/docs/build_site.py && zensical serve -f zensical.toml
