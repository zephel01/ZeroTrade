#!/usr/bin/env bash
# 前向き検証6本の生存確認。落ちているものがあれば forward_start.sh で再開できる。
cd "$(dirname "$0")/.."

alive=0
for config in config/forward/*.yaml; do
  name="$(basename "$config" .yaml)"
  if pid="$(pgrep -f "config/forward/${name}.yaml run" | head -1)"; then
    last="$(tail -1 "logs/forward-${name}.out" 2>/dev/null | cut -c1-70)"
    printf "  稼働  %-6s pid %-8s %s\n" "$name" "$pid" "$last"
    alive=$((alive + 1))
  else
    printf "  停止  %-6s （scripts/forward_start.sh で再開できます）\n" "$name"
  fi
done
echo
echo "稼働 ${alive}/6"
