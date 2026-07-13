#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "[build] C++ util"
g++ -O2 -std=c++17 -o util/solver-util util/solver_util.cpp

echo "[build] Rust watchdog"
(cd watchdog && cargo build --release && cp -f target/release/solver-watchdog ./solver-watchdog)

echo "[build] Go gateway"
(cd gateway && go build -trimpath -ldflags='-s -w' -o solver-gateway .)

echo "[build] OK"
ls -la gateway/solver-gateway watchdog/solver-watchdog util/solver-util
