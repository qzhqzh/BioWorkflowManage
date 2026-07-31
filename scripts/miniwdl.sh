#!/bin/sh
set -eu

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
project_root=$(CDPATH= cd -- "$script_directory/.." && pwd -P)

run_root=${MINIWDL_RUN_ROOT:-"$project_root/data/miniwdl/work"}
case "$run_root" in
  /*) ;;
  *)
    echo "MINIWDL_RUN_ROOT 必须是绝对路径：$run_root" >&2
    exit 64
    ;;
esac

MINIWDL_UID=${MINIWDL_UID:-$(id -u)}
MINIWDL_GID=${MINIWDL_GID:-$(id -g)}

mkdir -p \
  "$run_root" \
  "$project_root/data/miniwdl-certs/client" \
  "$project_root/data/miniwdl-engine"
chmod 0700 "$project_root/data/miniwdl-certs"
MINIWDL_RUN_ROOT=$(CDPATH= cd -- "$run_root" && pwd -P)
export MINIWDL_RUN_ROOT MINIWDL_UID MINIWDL_GID

compose() {
  docker compose \
    --project-directory "$project_root" \
    -f "$project_root/docker-compose.yml" \
    "$@"
}

run_static() {
  compose --profile wdl-check build miniwdl-check
  compose --profile wdl-check run --rm --no-deps miniwdl-check \
    python /workspace/scripts/miniwdl_runtime.py "$@"
}

start_runtime() {
  compose --profile wdl-runtime build miniwdl-runner
  compose --profile wdl-runtime up -d --wait miniwdl-docker
}

run_runtime() {
  start_runtime
  compose --profile wdl-runtime run --rm --no-deps miniwdl-runner \
    python /workspace/scripts/miniwdl_runtime.py "$@"
}

usage() {
  cat <<'EOF'
用法：./scripts/miniwdl.sh <command> [case]

  check                   静态校验所有项目 WDL，不启动 Docker 执行引擎
  prepare [case]          创建案例数据目录；省略 case 时准备全部案例
  preflight <case>        静态校验并检查案例数据是否齐全
  doctor                  检查隔离的 Docker-in-Docker 执行引擎
  smoke                   真实运行无数据的容器 smoke WDL
  run <case>              使用已准备的数据真实运行案例
  self-test               运行 miniwdl 官方联网 self-test（诊断用途）
  status                  查看隔离执行引擎状态
  logs                    查看隔离执行引擎日志
  stop                    停止隔离执行引擎，不删除运行结果或镜像缓存

案例：fastp、fastp-bwa
EOF
}

command=${1:-help}
if [ "$#" -gt 0 ]; then
  shift
fi

case "$command" in
  check | prepare | preflight)
    run_static "$command" "$@"
    ;;
  doctor | smoke | run | self-test)
    run_runtime "$command" "$@"
    ;;
  status)
    compose --profile wdl-runtime ps miniwdl-docker
    ;;
  logs)
    compose --profile wdl-runtime logs --no-color miniwdl-docker
    ;;
  stop)
    compose --profile wdl-runtime stop miniwdl-docker
    ;;
  help | -h | --help)
    usage
    ;;
  *)
    echo "未知命令：$command" >&2
    usage >&2
    exit 64
    ;;
esac
