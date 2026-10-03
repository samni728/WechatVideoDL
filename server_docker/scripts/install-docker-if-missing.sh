#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/docker_compat.sh
source "$SCRIPT_DIR/lib/docker_compat.sh"

detect_docker
case "$DOCKER_STATE" in
  ready)
    echo "Existing Docker is healthy; reusing it."
    echo "Docker binary: $DOCKER_BIN"
    echo "Docker root: $DOCKER_ROOT"
    echo "Docker flavor: $DOCKER_FLAVOR"
    detect_compose
    if [ "$COMPOSE_KIND" != "missing" ]; then
      echo "Compose: $COMPOSE_VERSION"
    else
      echo "WARN: Docker is healthy but Compose is missing. Install Compose separately; Docker itself will not be replaced." >&2
    fi
    exit 0
    ;;
  unreachable)
    echo "ERROR: Docker CLI is installed but the daemon/socket is unreachable." >&2
    echo "Refusing to install a second Docker daemon. Fix the existing Docker service/socket first." >&2
    exit 11
    ;;
  missing) ;;
  *) echo "ERROR: unknown Docker state: $DOCKER_STATE" >&2; exit 12;;
esac

# Extra safety: command lookup can fail because PATH is unusual. Before installing,
# check common locations and package/service traces so we do not create a second daemon.
shadow=0
for candidate in /snap/bin/docker /usr/bin/docker /usr/local/bin/docker; do
  if [ -x "$candidate" ]; then
    echo "ERROR: Docker binary exists outside current PATH: $candidate" >&2
    shadow=1
  fi
done
if command -v snap >/dev/null 2>&1 && snap list docker >/dev/null 2>&1; then
  echo "ERROR: Snap Docker is installed but was not found in PATH." >&2
  shadow=1
fi
if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files docker.service >/dev/null 2>&1; then
  if systemctl list-unit-files docker.service 2>/dev/null | grep -q '^docker\.service'; then
    echo "ERROR: docker.service already exists on this machine." >&2
    shadow=1
  fi
fi
if [ "$shadow" -ne 0 ]; then
  echo "Refusing to install another Docker. Restore/fix the existing installation instead." >&2
  exit 13
fi

if [ "${1:-}" != "--install" ]; then
  echo "Docker is genuinely missing. No changes were made."
  echo "Re-run with --install only if you want this script to install Docker on a clean Ubuntu/Debian host."
  exit 10
fi

if [ "$(id -u)" -eq 0 ]; then SUDO=();
elif command -v sudo >/dev/null 2>&1; then SUDO=(sudo);
else echo "ERROR: root/sudo is required to install Docker." >&2; exit 14; fi

if [ ! -r /etc/os-release ]; then
  echo "ERROR: unsupported OS; install Docker manually." >&2
  exit 15
fi
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in
  ubuntu|debian) ;;
  *) echo "ERROR: automatic install is limited to Ubuntu/Debian. Install Docker manually." >&2; exit 15;;
esac

# Use distribution packages only. We intentionally do not add Docker CE repositories.
"${SUDO[@]}" apt-get update
"${SUDO[@]}" apt-get install -y docker.io
if apt-cache show docker-compose-v2 >/dev/null 2>&1; then
  "${SUDO[@]}" apt-get install -y docker-compose-v2
elif apt-cache show docker-compose-plugin >/dev/null 2>&1; then
  "${SUDO[@]}" apt-get install -y docker-compose-plugin
else
  "${SUDO[@]}" apt-get install -y docker-compose
fi
"${SUDO[@]}" systemctl enable --now docker

echo "Docker was installed because no previous Docker installation/daemon was detected."
docker --version
docker info >/dev/null
detect_compose
[ "$COMPOSE_KIND" != "missing" ] || { echo "ERROR: Docker installed but Compose is still missing." >&2; exit 20; }
echo "$COMPOSE_VERSION"
