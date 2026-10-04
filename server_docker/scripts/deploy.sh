#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# When called from a copied/deployment directory, prefer that current directory.
if [ -n "${WX_PROJECT_DIR:-}" ]; then
  PROJECT_DIR="$(cd "$WX_PROJECT_DIR" && pwd)"
elif [ -f "$PWD/docker-compose.yml" ] && [ -f "$PWD/Dockerfile" ]; then
  PROJECT_DIR="$PWD"
else
  PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
# shellcheck source=lib/docker_compat.sh
source "$SCRIPT_DIR/lib/docker_compat.sh"

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      cat <<'HELP'
Usage: ./scripts/deploy.sh [--dry-run]

Safe deployment rules:
  * Reuse the Docker daemon that is already active.
  * Never install/start/stop/upgrade Docker.
  * Never switch docker.sock or Docker data-root.
  * Never run docker system prune or compose down -v.
  * Operate only on Compose project wx-video-download.
HELP
      exit 0;;
    *) echo "ERROR: unknown option: $arg" >&2; exit 2;;
  esac
done

PROJECT_NAME="${COMPOSE_PROJECT_NAME:-wx-video-download}"
APP_PORT="${APP_PORT:-}"
BROWSERLESS_IMAGE="${BROWSERLESS_IMAGE:-}"

read_env_value() {
  local key="$1" file="$PROJECT_DIR/.env"
  [ -f "$file" ] || return 0
  awk -v k="$key" 'index($0,k"=")==1 {sub("^[^=]*=",""); print; exit}' "$file"
}

if [ -z "$APP_PORT" ]; then
  APP_PORT="$(read_env_value APP_PORT || true)"
fi
APP_PORT="${APP_PORT:-18770}"

require_existing_docker
require_compose
validate_project_path_for_docker "$PROJECT_DIR"

# Explicit external Browserless settings win. Otherwise auto-detect a running
# Browserless published on the current Docker host and reuse it without restart.
if [ -z "${BROWSERLESS_URL:-}" ]; then
  BROWSERLESS_URL="$(read_env_value BROWSERLESS_URL || true)"
fi
if [ -z "${BROWSERLESS_IMAGE:-}" ]; then
  BROWSERLESS_IMAGE="$(read_env_value BROWSERLESS_IMAGE || true)"
  BROWSERLESS_IMAGE="${BROWSERLESS_IMAGE:-browserless/chrome@sha256:57d19e414d9fe4ae9d2ab12ba768c97f38d51246c5b31af55a009205c136012f}"
fi

USE_EXTERNAL_BROWSERLESS=0
BROWSERLESS_REQUIRED_TIMEOUT_MS="${BROWSERLESS_REQUIRED_TIMEOUT_MS:-180000}"
if [ -n "${BROWSERLESS_URL:-}" ] && [ "$BROWSERLESS_URL" != "http://browserless:3000" ]; then
  # Explicit external URL is an operator choice; do not mutate that Browserless.
  USE_EXTERNAL_BROWSERLESS=1
else
  detect_running_browserless "$BROWSERLESS_REQUIRED_TIMEOUT_MS"
  if [ -n "$RUNNING_BROWSERLESS_URL" ] && [ "$RUNNING_BROWSERLESS_COMPATIBLE" = "yes" ]; then
    BROWSERLESS_URL="$RUNNING_BROWSERLESS_URL"
    BROWSERLESS_TOKEN="$RUNNING_BROWSERLESS_TOKEN"
    export BROWSERLESS_URL BROWSERLESS_TOKEN
    USE_EXTERNAL_BROWSERLESS=1
  elif [ -n "$RUNNING_BROWSERLESS_NAME" ]; then
    echo "Existing Browserless '$RUNNING_BROWSERLESS_NAME' is running but its CONNECTION_TIMEOUT is below ${BROWSERLESS_REQUIRED_TIMEOUT_MS}ms (or unknown)." >&2
    echo "It will NOT be modified or restarted. A project-local Browserless container will be used instead." >&2
    if [ -n "$RUNNING_BROWSERLESS_IMAGE" ]; then
      BROWSERLESS_IMAGE="$RUNNING_BROWSERLESS_IMAGE"
      export BROWSERLESS_IMAGE
    fi
  fi
fi

BROWSERLESS_IMAGE_STATE="external"
if [ "$USE_EXTERNAL_BROWSERLESS" -eq 0 ]; then
  if docker image inspect "$BROWSERLESS_IMAGE" >/dev/null 2>&1; then
    BROWSERLESS_IMAGE_STATE="present"
  else
    BROWSERLESS_IMAGE_STATE="missing"
  fi
fi

printf '%s\n' '=== wx-video-download safe preflight ==='
printf 'Docker state      : %s\n' "$DOCKER_STATE"
printf 'Docker binary     : %s\n' "$DOCKER_BIN"
printf 'Docker server     : %s\n' "${DOCKER_VERSION:-unknown}"
printf 'Docker root       : %s\n' "${DOCKER_ROOT:-unknown}"
printf 'Docker flavor     : %s\n' "$DOCKER_FLAVOR"
printf 'Compose           : %s (%s)\n' "$COMPOSE_KIND" "${COMPOSE_VERSION:-unknown}"
printf 'Compose project   : %s\n' "$PROJECT_NAME"
printf 'Application port  : %s\n' "$APP_PORT"
if [ "$USE_EXTERNAL_BROWSERLESS" -eq 1 ]; then
  if [ -n "${RUNNING_BROWSERLESS_NAME:-}" ] && [ "${RUNNING_BROWSERLESS_COMPATIBLE:-}" = "yes" ]; then
    printf 'Browserless       : reuse compatible running container %s (%s)\n' "$RUNNING_BROWSERLESS_NAME" "$BROWSERLESS_URL"
  else
    printf 'Browserless       : external (%s)\n' "$BROWSERLESS_URL"
  fi
elif [ -n "${RUNNING_BROWSERLESS_NAME:-}" ] && [ "${RUNNING_BROWSERLESS_COMPATIBLE:-}" = "no" ]; then
  printf 'Browserless       : existing %s is incompatible; use isolated project container with local image %s\n' "$RUNNING_BROWSERLESS_NAME" "$BROWSERLESS_IMAGE"
elif [ "$BROWSERLESS_IMAGE_STATE" = "present" ]; then
  printf 'Browserless image : present (%s)\n' "$BROWSERLESS_IMAGE"
else
  printf 'Browserless image : missing (will pull this image only) (%s)\n' "$BROWSERLESS_IMAGE"
fi

# Refuse to steal a port from an unrelated running container.
PORT_OWNER="$(docker ps --format '{{.Names}}|{{.Ports}}' 2>/dev/null | awk -F'|' -v p=":${APP_PORT}->" 'index($2,p){print $1; exit}')"
if [ -n "$PORT_OWNER" ]; then
  case "$PORT_OWNER" in
    ${PROJECT_NAME}-*|${PROJECT_NAME}_*)
      echo "Port $APP_PORT is already owned by this Compose project ($PORT_OWNER); redeploy is allowed.";;
    *)
      echo "ERROR: port $APP_PORT is already used by unrelated container: $PORT_OWNER" >&2
      exit 30;;
  esac
fi

COMPOSE=("${COMPOSE_CMD[@]}" -p "$PROJECT_NAME" -f "$PROJECT_DIR/docker-compose.yml")
if [ "$USE_EXTERNAL_BROWSERLESS" -eq 0 ]; then
  COMPOSE+=( -f "$PROJECT_DIR/docker-compose.browserless.yml" )
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo
  echo 'DRY RUN: no container/image/service changes were made.'
  printf 'Would validate: '; printf '%q ' "${COMPOSE[@]}" config; echo
  if [ "$USE_EXTERNAL_BROWSERLESS" -eq 0 ] && [ "$BROWSERLESS_IMAGE_STATE" = "missing" ]; then
    printf 'Would pull only: '; printf '%q ' "${COMPOSE[@]}" pull browserless; echo
  fi
  printf 'Would deploy:   '; printf '%q ' "${COMPOSE[@]}" up -d --build; echo
  exit 0
fi

mkdir -p "$PROJECT_DIR/.deploy-baseline"
STAMP="$(date +%Y%m%d-%H%M%S)"
BASE="$PROJECT_DIR/.deploy-baseline/$STAMP"
mkdir -p "$BASE"

docker ps -a --format '{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}' | sort > "$BASE/containers.before.txt"
docker ps --format '{{.Names}}' | sort > "$BASE/running.before.txt"
docker network ls --format '{{.Name}}|{{.Driver}}' | sort > "$BASE/networks.before.txt"
docker volume ls --format '{{.Name}}' | sort > "$BASE/volumes.before.txt"
printf '%s\n' "$DOCKER_BIN" > "$BASE/docker-bin.txt"
printf '%s\n' "$DOCKER_VERSION" > "$BASE/docker-version.txt"
printf '%s\n' "$DOCKER_ROOT" > "$BASE/docker-root.txt"
printf '%s\n' "${COMPOSE_CMD[*]}" > "$BASE/compose-command.txt"

"${COMPOSE[@]}" config >/dev/null
if [ "$USE_EXTERNAL_BROWSERLESS" -eq 0 ] && [ "$BROWSERLESS_IMAGE_STATE" = "missing" ]; then
  echo "Browserless image is not present in the CURRENT Docker daemon; pulling only $BROWSERLESS_IMAGE ..."
  "${COMPOSE[@]}" pull browserless
fi

# 'up' is project-scoped. We intentionally never call global Docker service commands.
"${COMPOSE[@]}" up -d --build

docker ps -a --format '{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}' | sort > "$BASE/containers.after.txt"
docker ps --format '{{.Names}}' | sort > "$BASE/running.after.txt"

# Every container that was running before deployment, except an older copy of this
# same project, must still be running afterwards.
missing=0
while IFS= read -r name; do
  [ -n "$name" ] || continue
  case "$name" in ${PROJECT_NAME}-*|${PROJECT_NAME}_*) continue;; esac
  if ! grep -Fxq "$name" "$BASE/running.after.txt"; then
    echo "ERROR: pre-existing container is no longer running after deployment: $name" >&2
    missing=1
  fi
done < "$BASE/running.before.txt"
if [ "$missing" -ne 0 ]; then
  echo "Safety verification failed. No automatic restart/cleanup of other projects will be attempted." >&2
  exit 40
fi

echo "Deployment finished without stopping pre-existing containers."
echo "Baseline: $BASE"
"${COMPOSE[@]}" ps
