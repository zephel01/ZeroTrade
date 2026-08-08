#!/usr/bin/env bash
# 前向き検証を銘柄ぶんまとめて起動する。**発注は外へ出ない**（シャドー実行）。
#
#   scripts/forward_start.sh            # config/forward/*.yaml を起動
#   scripts/forward_start.sh forward2   # config/forward2/*.yaml を起動
#
# 別の戦略を検証するときは config/<group>/ を作って第1引数で指定する。
# **稼働中の検証に新しい戦略を混ぜてはいけない。** 集めている件数の意味が
# 変わり、検証がやり直しになる。グループごと分けること。
#
# 既に動いているものは起動しない（二重起動すると同じ記録DBを
# 2プロセスが書き、トレードが重複して数えられる）。
set -euo pipefail
cd "$(dirname "$0")/.."

GROUP="${1:-forward}"
CONFIG_DIR="config/${GROUP}"

if [[ ! -d "$CONFIG_DIR" ]]; then
  echo "設定ディレクトリがありません: ${CONFIG_DIR}" >&2
  exit 1
fi
if [[ -z "${BINGX_API_KEY:-}" || -z "${BINGX_API_SECRET:-}" ]]; then
  echo "BINGX_API_KEY と BINGX_API_SECRET を export してください" >&2
  exit 1
fi

mkdir -p logs
for config in "$CONFIG_DIR"/*.yaml; do
  name="$(basename "$config" .yaml)"
  if pgrep -f "${CONFIG_DIR}/${name}.yaml run" >/dev/null 2>&1; then
    echo "既に動いています: ${GROUP}/${name}"
    continue
  fi
  nohup zerotrade -c "$config" run > "logs/${GROUP}-${name}.out" 2>&1 &
  echo "起動しました: ${GROUP}/${name} (pid $!)"
  sleep 2   # 接続が同時に殺到しないよう少しずらす
done

echo
echo "現況: python3 scripts/forward_watch.py --group ${GROUP}"
echo "判定: python3 scripts/forward_judge.py --group ${GROUP}"
