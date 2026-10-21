#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

# Print the non-secret facts about the runner that decide whether the job can
# run: platform, user, and whether the tool cache is writable and which Python
# versions it already holds. Prints no environment variables and reads no
# runner configuration or credential files.

set -Eeuo pipefail

echo "runner: name=${RUNNER_NAME:-unknown} os=${RUNNER_OS:-unknown} arch=${RUNNER_ARCH:-unknown} environment=${RUNNER_ENVIRONMENT:-unknown}"
echo "kernel: $(uname -srm)"
if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    (. /etc/os-release && echo "distribution: ${ID:-unknown} ${VERSION_ID:-unknown}")
fi
echo "user id: $(id -u)"

cache="${RUNNER_TOOL_CACHE:-}"
if [[ -n "${cache}" ]]; then
    writable=no
    [[ -w "${cache}" ]] && writable=yes
    echo "tool cache: ${cache} (exists: $([[ -d "${cache}" ]] && echo yes || echo no), writable: ${writable})"
    if [[ -d "${cache}/Python" ]]; then
        echo "cached Python versions: $(find "${cache}/Python" -mindepth 1 -maxdepth 1 -printf '%f ' | tr ' ' '\n' | sort -V | tr '\n' ' ')"
    else
        echo "cached Python versions: none"
    fi
else
    echo "tool cache: RUNNER_TOOL_CACHE is not set"
fi

for tool in git node go docker shellcheck; do
    if command -v "${tool}" >/dev/null 2>&1; then
        echo "on PATH: ${tool}"
    else
        echo "not on PATH: ${tool}"
    fi
done
