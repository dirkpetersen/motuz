#!/usr/bin/env bash
# Entrypoint wrapper for the e2e app and celery containers: creates the test users
# alice (password AlicePass1) and bob (BobPass1) with the same uids in both containers,
# and their homes on the shared `homes` volume, then runs the real entrypoint.
set -e
id alice >/dev/null 2>&1 || useradd -M -u 1501 -d /home/alice -p '$6$0AksL.PHtkjT8ZM9$I8ZwPbQ503lVjNDTx7C5jetu4nhhpF5vboTA6OzSI/nMXxGxvgjPctzNggyw8CD4gnLB8gGP/H/Oep5U.AAqt1' alice
id bob   >/dev/null 2>&1 || useradd -M -u 1502 -d /home/bob   -p '$6$G1Kkfa/zD33f3qPy$F94kbLsXq/qQPPKATUCNRjmtL0.oRYTj50aefTiWgwL1axqWRirkAZAeA1DYiQ4wE8FdHa/dK8F/Jha0WU8yg0' bob
install -d -o alice -g alice -m 700 /home/alice
install -d -o bob   -g bob   -m 700 /home/bob
exec "$@"
