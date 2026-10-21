#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

# Create and remove the scratch directory that one CI job owns.
#
#   ci-scratch.sh create    makes a directory directly under RUNNER_TEMP, marks it
#                           as owned by this job and exports its path and the
#                           cache locations that live inside it
#   ci-scratch.sh cleanup   removes that directory after checking that it is
#                           the one this job created
#
# Only the directory this job created is ever removed: not RUNNER_TEMP, not the
# tool cache and not another job's files. A host failure or SIGKILL can still
# leave the directory behind; the runner's own temporary-directory handling is
# the only thing that covers that.

set -Eeuo pipefail

OWNER_FILE=.omnitient-ci-owner

fail() {
    echo "::error::ci-scratch: $*" >&2
    exit 1
}

# What this job writes into the marker file, and expects to read back.
owner_tag() {
    printf '%s/%s/%s/%s\n' "${GITHUB_RUN_ID}" "${GITHUB_RUN_ATTEMPT}" "${GITHUB_JOB}" "${CI_JOB_KEY:-}"
}

temp_root() {
    [[ -n "${RUNNER_TEMP:-}" && -d "${RUNNER_TEMP}" ]] || fail "RUNNER_TEMP is not a directory"
    (cd "${RUNNER_TEMP}" && pwd -P)
}

create() {
    local root scratch
    root="$(temp_root)"
    scratch="$(mktemp -d "${root}/omnitient-ci.XXXXXX")"
    # A failure between here and the export leaves nothing behind.
    trap 'rm -rf --one-file-system -- "${scratch}"' ERR
    owner_tag >"${scratch}/${OWNER_FILE}"
    {
        echo "CI_SCRATCH=${scratch}"
        echo "PIP_CACHE_DIR=${scratch}/pip-cache"
        echo "PRE_COMMIT_HOME=${scratch}/pre-commit"
    } >>"${GITHUB_ENV}"
    trap - ERR
    echo "created ${scratch}"
}

cleanup() {
    local root real
    if [[ -z "${CI_SCRATCH:-}" ]]; then
        echo "no scratch directory was recorded; nothing to remove"
        return 0
    fi
    root="$(temp_root)"
    [[ -d "${CI_SCRATCH}" && ! -L "${CI_SCRATCH}" ]] || fail "${CI_SCRATCH} is not a plain directory"
    real="$(cd "${CI_SCRATCH}" && pwd -P)"
    [[ "$(dirname "${real}")" == "${root}" ]] || fail "${real} is not directly inside ${root}"
    [[ "$(basename "${real}")" == omnitient-ci.?????? ]] || fail "${real} does not have the scratch directory name"
    [[ "$(stat -c %u "${real}")" == "$(id -u)" ]] || fail "${real} is not owned by this user"
    [[ -f "${real}/${OWNER_FILE}" && "$(cat "${real}/${OWNER_FILE}")" == "$(owner_tag)" ]] ||
        fail "${real} is not marked as owned by this job"
    rm -rf --one-file-system -- "${real}"
    [[ ! -e "${real}" ]] || fail "could not remove ${real}"
    echo "removed ${real}"
}

case "${1:-}" in
create) create ;;
cleanup) cleanup ;;
*) fail "usage: ci-scratch.sh create|cleanup" ;;
esac
