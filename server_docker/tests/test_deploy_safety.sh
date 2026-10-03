#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY="$ROOT/scripts/deploy.sh"
[ -x "$DEPLOY" ] || { echo "FAIL: missing executable $DEPLOY" >&2; exit 1; }

fail(){ echo "FAIL: $*" >&2; exit 1; }
assert_contains(){ case "$1" in *"$2"*) ;; *) fail "expected [$2] in [$1]";; esac; }
assert_not_contains(){ case "$1" in *"$2"*) fail "did not expect [$2] in [$1]";; *) :;; esac; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/project/data"
cp "$ROOT/docker-compose.yml" "$TMP/project/docker-compose.yml"
[ -f "$ROOT/docker-compose.browserless.yml" ] && cp "$ROOT/docker-compose.browserless.yml" "$TMP/project/docker-compose.browserless.yml"
mkdir -p "$TMP/project/scripts/lib"
cp "$ROOT/scripts/lib/docker_compat.sh" "$TMP/project/scripts/lib/"

cat > "$TMP/bin/docker" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${FAKE_LOG}"
case "${1:-}" in
  info)
    [ "${FAKE_INFO_OK:-1}" = 1 ] || exit 1
    if [ "${2:-}" = "--format" ]; then echo "${FAKE_ROOT:-/var/snap/docker/common/var-lib-docker}"; else echo ok; fi
    exit 0;;
  version) echo '29.8.0'; exit 0;;
  compose)
    if [ "${2:-}" = version ]; then echo 'Docker Compose version v2.40.0'; exit 0; fi
    exit 0;;
  ps) exit 0;;
  image)
    [ "${FAKE_IMAGE_EXISTS:-1}" = 1 ] && exit 0 || exit 1;;
  *) exit 0;;
esac
SH
chmod +x "$TMP/bin/docker"

# Dry-run with healthy Snap Docker must stay project-scoped and must not invoke any global service/install command.
: > "$TMP/log1"
out="$(cd "$TMP/project" && PATH="$TMP/bin:/usr/bin:/bin" FAKE_LOG="$TMP/log1" FAKE_INFO_OK=1 FAKE_IMAGE_EXISTS=1 APP_PORT=18770 bash "$DEPLOY" --dry-run 2>&1)"
assert_contains "$out" 'Docker state      : ready'
assert_contains "$out" 'Docker flavor     : snap'
assert_contains "$out" 'Compose project   : wx-video-download'
assert_contains "$out" 'DRY RUN'
assert_not_contains "$(cat "$TMP/log1")" 'systemctl'
assert_not_contains "$(cat "$TMP/log1")" 'apt-get'
assert_not_contains "$(cat "$TMP/log1")" 'snap install'

# Existing CLI + unreachable daemon must fail safely and never try to install/start another daemon.
: > "$TMP/log2"
set +e
out2="$(cd "$TMP/project" && PATH="$TMP/bin:/usr/bin:/bin" FAKE_LOG="$TMP/log2" FAKE_INFO_OK=0 bash "$DEPLOY" --dry-run 2>&1)"
rc=$?
set -e
[ "$rc" -ne 0 ] || fail 'unreachable daemon unexpectedly succeeded'
assert_contains "$out2" 'will NOT install or start another Docker daemon'
assert_not_contains "$(cat "$TMP/log2")" 'systemctl start'

# Browserless image absent => dry-run must say it would pull only Browserless, not Docker itself.
: > "$TMP/log3"
out3="$(cd "$TMP/project" && PATH="$TMP/bin:/usr/bin:/bin" FAKE_LOG="$TMP/log3" FAKE_INFO_OK=1 FAKE_IMAGE_EXISTS=0 bash "$DEPLOY" --dry-run 2>&1)"
assert_contains "$out3" 'Browserless image : missing (will pull this image only)'
assert_not_contains "$out3" 'install Docker'

echo 'PASS: deployment safety behavior'
