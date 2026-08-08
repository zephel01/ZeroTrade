#!/usr/bin/env bash
# 前向き検証の生存確認。落ちていれば forward_start.sh で再開できる。
#
#   scripts/forward_status.sh            # config/forward/
#   scripts/forward_status.sh forward2   # config/forward2/
cd "$(dirname "$0")/.."

GROUP="${1:-forward}"
CONFIG_DIR="config/${GROUP}"
alive=0
total=0

for config in "$CONFIG_DIR"/*.yaml; do
  [ -e "$config" ] || continue
  name="$(basename "$config" .yaml)"
  total=$((total + 1))
  if pid="$(pgrep -f "${CONFIG_DIR}/${name}.yaml run" | head -1)"; then
    last="$(tail -1 "logs/${GROUP}-${name}.out" 2>/dev/null | cut -c1-70)"
    printf "  稼働  %-6s pid %-8s %s\n" "$name" "$pid" "$last"
    alive=$((alive + 1))
  else
    printf "  停止  %-6s （scripts/forward_start.sh %s で再開できます）\n" "$name" "$GROUP"
  fi
done

echo
echo "稼働 ${alive}/${total}"
