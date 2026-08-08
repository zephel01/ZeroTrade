#!/usr/bin/env bash
# 前向き検証をまとめて止める。**強制終了はしない。**
#
#   scripts/forward_stop.sh            # config/forward/*.yaml を止める
#   scripts/forward_stop.sh forward2   # config/forward2/*.yaml を止める
#   scripts/forward_stop.sh forward "PC再起動のため"
#
# 各プロセスの state_dir にキルスイッチ（STOP ファイル）を置く。取引ループは
# **次のループ境界で**それを見て、未約定注文の取消・リスク状態の保存・切断まで
# 済ませてから終了する。poll_interval_seconds が 60 なら最大1分ほどかかる。
#
# **停止要求はプロセスの生存確認に依存させない。** pgrep が取りこぼしても
# STOP さえ置いてあれば取引ループは必ず止まる。逆に生存確認を条件にすると、
# 取りこぼした瞬間に「止めたつもりで動き続けている」状態になる。
# 止まっているプロセスに STOP を置いても害はない（起動時に消える）。
#
# 建玉は閉じない（意図的）。停止のたびに全決済すると、ネットワーク断で
# 再起動しただけで意図しない損失確定が起きる。シャドー実行の建玉と残高は
# state/<group>/<symbol>/shadow_state.json に残り、再開時に読み直される。
#
# 再開は forward_start.sh。起動時に STOP ファイルは自動で消える
# （起動という操作そのものが再開の意思表示なので）。
set -euo pipefail
cd "$(dirname "$0")/.."

GROUP="${1:-forward}"
CONFIG_DIR="config/${GROUP}"
REASON="${2:-scripts/forward_stop.sh による停止}"

#: 停止を待つ上限（秒）。poll_interval_seconds の2倍強を見ておく。
TIMEOUT=150

if [[ ! -d "$CONFIG_DIR" ]]; then
  echo "設定ディレクトリがありません: ${CONFIG_DIR}" >&2
  exit 1
fi

running_before=0
targets=()

for config in "$CONFIG_DIR"/*.yaml; do
  [ -e "$config" ] || continue
  name="$(basename "$config" .yaml)"
  targets+=("$name")

  if pgrep -f "${CONFIG_DIR}/${name}.yaml run" >/dev/null 2>&1; then
    state="稼働中"
    running_before=$((running_before + 1))
  else
    state="プロセス未検出"
  fi

  # 生存確認の結果にかかわらず必ず要求する。
  # 失敗したら黙って進まない。「止めたつもり」が最悪の状態なので必ず落とす。
  if ! output="$(zerotrade -c "$config" stop --reason "$REASON" 2>&1)"; then
    echo "  停止の要求に失敗  ${name}" >&2
    echo "$output" >&2
    exit 1
  fi
  printf "  停止を要求  %-6s （%s）\n" "$name" "$state"
done

if [[ "${#targets[@]}" -eq 0 ]]; then
  echo "設定ファイルがありません: ${CONFIG_DIR}/*.yaml" >&2
  exit 1
fi

echo
if [[ "$running_before" -eq 0 ]]; then
  echo "動いているプロセスは見つかりませんでした（STOP は置いてあります）。"
  echo "再開: scripts/forward_start.sh ${GROUP}"
  exit 0
fi

echo "稼働 ${running_before} 本。次のループ境界まで待ちます（最大 ${TIMEOUT} 秒）..."

waited=0
while :; do
  alive=0
  for name in "${targets[@]}"; do
    if pgrep -f "${CONFIG_DIR}/${name}.yaml run" >/dev/null 2>&1; then
      alive=$((alive + 1))
    fi
  done

  if [[ "$alive" -eq 0 ]]; then
    echo "すべて停止しました（${waited}秒）。建玉は保持されています。"
    echo "現況: python3 scripts/forward_watch.py --group ${GROUP}"
    echo "再開: scripts/forward_start.sh ${GROUP}"
    exit 0
  fi
  if [[ "$waited" -ge "$TIMEOUT" ]]; then
    break
  fi
  sleep 5
  waited=$((waited + 5))
done

echo "残り ${alive} 本がまだ止まっていません。" >&2
echo "STOP は置いてあるので、ブローカーの応答待ちが終われば止まります。確認:" >&2
echo "  scripts/forward_status.sh ${GROUP}" >&2
echo "急ぐ場合は SIGTERM を送ってください（これも安全に閉じます）:" >&2
echo "  pkill -TERM -f '${CONFIG_DIR}/.*\\.yaml run'" >&2
exit 1
