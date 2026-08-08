#!/usr/bin/env bash
# 前向き検証を6銘柄ぶん起動する。**発注は外へ出ない**（シャドー実行）。
#
#   scripts/forward_start.sh
#
# 既に動いているものは起動しない（二重起動すると同じ記録DBを
# 2プロセスが書き、トレードが重複して数えられる）。
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -z "${BINGX_API_KEY:-}" || -z "${BINGX_API_SECRET:-}" ]]; then
  echo "BINGX_API_KEY と BINGX_API_SECRET を export してください" >&2
  exit 1
fi

mkdir -p logs
for config in config/forward/*.yaml; do
  name="$(basename "$config" .yaml)"
  if pgrep -f "config/forward/${name}.yaml run" >/dev/null 2>&1; then
    echo "既に動いています: ${name}"
    continue
  fi
  nohup zerotrade -c "$config" run > "logs/forward-${name}.out" 2>&1 &
  echo "起動しました: ${name} (pid $!)"
  sleep 2   # 接続が同時に殺到しないよう少しずらす
done

echo
echo "生存確認: scripts/forward_status.sh"
echo "進捗確認: python3 scripts/forward_judge.py"
