#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/../../../.." && pwd)
demo="$root/packages/http-server/demo"
compiler=${TERRANE:-"$root/target/release/terrane"}

if [ ! -x "$compiler" ]; then
    echo "Terrane CLI not found or not executable: $compiler (set TERRANE to override)" >&2
    exit 1
fi

artifact=$("$compiler" build "$demo/package.toml")
case "$artifact" in
    /*) ;;
    *) echo "Terrane build did not return an absolute artifact path: $artifact" >&2; exit 1 ;;
esac
if [ ! -f "$artifact" ]; then
    echo "Terrane build artifact not found: $artifact" >&2
    exit 1
fi

mkdir -p "$demo/.docker"
cp -f "$artifact" "$demo/.docker/app"
chmod 0555 "$demo/.docker/app"

cd "$demo"
docker compose -f compose.yaml build "$@"
docker compose -f compose.yaml up --wait
