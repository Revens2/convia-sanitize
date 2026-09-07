#!/usr/bin/env bash
# deploy.sh — Déploie convia-sanitize du dépôt Git vers /opt/convia-sanitize.
#
# Usage : bash deploy/deploy.sh [host]        (host par défaut : vps-etude-nb)
#
# Depuis un working tree PROPRE :
#   1. tests pytest
#   2. staging des fichiers runtime (jamais .git, tests, caches, secrets)
#   3. backup cible dans /var/backups/convia-sanitize/<ts>/ (rétention 2)
#   4. installation atomique (install -o convia -g convia -m 0644)
#   5. écriture de .deployed-commit (SHA git HEAD) et .deployed-at (ISO)
#   6. py_compile + affichage de `runner.py --version`
#
# Les configs sensibles (rclone.conf, /etc/default/telegram_notify) restent hors
# Git et ne sont JAMAIS touchées. Les unités systemd ne sont pas modifiées ici :
# voir systemd/README.md (déployées séparément, rarement).
set -euo pipefail

HOST="${1:-vps-etude-nb}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -n "$(git status --porcelain)" ]; then
    echo "ERREUR: working tree non propre" >&2
    exit 1
fi
SHA="$(git rev-parse HEAD)"
TS="$(date -u +%Y%m%d-%H%M%S)"

echo "== deploy $SHA -> $HOST =="
python3 -m pytest -q tests/

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp runner.py sanitizer.py redact.py publish.py "$STAGE/"
printf '%s\n' "$SHA" > "$STAGE/.deployed-commit"
date -u +%Y-%m-%dT%H:%M:%SZ > "$STAGE/.deployed-at"

echo "== backup cible ($TS) =="
ssh "$HOST" "sudo mkdir -p /var/backups/convia-sanitize"
ssh "$HOST" "sudo cp -a /opt/convia-sanitize /var/backups/convia-sanitize/$TS"
# rétention : ne conserver que les 2 sauvegardes les plus récentes
ssh "$HOST" "sudo sh -c 'ls -1d /var/backups/convia-sanitize/* 2>/dev/null | sort | head -n -2 | xargs -r rm -rf'"

echo "== copie staging =="
ssh "$HOST" "mkdir -p /tmp/convia-deploy"
scp -q "$STAGE/runner.py" "$STAGE/sanitizer.py" "$STAGE/redact.py" \
     "$STAGE/publish.py" "$STAGE/.deployed-commit" "$STAGE/.deployed-at" \
     "$HOST:/tmp/convia-deploy/"

echo "== installation (/opt/convia-sanitize) =="
ssh "$HOST" "sudo install -o convia -g convia -m 0644 /tmp/convia-deploy/runner.py /tmp/convia-deploy/sanitizer.py /tmp/convia-deploy/redact.py /tmp/convia-deploy/publish.py /tmp/convia-deploy/.deployed-commit /tmp/convia-deploy/.deployed-at /opt/convia-sanitize/"
ssh "$HOST" "rm -rf /tmp/convia-deploy"

echo "== verification =="
ssh "$HOST" "sudo -u convia python3 -m py_compile /opt/convia-sanitize/runner.py /opt/convia-sanitize/publish.py"
ssh "$HOST" "sudo -u convia python3 /opt/convia-sanitize/runner.py --version"

echo "== deploy OK ($SHA) =="
