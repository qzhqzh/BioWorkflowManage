#!/bin/sh
set -eu

run_root="${MINIWDL_RUN_ROOT:-/tmp/bioworkflow-miniwdl}"
case "$run_root" in
  /*) ;;
  *)
    echo "MINIWDL_RUN_ROOT must be an absolute path." >&2
    exit 64
    ;;
esac

mkdir -p \
  "$run_root/cache" \
  "$run_root/cases" \
  "$run_root/home" \
  "$run_root/runs" \
  "$run_root/tmp"

export HOME="$run_root/home"
export TMPDIR="$run_root/tmp"
export XDG_CACHE_HOME="$run_root/cache"

if [ "$#" -eq 0 ]; then
  set -- miniwdl --version
fi

exec "$@"
