#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIB="$ROOT/scripts/lib/docker_compat.sh"
fail(){ echo "FAIL: $*" >&2; exit 1; }
source "$LIB"
DOCKER_FLAVOR=snap
set +e
out="$(validate_project_path_for_docker /opt/wx-video-download 2>&1)"
rc=$?
set -e
[ "$rc" -ne 0 ] || fail '/opt should be rejected for Snap Docker'
case "$out" in *'/root'*) ;; *) fail "expected relocation hint, got: $out";; esac
validate_project_path_for_docker /root/wx-video-download >/dev/null || fail '/root should be accepted for Snap Docker'
DOCKER_FLAVOR=system
validate_project_path_for_docker /opt/wx-video-download >/dev/null || fail '/opt should be accepted for non-Snap Docker'
echo 'PASS: Snap Docker project path guard'
