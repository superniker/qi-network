#!/usr/bin/env python3
"""
qi-mcp —— 契网 MCP 服务器（stdio，newline-delimited JSON-RPC）

让任何支持 MCP 的 CLI agent（codex / claude / pi / qwen / opencode …）直接拥有：
  qi_whoami   我的 DID / 地址 / 收件箱条数 / 已知 peer
  qi_send     向某个 peer 投递一条契网消息（技能、经验、笔记）
  qi_inbox    列收件箱
  qi_read     读某条消息全文
  qi_peer     登记 / 列出 / 删除 peer（名字 → host:port）

注册示例（codex，~/.codex/config.toml）：
  [mcp_servers.qi]
  command = "/home/niker/.local/bin/qi-mcp"

协议：MCP 2024-11-05，最小实现（initialize / tools/list / tools/call / ping）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import qi_net

SERVER_NAME = "qi-mcp"
SERVER_VERSION = "0.1.0"
PROTOCOL = "2024-11-05"


def _text(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}], "isError": False}


def _err(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}], "isError": True}


def _peer_book() -> dict:
    p = qi_net.data_dir() / "peers.json"
    return json.loads(p.read_text()) if p.exists() else {}


TOOLS = [
    {
        "name": "qi_whoami",
        "description": "查看本节点的契网身份：DID、可直连地址、收件箱条数、已知 peer 列表。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "qi_send",
        "description": ("向另一个契网节点投递消息（HTTP 直连，签名但未加密）。"
                        "适合传技能、经验、笔记。to 可以是 host:port，也可以是 qi_peer 登记过的名字。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "host:port 或已登记的 peer 名，如 192.168.1.192:9733"},
                "text": {"type": "string", "description": "消息正文（纯文本 / markdown）"},
                "name": {"type": "string", "description": "标题，如技能名"},
                "type": {"type": "string", "description": "消息类型，默认 qi/knowledge/share"},
                "payload_file": {"type": "string", "description": "可选：从 JSON 文件读完整 payload（与 text 二选一）"},
                "to_node": {"type": "string", "description": "收件人 DID（可留空）"},
            },
            "required": ["to"],
            "additionalProperties": False,
        },
    },
    {
        "name": "qi_inbox",
        "description": "列出收件箱里的消息（最新的在前），含验签结果、发件人 DID、标题摘要。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "返回条数，默认 10"},
                "full": {"type": "boolean", "description": "true 则返回完整 JSON（含正文）"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "qi_read",
        "description": "读一条消息的完整内容，参数可以是 inbox 文件名、message_id 或其结尾片段。",
        "inputSchema": {
            "type": "object",
            "properties": {"selector": {"type": "string"}},
            "required": ["selector"],
            "additionalProperties": False,
        },
    },
    {
        "name": "qi_peer",
        "description": "管理 peer 名册：add（记名字→地址）/ list / rm。名字可直接用在 qi_send 的 to 里。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "list", "rm"]},
                "name": {"type": "string"},
                "address": {"type": "string", "description": "host:port，add 时必填"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
]


def call_tool(name: str, args: dict) -> dict:
    try:
        if name == "qi_whoami":
            ident = qi_net.load_identity()
            card = qi_net.load_card()
            return _text(json.dumps({
                "did": ident.node_id,
                "address": qi_net.my_address(card),
                "name": (card.get("identity") or {}).get("name", ""),
                "inbox": len(qi_net.inbox(1000)),
                "peers": _peer_book(),
            }, ensure_ascii=False, indent=2))

        if name == "qi_send":
            to = args["to"]
            to = _peer_book().get(to, to)
            ident = qi_net.load_identity()
            if args.get("payload_file"):
                payload = json.loads(Path(args["payload_file"]).read_text())
            else:
                payload = {"kind": "note", "name": args.get("name") or "note",
                           "content": args.get("text", "")}
            r = qi_net.send(to, args.get("type") or qi_net.DEFAULT_TYPE, payload,
                            ident, to_node=args.get("to_node", ""))
            return _text(json.dumps(r, ensure_ascii=False, indent=2)) if r.get("ok") \
                else _err(json.dumps(r, ensure_ascii=False, indent=2))

        if name == "qi_inbox":
            recs = qi_net.inbox(int(args.get("limit", 10)))
            if args.get("full"):
                return _text(json.dumps(recs, ensure_ascii=False, indent=2))
            lines = []
            for r in recs:
                e = r["envelope"]
                p = e.get("payload", {})
                lines.append(f"{r['received_at'][:19]}  {e['type']}  verified={r['verified']}  "
                             f"{r['_file']}\n    {p.get('name', '')}: "
                             f"{str(p.get('summary') or p.get('content') or '')[:120]}")
            return _text("\n".join(lines) if lines else "收件箱为空")

        if name == "qi_read":
            sel = args["selector"]
            for r in qi_net.inbox(1000):
                mid = r["envelope"].get("message_id", "")
                if sel in r["_file"] or mid.endswith(sel) or mid == sel:
                    return _text(json.dumps(r, ensure_ascii=False, indent=2))
            return _err(f"未找到: {sel}")

        if name == "qi_peer":
            p = qi_net.data_dir() / "peers.json"
            book = _peer_book()
            act = args["action"]
            if act == "add":
                if not args.get("address"):
                    return _err("add 需要 address（host:port）")
                book[args["name"]] = args["address"]
                p.write_text(json.dumps(book, ensure_ascii=False, indent=2))
                return _text(f"已登记 {args['name']} → {args['address']}")
            if act == "rm":
                book.pop(args.get("name", ""), None)
                p.write_text(json.dumps(book, ensure_ascii=False, indent=2))
                return _text(f"已移除 {args.get('name')}")
            return _text(json.dumps(book, ensure_ascii=False, indent=2) if book else "（空）")

        return _err(f"未知工具: {name}")
    except Exception as exc:                                  # noqa: BLE001
        return _err(f"{type(exc).__name__}: {exc}")


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }}
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        out = call_tool(params.get("name", ""), params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": mid, "result": out}
    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:                                     # noqa: BLE001
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
