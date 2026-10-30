#!/bin/sh
# A Mac bb_worker's launchd program: set the archive store aside, make it
# afresh for the group every runner's action account shares, and exec the
# worker (docs/executor.md).
#
#     worker-start-macos.sh WORKER CONFIG STORE GROUP
#
# A rename leaves the worker an empty store at once; the old one is removed in
# the background, so a full store cannot hold the start.
set -eu

worker=$1 config=$2 store=$3 group=$4

if [ -e "$store" ]; then
    mv "$store" "$(mktemp -d "$store.discarded.XXXXXX")/"
fi
mkdir -m 775 "$store"
chown root:"$group" "$store"

# The subshell exits at once, so launchd rather than the worker reaps the
# removal.
(rm -rf "$store".discarded.* &)

exec "$worker" "$config"
