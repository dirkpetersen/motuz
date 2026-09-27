#!/usr/bin/env bash

set -e

THIS_DIR=$(dirname "$0")
cd ${THIS_DIR}
cd ../..

# Plain-JS frontend helpers (no JSX), run with Node's built-in test runner. Twice, in
# different time zones: file ages must not depend on the browser's time zone.
for tz in UTC America/New_York; do
    echo "TZ=$tz"
    TZ=$tz node --no-warnings --test test/frontend/*.mjs
done
