#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIB="$ROOT/scripts/lib/docker_compat.sh"

fail(){ echo "FAIL: $*" >&2; exit 1; }
assert_eq(){ [ "$1" = "$2" ] || fail "expected [$2], got [$1]"; }
assert_contains(){ case "$1" in *"$2"*) ;; *) fail "expected output to contain [$2], got [$1]";; esac; }

make_fake_bin(){
  local d="$1"
  mkdir -p "$d"
  cat > "$d/docker" <<'SH'
#!/usr/bin/env bash
case "${1:-}" in
  info)
    if [ "${FAKE_DOCKER_INFO_OK:-1}" = 1 ]; then
      if [ "${2:-}" = "--format" ]; then echo "${FAKE_DOCKER_ROOT:-/var/snap/docker/common/var-lib-docker}"; else echo ok; fi
      exit 0
    fi
    echo "daemon unavailable" >&2; exit 1;;
  version)
    echo "${FAKE_DOCKER_VERSION:-29.8.0}"; exit 0;;
  compose)
    [ "${FAKE_DOCKER_COMPOSE_OK:-1}" = 1 ] && { echo "Docker Compose version v2.40.0"; exit 0; }
    exit 1;;
  *) exit 0;;
esac
SH
  chmod +x "$d/docker"
}

[ -f "$LIB" ] || fail "missing $LIB"
# shellcheck source=/dev/null
source "$LIB"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Existing healthy Docker must be reused and its daemon/root reported.
make_fake_bin "$TMP/bin1"
PATH="$TMP/bin1:/usr/bin:/bin" FAKE_DOCKER_INFO_OK=1 FAKE_DOCKER_ROOT=/var/snap/docker/common/var-lib-docker \
  bash -c 'source "$1"; detect_docker; echo "$DOCKER_STATE|$DOCKER_ROOT"' _ "$LIB" > "$TMP/out1"
assert_eq "$(cat "$TMP/out1")" "ready|/var/snap/docker/common/var-lib-docker"

# Docker CLI exists but daemon is unreachable: MUST NOT be treated as missing.
make_fake_bin "$TMP/bin2"
PATH="$TMP/bin2:/usr/bin:/bin" FAKE_DOCKER_INFO_OK=0 \
  bash -c 'source "$1"; detect_docker; echo "$DOCKER_STATE"' _ "$LIB" > "$TMP/out2"
assert_eq "$(cat "$TMP/out2")" "unreachable"

# No Docker command at all => missing.
mkdir -p "$TMP/empty"
PATH="$TMP/empty" /bin/bash -c 'source "$1"; detect_docker; echo "$DOCKER_STATE"' _ "$LIB" > "$TMP/out3"
assert_eq "$(cat "$TMP/out3")" "missing"

# Prefer docker compose when present.
make_fake_bin "$TMP/bin4"
PATH="$TMP/bin4:/usr/bin:/bin" FAKE_DOCKER_COMPOSE_OK=1 \
  bash -c 'source "$1"; detect_compose; echo "$COMPOSE_KIND|${COMPOSE_CMD[*]}"' _ "$LIB" > "$TMP/out4"
assert_eq "$(cat "$TMP/out4")" "plugin|docker compose"

# If docker compose is absent, fall back to legacy docker-compose.
make_fake_bin "$TMP/bin5"
cat > "$TMP/bin5/docker-compose" <<'SH'
#!/usr/bin/env bash
echo 'docker-compose version 1.29.2'
exit 0
SH
chmod +x "$TMP/bin5/docker-compose"
PATH="$TMP/bin5:/usr/bin:/bin" FAKE_DOCKER_COMPOSE_OK=0 \
  bash -c 'source "$1"; detect_compose; echo "$COMPOSE_KIND|${COMPOSE_CMD[*]}"' _ "$LIB" > "$TMP/out5"
assert_eq "$(cat "$TMP/out5")" "legacy|docker-compose"

echo "PASS: docker compatibility detection"
