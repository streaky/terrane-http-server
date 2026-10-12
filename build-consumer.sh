#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
compiler=${TERRANE:-"$root/target/debug/terrane"}
artifact=$("$compiler" build "$root/packages/http-server/consumer/package.toml")
mkdir -p "$root/packages/http-server/.trn/consumers"
cp "$artifact" "$root/packages/http-server/.trn/consumers/http-server"
sh "$root/packages/template/build-consumer.sh"
