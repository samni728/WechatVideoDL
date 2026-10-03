#!/usr/bin/env bash
# Shared Docker/Compose compatibility detection.
# This file is deliberately READ-ONLY: it never starts/stops/installs Docker.

DOCKER_STATE="unknown"
DOCKER_BIN=""
DOCKER_ROOT=""
DOCKER_VERSION=""
DOCKER_FLAVOR="unknown"
COMPOSE_KIND="missing"
COMPOSE_VERSION=""
COMPOSE_CMD=()

_detect_server_version() {
  docker version --format '{{.Server.Version}}' 2>/dev/null || true
}

detect_docker() {
  DOCKER_STATE="missing"
  DOCKER_BIN=""
  DOCKER_ROOT=""
  DOCKER_VERSION=""
  DOCKER_FLAVOR="unknown"

  if ! command -v docker >/dev/null 2>&1; then
    return 0
  fi

  DOCKER_BIN="$(command -v docker)"
  if ! docker info >/dev/null 2>&1; then
    DOCKER_STATE="unreachable"
    return 0
  fi

  DOCKER_STATE="ready"
  DOCKER_ROOT="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
  DOCKER_VERSION="$(_detect_server_version)"

  case "$DOCKER_ROOT:$DOCKER_BIN" in
    /var/snap/docker/*:*|*:/snap/bin/docker) DOCKER_FLAVOR="snap" ;;
    /var/lib/docker:/usr/bin/docker|/var/lib/docker:/usr/local/bin/docker) DOCKER_FLAVOR="system" ;;
    *) DOCKER_FLAVOR="unknown" ;;
  esac
}

detect_compose() {
  COMPOSE_KIND="missing"
  COMPOSE_VERSION=""
  COMPOSE_CMD=()

  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE_KIND="plugin"
    COMPOSE_CMD=(docker compose)
    COMPOSE_VERSION="$(docker compose version 2>/dev/null | head -n 1 || true)"
    return 0
  fi

  if command -v docker-compose >/dev/null 2>&1 && docker-compose version >/dev/null 2>&1; then
    COMPOSE_KIND="legacy"
    COMPOSE_CMD=(docker-compose)
    COMPOSE_VERSION="$(docker-compose version 2>/dev/null | head -n 1 || true)"
    return 0
  fi
}


RUNNING_BROWSERLESS_NAME=""
RUNNING_BROWSERLESS_URL=""
RUNNING_BROWSERLESS_TOKEN=""
RUNNING_BROWSERLESS_PORT=""
RUNNING_BROWSERLESS_COMPATIBLE=""
RUNNING_BROWSERLESS_IMAGE=""

detect_running_browserless() {
  local required_timeout_ms="${1:-180000}"
  RUNNING_BROWSERLESS_NAME=""
  RUNNING_BROWSERLESS_URL=""
  RUNNING_BROWSERLESS_TOKEN=""
  RUNNING_BROWSERLESS_PORT=""
  RUNNING_BROWSERLESS_COMPATIBLE=""
  RUNNING_BROWSERLESS_IMAGE=""

  command -v docker >/dev/null 2>&1 || return 0

  local name image ports port env_lines token timeout_ms image_ref
  while IFS='|' read -r name image ports; do
    [ -n "$name" ] || continue
    case "$image" in
      browserless/chrome*) ;;
      *) continue;;
    esac

    # Automatic reuse is safe only when Browserless publishes container port
    # 3000 on a host address reachable from another Docker container.
    port="$(printf '%s' "$ports" | sed -nE 's#.*0\.0\.0\.0:([0-9]+)->3000/tcp.*#\1#p' | head -n 1)"
    if [ -z "$port" ]; then
      port="$(printf '%s' "$ports" | sed -nE 's#.*\[::\]:([0-9]+)->3000/tcp.*#\1#p' | head -n 1)"
    fi
    [ -n "$port" ] || continue

    env_lines="$(docker inspect "$name" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null || true)"
    token="$(printf '%s\\n' "$env_lines" | sed -n 's/^TOKEN=//p' | head -n 1)"
    if [ -z "$token" ]; then
      token="$(printf '%s\\n' "$env_lines" | sed -n 's/^BROWSERLESS_TOKEN=//p' | head -n 1)"
    fi

    timeout_ms="$(printf '%s\n' "$env_lines" | sed -n 's/^CONNECTION_TIMEOUT=//p' | head -n 1)"
    image_ref="$(docker inspect "$name" --format '{{.Config.Image}}' 2>/dev/null || true)"

    RUNNING_BROWSERLESS_NAME="$name"
    RUNNING_BROWSERLESS_PORT="$port"
    RUNNING_BROWSERLESS_URL="http://host.docker.internal:$port"
    RUNNING_BROWSERLESS_TOKEN="$token"
    RUNNING_BROWSERLESS_IMAGE="${image_ref:-$image}"
    if [[ "$timeout_ms" =~ ^[0-9]+$ ]] && [[ "$required_timeout_ms" =~ ^[0-9]+$ ]] && [ "$timeout_ms" -ge "$required_timeout_ms" ]; then
      RUNNING_BROWSERLESS_COMPATIBLE="yes"
    else
      RUNNING_BROWSERLESS_COMPATIBLE="no"
    fi
    return 0
  done < <(docker ps --format '{{.Names}}|{{.Image}}|{{.Ports}}' 2>/dev/null || true)
}


validate_project_path_for_docker() {
  local project_dir="${1:-}"
  [ -n "$project_dir" ] || { echo "ERROR: project path is empty" >&2; return 31; }
  if [ "$DOCKER_FLAVOR" = "snap" ]; then
    case "$project_dir" in
      /opt|/opt/*)
        echo "ERROR: Snap Docker cannot reliably bind-mount project data from $project_dir" >&2
        echo "Move this project under /root, /home, or /var/snap/docker/common and retry." >&2
        return 31
        ;;
    esac
  fi
  return 0
}

require_existing_docker() {
  detect_docker
  case "$DOCKER_STATE" in
    ready) return 0 ;;
    missing)
      echo "ERROR: Docker is not installed." >&2
      echo "Run the optional Docker installer only on a machine that truly has no Docker installation." >&2
      return 10
      ;;
    unreachable)
      echo "ERROR: A Docker CLI exists, but its daemon/socket is unreachable." >&2
      echo "Safety stop: this project will NOT install or start another Docker daemon." >&2
      return 11
      ;;
    *) return 12 ;;
  esac
}

require_compose() {
  detect_compose
  if [ "$COMPOSE_KIND" = "missing" ]; then
    echo "ERROR: Neither 'docker compose' nor 'docker-compose' is available." >&2
    return 20
  fi
}
