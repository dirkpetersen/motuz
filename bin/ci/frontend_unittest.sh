#!/usr/bin/env bash

set -e

THIS_DIR=$(dirname "$0")
cd ${THIS_DIR}
cd ../..

# Plain-JS frontend helpers (no JSX), run with Node's built-in test runner
node --no-warnings --test test/frontend/*.mjs
