#!/bin/sh
# 契网 agent 集成安装脚本 —— 在任何装了 python3 与 qinetwork 的机器上跑
#
#   ./install.sh                  # 装 CLI + MCP server 到 ~/.local/bin
#   ./install.sh --mcp codex      # 装完后顺便注册到 codex/claude/pi/qwen
#
# 依赖：python3（3.10+）、qinetwork（契网身份/信封库）、~/.qi/identity.key
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
BIN="${HOME}/.local/bin"
mkdir -p "$BIN"

# 找一个能 import qinetwork 的 python
pick_python() {
  for p in $(command -v python3) $(ls -d "$HOME"/.hermes/tools/python-3.1*/bin/python3 2>/dev/null) \
           "$HOME"/.hermes/hermes-agent/venv/bin/python3; do
    [ -x "$p" ] || continue
    if "$p" -c 'import qinetwork' >/dev/null 2>&1; then echo "$p"; return 0; fi
    if PYTHONPATH="$HOME/.local/lib/python3.14/site-packages" "$p" -c 'import qinetwork' >/dev/null 2>&1; then
      echo "$p"; return 0
    fi
  done
  return 1
}

PY=$(pick_python) || { echo "找不到能 import qinetwork 的 python3（先在 qi-core 里 pip install -e .）" >&2; exit 1; }
SP="$HOME/.local/lib/python3.14/site-packages"
echo "python: $PY"

write_wrapper() {
  name=$1; script=$2
  cat > "$BIN/$name" <<EOF
#!/bin/sh
export PYTHONPATH="$SP:$HERE\${PYTHONPATH:+:\$PYTHONPATH}"
exec $PY $HERE/$script "\$@"
EOF
  chmod +x "$BIN/$name"
  echo "  已装 $BIN/$name"
}

write_wrapper qi qi
write_wrapper qi-mcp qi_mcp.py

# 首次使用：没有身份就生成一个
if [ ! -f "$HOME/.qi/identity.key" ]; then
  mkdir -p "$HOME/.qi/inbox"
  "$BIN/qi" whoami >/dev/null 2>&1 || true
  echo "  提示：$HOME/.qi/identity.key 不存在，请先用 qi-core 生成身份"
fi

case "${1:-}" in
  --mcp)
    shift
    for tool in "$@"; do
      case "$tool" in
        codex)  grep -q '^\[mcp_servers.qi\]' "$HOME/.codex/config.toml" 2>/dev/null || \
                printf '\n[mcp_servers.qi]\ncommand = "%s/qi-mcp"\nargs = []\n' "$BIN" >> "$HOME/.codex/config.toml"
                echo "  codex: 已写入 ~/.codex/config.toml" ;;
        claude) command -v claude >/dev/null && claude mcp add qi -s user "$BIN/qi-mcp" >/dev/null 2>&1 && echo "  claude: 已注册" ;;
        pi)     command -v pi >/dev/null && pi mcp add qi -- "$BIN/qi-mcp" >/dev/null 2>&1 && echo "  pi: 已注册" ;;
        qwen)   command -v qwen >/dev/null && qwen mcp add qi "$BIN/qi-mcp" >/dev/null 2>&1 && echo "  qwen: 已注册" ;;
        opencode) echo "  opencode: 手动在 opencode.json 的 mcp 里加 {\"qi\":{\"type\":\"local\",\"command\":[\"$BIN/qi-mcp\"]}}" ;;
        *) echo "  未知工具: $tool" ;;
      esac
    done ;;
esac

echo
echo "自检："
"$BIN/qi" whoami || true
