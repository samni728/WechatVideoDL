#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIB="$ROOT/scripts/lib/docker_compat.sh"
fail(){ echo "FAIL: $*" >&2; exit 1; }
assert_eq(){ [ "$1" = "$2" ] || fail "expected [$2], got [$1]"; }

run_case(){
  local timeout="$1" expected="$2"
  local tmp="$(mktemp -d)"
  mkdir -p "$tmp/bin"
  cat > "$tmp/bin/docker" <<SH
#!/usr/bin/env bash
case "\${1:-}" in
  ps)
    echo 'browserless|browserless/chrome@sha256:abc|0.0.0.0:3080->3000/tcp'
    exit 0;;
  inspect)
    if printf '%s' "\$*" | grep -q '{{.Config.Image}}'; then
      echo 'browserless/chrome@sha256:abc'
    else
      cat <<'ENV'
TOKEN=secret-token
CONNECTION_TIMEOUT=$timeout
ENV
    fi
    exit 0;;
  *) exit 0;;
esac
SH
  chmod +x "$tmp/bin/docker"
  out="$(PATH="$tmp/bin:/usr/bin:/bin" bash -c 'source "$1"; detect_running_browserless 180000; printf "%s|%s|%s|%s|%s|%s" "$RUNNING_BROWSERLESS_NAME" "$RUNNING_BROWSERLESS_URL" "$RUNNING_BROWSERLESS_TOKEN" "$RUNNING_BROWSERLESS_PORT" "$RUNNING_BROWSERLESS_COMPATIBLE" "$RUNNING_BROWSERLESS_IMAGE"' _ "$LIB")"
  rm -rf "$tmp"
  assert_eq "$out" "$expected"
}

run_case 300000 'browserless|http://host.docker.internal:3080|secret-token|3080|yes|browserless/chrome@sha256:abc'
run_case 60000 'browserless|http://host.docker.internal:3080|secret-token|3080|no|browserless/chrome@sha256:abc'

echo 'PASS: existing Browserless compatibility detection'
