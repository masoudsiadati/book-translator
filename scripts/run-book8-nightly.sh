#!/usr/bin/env bash
set -uo pipefail

export HOME=/root
export PATH="/root/.local/bin:/root/.nvm/versions/node/v22.23.1/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

cd /root/Projects/book-translator
source .venv/bin/activate

mkdir -p logs

echo "===== $(date -Is) nightly start =====" >> logs/book8-nightly.log
echo "codex=$(command -v codex || true)" >> logs/book8-nightly.log
echo "node=$(command -v node || true)" >> logs/book8-nightly.log

.venv/bin/book-translator \
  run-nightly-translation tr-473bab4ea0f7-001 \
  --max-units 3 \
  >> logs/book8-nightly.log 2>&1

rc=$?

echo "===== $(date -Is) nightly end rc=$rc =====" >> logs/book8-nightly.log
exit "$rc"
