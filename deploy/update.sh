#!/bin/bash
# Stáhne změny z GitHubu; když nějaké jsou, znovu sestaví a spustí aplikaci.
set -e
cd /opt/glukoradce
git fetch -q origin
LOCAL=$(git rev-parse HEAD); REMOTE=$(git rev-parse @{u})
if [ "$LOCAL" != "$REMOTE" ]; then
  echo "$(date) aktualizace $LOCAL -> $REMOTE"
  git reset -q --hard "$REMOTE"
  docker compose up -d --build
  docker image prune -f >/dev/null
fi
