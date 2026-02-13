#!/usr/bin/env bash
set -e

echo "=== MeshCore Repeater Dashboard — Setup ==="
echo ""

# Check Docker is available
if ! command -v docker &>/dev/null; then
  echo "ERROR: Docker is not installed."
  echo "Install Docker first: https://docs.docker.com/engine/install/"
  exit 1
fi

# Check Docker Compose v2
if ! docker compose version &>/dev/null 2>&1; then
  echo "ERROR: Docker Compose v2 not found (need 'docker compose', not 'docker-compose')."
  echo "Update Docker Desktop or install the Compose plugin."
  exit 1
fi

# Create data directory and placeholder files.
# docker-compose bind-mounts these as FILES — if they don't exist Docker
# creates them as directories, which breaks the app.
mkdir -p data
if [ ! -f data/settings.json ]; then
  echo '{}' > data/settings.json
  echo "Created data/settings.json"
fi
if [ ! -f data/repeater_history.db ]; then
  touch data/repeater_history.db
  echo "Created data/repeater_history.db"
fi

# Build image and start container
echo ""
echo "Building and starting container..."
docker compose up --build -d

echo ""
echo "Done! Dashboard is running at:"
echo "  http://$(hostname -I | awk '{print $1}'):8080"
echo ""
echo "Open that URL, click the gear icon, and enter your companion device IP."
