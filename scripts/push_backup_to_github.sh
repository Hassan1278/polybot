#!/usr/bin/env bash
# =============================================================================
# scripts/push_backup_to_github.sh — push a single dump to a private GitHub repo
#
# This script is called automatically by scripts/backup.sh after a successful
# pg_dump, IF GITHUB_BACKUP_TOKEN + GITHUB_BACKUP_REPO are set in .env.
#
# Required env:
#   GITHUB_BACKUP_TOKEN   personal access token with `repo` scope on the target
#   GITHUB_BACKUP_REPO    owner/repo, e.g. "Hassan1278/polybot-backups"
# Optional:
#   GITHUB_BACKUP_KEEP    how many dumps to keep in the repo (default: 7)
#   GITHUB_BACKUP_BRANCH  branch to push to (default: main)
#
# Usage:
#   bash push_backup_to_github.sh /path/to/pg-2026-06-07.sql.gz
#
# Strategy:
#   - Clone the backups repo into /tmp (depth=1, blobless to keep it small).
#   - Copy today's dump into the clone.
#   - Delete dumps older than the N most recent (LRU rotation).
#   - git add + commit + push.
#
# GitHub private repo soft limit is ~1 GB. With ~20 MB/dump × 7 days = ~140 MB
# we have plenty of headroom. Set GITHUB_BACKUP_KEEP higher if you've got
# tiny dumps and want more history.
# =============================================================================
set -euo pipefail

DUMP="${1:-}"
if [[ -z "$DUMP" || ! -f "$DUMP" ]]; then
  echo "usage: $0 /path/to/pg-YYYY-MM-DD.sql.gz" >&2
  exit 2
fi
if [[ -z "${GITHUB_BACKUP_TOKEN:-}" || -z "${GITHUB_BACKUP_REPO:-}" ]]; then
  echo "GITHUB_BACKUP_TOKEN + GITHUB_BACKUP_REPO must be set" >&2
  exit 2
fi

KEEP="${GITHUB_BACKUP_KEEP:-7}"
BRANCH="${GITHUB_BACKUP_BRANCH:-main}"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

REPO_URL="https://${GITHUB_BACKUP_TOKEN}@github.com/${GITHUB_BACKUP_REPO}.git"

echo "[gh-backup] cloning ${GITHUB_BACKUP_REPO}#${BRANCH} into ${WORKDIR}"
git clone --quiet --depth=1 --branch "$BRANCH" "$REPO_URL" "$WORKDIR/repo" 2>/dev/null || {
  # First-time bootstrap: empty repo with no commits yet
  echo "[gh-backup] clone failed — initialising fresh repo"
  mkdir -p "$WORKDIR/repo"
  cd "$WORKDIR/repo"
  git init -q -b "$BRANCH"
  git remote add origin "$REPO_URL"
  echo "# Polybot DB backups" > README.md
  git add README.md
  git -c user.name=polybot -c user.email=polybot@local commit -q -m "init"
}

cd "$WORKDIR/repo"
git config user.name "polybot-backup"
git config user.email "polybot-backup@local"

DUMP_NAME="$(basename "$DUMP")"
cp "$DUMP" "./${DUMP_NAME}"

# LRU rotation: keep the N most recent dumps (by mtime), remove the rest.
# Find all dump files, sort newest first, skip the first N, delete the rest.
ls -1t pg-*.sql.gz 2>/dev/null | tail -n "+$((KEEP + 1))" | while read -r old; do
    echo "[gh-backup] removing old dump: $old"
    git rm -q "$old" 2>/dev/null || rm -f "$old"
done

git add "${DUMP_NAME}"
if git diff --cached --quiet; then
    echo "[gh-backup] no changes to commit (dump already present)"
    exit 0
fi
git commit -q -m "backup $(date -u +%FT%TZ) — $(du -h "${DUMP_NAME}" | cut -f1)"
git push -q origin "$BRANCH"
echo "[gh-backup] pushed ${DUMP_NAME} to ${GITHUB_BACKUP_REPO}#${BRANCH}"
