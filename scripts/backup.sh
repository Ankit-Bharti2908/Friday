#!/usr/bin/env bash
# Nightly backup: the entire brain is this folder. Cron example:
#   30 2 * * * /path/to/friday/scripts/backup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
DEST="${FRIDAY_BACKUP_DIR:-$HOME/backups/friday}"
mkdir -p "$DEST"
STAMP=$(date +%Y%m%d)
zip -rq "$DEST/friday-$STAMP.zip" . \
  -x ".venv/*" "__pycache__/*" "*/__pycache__/*" "bridge/whatsapp/node_modules/*" ".phoenix/*"
ls -t "$DEST"/friday-*.zip | tail -n +15 | xargs -r rm --   # keep last 14
echo "backup written: $DEST/friday-$STAMP.zip"
