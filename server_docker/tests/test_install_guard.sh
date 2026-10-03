#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALLER="$ROOT/scripts/install-docker-if-missing.sh"
[ -x "$INSTALLER" ] || { echo "FAIL: missing executable $INSTALLER" >&2; exit 1; }

fail(){ echo "FAIL: $*" >&2; exit 1; }
assert_contains(){ case "$1" in *"$2"*) ;; *) fail "expected [$2] in [$1]";; esac; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"
cat > "$TMP/bin/docker" <<'SH'
#!/usr/bin/env bash
case "${1:-}" in
  info)
    [ "${FAKE_INFO_OK:-1}" = 1 ] || exit 1
    if [ "${2:-}" = "--format" ]; then echo '/var/snap/docker/common/var-lib-docker'; else echo ok; fi
    exit 0;;
  version) echo '29.8.0'; exit 0;;
  compose) echo 'Docker Compose version v2.40.0'; exit 0;;
esac
exit 0
SH
chmod +x "$TMP/bin/docker"

out="$(PATH="$TMP/bin:/usr/bin:/bin" FAKE_INFO_OK=1 bash "$INSTALLER" 2>&1)"
assert_contains "$out" 'Existing Docker is healthy; reusing it.'
assert_contains "$out" '/var/snap/docker/common/var-lib-docker'

set +e
out2="$(PATH="$TMP/bin:/usr/bin:/bin" FAKE_INFO_OK=0 bash "$INSTALLER" 2>&1)"
rc=$?
set -e
[ "$rc" -ne 0 ] || fail 'unreachable daemon should fail'
assert_contains "$out2" 'Refusing to install a second Docker daemon'

echo 'PASS: Docker install guard'
