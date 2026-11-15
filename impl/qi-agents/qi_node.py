#!/usr/bin/env python3
"""qi_node —— 契网节点的命令行入口

把 qi_net.py（传输+信封）和 qinetwork（身份+卡片+契约消息）拼成一个可用的
节点：一个身份、一张 Agent Card、一个 HTTP 收件口、一组命令。

  qi_node.py init --name laptop --skill 编码 --endpoint http=http://[::1]:9733
  qi_node.py publish                      # 注册到 registry + DHT
  qi_node.py serve --port 9733            # 直接收消息（前台）
  qi_node.py send --to did:key:z... --addr host:9733 --type qi/covenant/propose ...
  qi_node.py inbox                        # 看收到的消息
  qi_node.py card                         # 打印自己的卡片

数据目录默认 ~/.qi（可用 --data 或 QI_DATA_DIR 覆盖）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qi_net as net                                        # noqa: E402
from qinetwork import AgentCard, Endpoint, QiIdentity, Skill  # noqa: E402

DEFAULT_REGISTRY = os.environ.get("QI_REGISTRY", "http://qi-network.net:7881")
DEFAULT_BOOTSTRAP = os.environ.get("QI_BOOTSTRAP", "http://qi-network.net:7883")
# 中转信箱默认就是 bootstrap（它同时提供存储转发）；置空则完全关掉降级
DEFAULT_MAILBOX = os.environ.get("QI_MAILBOX", DEFAULT_BOOTSTRAP)


# ── 身份与卡片 ──────────────────────────────────────────

def key_path(d):
    return net.data_dir(d) / "identity.key"


def card_path(d):
    return net.data_dir(d) / "agent_card.json"


def get_identity(d=None, create=False):
    p = key_path(d)
    if p.exists():
        return QiIdentity(p.read_bytes())
    if not create:
        raise SystemExit(f"没有身份：{p}（先跑 qi_node.py init）")
    ident = QiIdentity.generate()
    p.write_bytes(ident.private_key_bytes)
    os.chmod(p, 0o600)
    return ident


def build_card(ident, name, description, master_name, skills, endpoints, ttl_days=365):
    sk = [Skill(id=s, name=s, description=s, confidence=1.0) for s in skills]
    eps = []
    for spec in endpoints:
        if "=" in spec:
            tr, url = spec.split("=", 1)
        else:
            tr, url = "http", spec
        eps.append(Endpoint(transport=tr, url=url))
    return AgentCard.create(
        identity=ident,
        master_identity=ident,           # master = 自己（无上级委托人）
        name=name,
        description=description,
        master_name=master_name,
        skills=sk,
        endpoints=eps,
        ttl_days=ttl_days,
    )


# ── 命令 ────────────────────────────────────────────────

def cmd_init(a):
    ident = get_identity(a.data, create=True)
    card = build_card(ident, a.name, a.desc, a.master, a.skill or [],
                      a.endpoint or [], a.ttl_days)
    card_path(a.data).write_text(card.to_json(), encoding="utf-8")
    d = net.data_dir(a.data)
    print(f"身份   {ident.node_id}")
    print(f"名称   {a.name}")
    print(f"数据   {d}")
    print(f"卡片   {card_path(a.data)}")
    if a.skill:
        print(f"技能   {', '.join(a.skill)}")
    if a.endpoint:
        print(f"端点   {', '.join(a.endpoint)}")


def cmd_card(a):
    p = card_path(a.data)
    if not p.exists():
        raise SystemExit("还没建卡片（先跑 init）")
    raw = json.loads(p.read_text(encoding="utf-8"))
    ident = get_identity(a.data)
    print(f"我的 DID: {ident.node_id}")
    print(f"卡片名称: {raw.get('identity', {}).get('name')}")
    print(f"技能:     {[s.get('name') for s in raw.get('skills', [])]}")
    print(f"端点:     {[(e.get('transport'), e.get('url')) for e in raw.get('endpoints', [])]}")
    print(f"过期:     {raw.get('expires_at')}")


def cmd_peer(a):
    """记下对端地址（worker 回执要用：did → addr）。"""
    p = net.data_dir(a.data) / "peers.json"
    peers = {}
    if p.exists():
        try:
            peers = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                                    # noqa: BLE001
            peers = {}
    if a.remove:
        peers.pop(a.did, None)
        p.write_text(json.dumps(peers, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已删除 {a.did[:30]}…")
        return
    peers[a.did] = {"addr": a.addr, "note": a.note}
    p.write_text(json.dumps(peers, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已记录 {a.did[:30]}… → {a.addr}")
    for k, v in peers.items():
        print(f"  {k[:40]}…  {v.get('addr')}")


def cmd_drain(a):
    """上线补拉：把中转站里给自己的消息取回来（离线期间发的不会丢）。"""
    ident = get_identity(a.data)
    base = a.mailbox or os.environ.get("QI_MAILBOX", DEFAULT_MAILBOX)
    if not base:
        print("没配置中转站（QI_MAILBOX 或 --mailbox）")
        sys.exit(1)
    res = net.drain_mailbox(base, ident, a.data, timeout=a.timeout, limit=a.limit)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    sys.exit(0 if res.get("ok") else 1)


def cmd_file(a):
    """契网文件通道：put / get / ls"""
    ident = get_identity(a.data)
    addr = a.addr
    if a.filecmd == "put":
        src = os.path.expanduser(a.src)
        if not os.path.exists(src):
            print(f"没有这个文件: {src}")
            sys.exit(1)
        size = os.path.getsize(src)
        print(f"上传 {os.path.basename(src)}  {size/1048576:.1f}MB → {addr}")
        last = [0.0]

        def prog(sent, total):
            now = __import__("time").time()
            if now - last[0] > 2 or sent >= total:
                last[0] = now
                pct = sent * 100.0 / max(1, total)
                print(f"  {sent/1048576:8.1f}/{total/1048576:.1f} MB  {pct:5.1f}%", flush=True)

        res = net.put_file(addr, ident, src, name=a.name or "", progress=prog)
        print(json.dumps({k: v for k, v in res.items() if k != "response"}, ensure_ascii=False, indent=2))
        if res.get("ok") and a.to:
            note = {"tid": res["tid"], "name": a.name or os.path.basename(src),
                    "size": res["size"], "sha256": res["sha256"]}
            r = net.send(addr, "qi/file/announce", note, ident, to_node=a.to)
            print("已通知对端:", r.get("ok"))
        sys.exit(0 if res.get("ok") else 1)

    if a.filecmd == "get":
        res = net.get_file(addr, ident, a.tid, a.dest, name=a.name or "")
        print(json.dumps(res, ensure_ascii=False, indent=2))
        sys.exit(0 if res.get("ok") else 1)

    if a.filecmd == "ls":
        files = net.list_files(addr, ident)
        if not files:
            print("（对端没有文件，或不允许我列）")
            sys.exit(0)
        for f in files:
            print(f"  {f['tid']}  {f['size']/1048576:8.1f}MB  {f['name']}  from {f['from'][:20]}…")
        sys.exit(0)


def cmd_send(a):
    ident = get_identity(a.data)
    payload = a.payload
    if payload and payload.startswith("@"):
        payload = Path(payload[1:]).read_text(encoding="utf-8")
    data = json.loads(payload) if payload else {}
    res = net.send(a.addr, a.type, data, ident, to_node=a.to, timeout=a.timeout,
                   mailbox=a.mailbox)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    sys.exit(0 if res.get("ok") else 1)


def cmd_inbox(a):
    items = net.inbox(a.limit, a.data)
    if a.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    if not items:
        print("（空）")
        return
    for it in items:
        env = it.get("envelope", {})
        print(f"[{it.get('received_at', '')[:19]}] {env.get('type', '?'):28s} "
              f"from={env.get('from', '')[:26]}… ok={it.get('verified')}")
        body = env.get("payload", {})
        if body:
            print(f"    {json.dumps(body, ensure_ascii=False)[:220]}")


def cmd_serve(a):
    ident = get_identity(a.data)
    print(f"节点 DID: {ident.node_id}")
    net.serve(a.port, a.host, a.data)


def cmd_publish(a):
    ident = get_identity(a.data)
    p = card_path(a.data)
    if not p.exists():
        raise SystemExit("还没建卡片（先跑 init）")
    card_json = p.read_text(encoding="utf-8")
    body = json.dumps({"card_json": card_json}, ensure_ascii=False).encode()
    req = urllib.request.Request(f"{a.registry}/api/v1/agents", data=body,
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=a.timeout) as r:
            res = json.loads(r.read().decode() or "{}")
            print(f"✅ registry 已登记 HTTP {r.status}  {res.get('node_id', '')}")
    except urllib.error.HTTPError as e:
        if e.code == 409:                       # 已注册过 → 改走 PUT
            try:
                req2 = urllib.request.Request(
                    f"{a.registry}/api/v1/agents/{ident.node_id}", data=body,
                    method="PUT", headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req2, timeout=a.timeout) as r2:
                    print(f"✅ registry 已更新 HTTP {r2.status}")
            except Exception as e2:                          # noqa: BLE001
                print(f"❌ registry 更新失败: {e2}")
                sys.exit(1)
        else:
            print(f"❌ registry 登记失败: {e}")
            sys.exit(1)
    except Exception as e:                                   # noqa: BLE001
        print(f"❌ registry 登记失败: {e}")
        sys.exit(1)
    # DHT：key = sha256(did)[:20]（与 bootstrap/node.py:node_id_from_did 一致）
    try:
        import hashlib
        key_hex = hashlib.sha256(ident.node_id.encode("utf-8")).digest()[:20].hex()
        put = json.dumps({"key": key_hex, "value": card_json, "did": ident.node_id},
                         ensure_ascii=False).encode()
        req2 = urllib.request.Request(f"{DEFAULT_BOOTSTRAP}/store", data=put,
                                      method="POST",
                                      headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req2, timeout=a.timeout) as r2:
            print(f"✅ DHT 已存储 HTTP {r2.status}  key={key_hex[:16]}…")
    except Exception as e:                                   # noqa: BLE001
        print(f"⚠️  DHT 存储失败（registry 已成功）: {e}")


def cmd_health(a):
    try:
        with urllib.request.urlopen(f"{a.addr}/qi/health", timeout=5) as r:
            print(json.dumps(json.loads(r.read().decode()), ensure_ascii=False, indent=2))
    except Exception as e:                                   # noqa: BLE001
        print(f"不可达: {e}")
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="契网节点 CLI")
    ap.add_argument("--data", default=None, help="数据目录（默认 ~/.qi）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="生成身份和 Agent Card")
    p.add_argument("--name", required=True)
    p.add_argument("--desc", default="")
    p.add_argument("--master", default="")
    p.add_argument("--skill", action="append", help="可重复")
    p.add_argument("--endpoint", action="append", help="transport=url，可重复")
    p.add_argument("--ttl-days", type=int, default=365)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("card")
    p.set_defaults(func=cmd_card)

    p = sub.add_parser("file", help="文件通道：put/get/ls")
    fsub = p.add_subparsers(dest="filecmd", required=True)
    fp = fsub.add_parser("put", help="上传文件给对端")
    fp.add_argument("src")
    fp.add_argument("--addr", required=True, help="对端 host:port")
    fp.add_argument("--to", default="", help="对端 did（用于发通知消息）")
    fp.add_argument("--name", default="", help="对端显示的文件名")
    fp.set_defaults(func=cmd_file)
    fg = fsub.add_parser("get", help="从对端下载")
    fg.add_argument("--tid", required=True)
    fg.add_argument("--dest", required=True)
    fg.add_argument("--addr", required=True)
    fg.add_argument("--name", default="")
    fg.set_defaults(func=cmd_file)
    fl = fsub.add_parser("ls", help="列对端已收到的文件")
    fl.add_argument("--addr", required=True)
    fl.set_defaults(func=cmd_file)

    p = sub.add_parser("send")
    p.add_argument("--to", required=True, help="目标 DID")
    p.add_argument("--addr", required=True, help="host:port")
    p.add_argument("--type", required=True, help="qi/covenant/propose 等")
    p.add_argument("--payload", default="{}", help="JSON，或 @文件")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--mailbox", default=None,
                   help="直连失败时的中转站（默认 QI_MAILBOX，置空串可禁用降级）")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("drain", help="从中转站补拉离线期间的消息")
    p.add_argument("--mailbox", default=None)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--timeout", type=float, default=15.0)
    p.set_defaults(func=cmd_drain)

    p = sub.add_parser("peer", help="记录对端 did → 地址")
    p.add_argument("--did", required=True)
    p.add_argument("--addr", default="")
    p.add_argument("--note", default="")
    p.add_argument("--remove", action="store_true")
    p.set_defaults(func=cmd_peer)

    p = sub.add_parser("inbox")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=9733)
    p.add_argument("--host", default="::", help="默认 :: 双栈；0.0.0.0 只监听 IPv4")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("publish")
    p.add_argument("--registry", default=DEFAULT_REGISTRY)
    p.add_argument("--timeout", type=float, default=20.0)
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("health")
    p.add_argument("--addr", required=True)
    p.set_defaults(func=cmd_health)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
