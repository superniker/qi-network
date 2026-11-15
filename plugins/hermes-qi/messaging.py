"""
契网通讯与文件工具 —— 装到 agent 身上的能力，不预设流程

设计原则（重要）：
  编排方只发消息，不碰对端机器。agent 收到消息后，
  自己决定怎么干 —— 用契网文件通道、用 rsync、用 tar，
  都是它自己的选择。这里只提供原语：

    qi_serve        起监听（让别的 agent 能找到我）
    qi_inbox        我收到了什么
    qi_send         发一条消息给某个 did
    qi_reply        回复某条消息（带 in_reply_to）
    qi_send_file    把一个文件推给对方
    qi_recv_file    从对方拉一个文件
    qi_remote_files 看对方有哪些文件
    qi_resolve      查某个 agent 的地址
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

_SERVE_LOCK = threading.Lock()
_SERVING: dict[int, threading.Thread] = {}


def agents_dir() -> str:
    """找到 qi_net.py 所在的目录（各机器路径不同，按候选顺序找）"""
    cands = [
        os.environ.get("QI_AGENTS_DIR", ""),
        str(Path(__file__).resolve().parents[3] / "impl" / "qi-agents"),
        str(Path.home() / "qi-src" / "qi-agents"),
        str(Path.home() / "hot4" / "开发" / "qi-network" / "impl" / "qi-agents"),
        "/opt/qi/qi-agents",
    ]
    for c in cands:
        if c and (Path(c) / "qi_net.py").exists():
            return c
    raise RuntimeError(
        "找不到 qi_net.py，请设置 QI_AGENTS_DIR 环境变量指向 impl/qi-agents")


_AGENTS = agents_dir()
if _AGENTS not in sys.path:
    sys.path.insert(0, _AGENTS)
# qi_net 依赖 qinetwork（qi-core），一起挂上，免得各机器要配环境变量
_CORE = Path(_AGENTS).parent / "qi-core"
if (_CORE / "qinetwork").is_dir() and str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import qi_net as net                                     # noqa: E402
from qinetwork import QiIdentity                          # noqa: E402

DATA_DIR = Path(os.environ.get("QI_DATA_DIR", "~/.qi-node")).expanduser()

# 同一台机器上可能跑多个节点实例（比如 9733 主节点 + 9738 文件接收节点），
# 它们共用身份但数据目录不同 —— 收件箱会分裂。这里把所有同 DID 的目录都读上。
_EXTRA_DIRS = [
    Path.home() / ".qi-node-file",
    Path.home() / ".qi-node-tax",
    Path.home() / ".qi-node-send",
]


def data_dirs() -> list[Path]:
    out = [DATA_DIR]
    for d in _EXTRA_DIRS:
        if d != DATA_DIR and d.is_dir() and (d / "inbox").is_dir():
            out.append(d)
    return out


def identity() -> QiIdentity:
    key = DATA_DIR / "identity.key"
    if not key.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ident = QiIdentity.generate()
        key.write_bytes(ident.private_key_bytes)
        key.chmod(0o600)
        print(f"[契网] 新身份已生成: {ident.node_id}", file=sys.stderr)
        return ident
    return QiIdentity(key.read_bytes())


def _out(obj) -> str:
    if isinstance(obj, str):
        return obj
    return json.dumps(obj, ensure_ascii=False, indent=2)


# ── 监听 ────────────────────────────────────────────────

def serve(port: int = 9733, host: str = "::") -> str:
    """起后台监听线程（幂等）。收到消息落 inbox，收到文件落 ~/.qi-node/files。"""
    with _SERVE_LOCK:
        t = _SERVING.get(port)
        if t and t.is_alive():
            return _out({"ok": True, "already": True, "port": port,
                         "did": identity().node_id})
        th = threading.Thread(target=net.serve, args=(port, host, str(DATA_DIR)),
                              daemon=True, name=f"qi-serve-{port}")
        th.start()
        _SERVING[port] = th
        return _out({"ok": True, "started": True, "port": port, "host": host,
                     "did": identity().node_id, "data": str(DATA_DIR)})


# ── 消息 ────────────────────────────────────────────────

def inbox(limit: int = 20) -> str:
    seen = {}
    for d in data_dirs():
        for it in net.inbox(limit, str(d)):
            env = it.get("envelope", {})
            mid = env.get("message_id")
            if not mid or mid in seen:
                continue
            p = env.get("payload", {})
            seen[mid] = {
                "message_id": mid,
                "type": env.get("type"),
                "from": env.get("from"),
                "timestamp": env.get("timestamp"),
                "payload": p,
                "_dir": str(d),
            }
    items = sorted(seen.values(), key=lambda x: x.get("timestamp") or "")
    items = items[-limit:]
    return _out({"ok": True, "count": len(items), "messages": items})


def send(to_did: str, address: str, msg_type: str, payload_json: str = "{}",
         mailbox: str = "") -> str:
    """
    发一条消息。address 形如 host:port（IPv6 用 [::1]:9733）。
    投递失败会自动走中转信箱（离线也不丢）。
    """
    try:
        payload = json.loads(payload_json) if payload_json else {}
    except json.JSONDecodeError as e:
        return _out({"ok": False, "error": f"payload 不是合法 JSON: {e}"})
    mb = mailbox or os.environ.get("QI_MAILBOX", "")
    res = net.send(address, msg_type, payload, identity(), to_node=to_did,
                   mailbox=mb or None)
    return _out(res)


def send_task(to_did: str, address: str, goal: str, detail: str = "",
              reply_to: str = "") -> str:
    """
    发一个"请你去办"的委托消息。对端 agent 自己决定怎么执行、用不用它自己的工具。
    这是编排方唯一该做的事：说清楚要什么，不说怎么做。
    """
    payload = {
        "goal": goal,
        "detail": detail,
        "issued_at_device": os.uname().nodename,
        "from_did": identity().node_id,
    }
    if reply_to:
        payload["in_reply_to"] = reply_to
    mb = os.environ.get("QI_MAILBOX", "")
    res = net.send(address, "qi/task/request", payload, identity(),
                   to_node=to_did, mailbox=mb or None)
    return _out(res)


def reply(to_did: str, address: str, in_reply_to: str, body: str,
          status: str = "done") -> str:
    payload = {"in_reply_to": in_reply_to, "status": status, "body": body,
               "from_did": identity().node_id}
    mb = os.environ.get("QI_MAILBOX", "")
    res = net.send(address, "qi/task/report", payload, identity(),
                   to_node=to_did, mailbox=mb or None)
    return _out(res)


# ── 文件 ────────────────────────────────────────────────

def send_file(address: str, path: str, name: str = "") -> str:
    """把本地文件推给对方（契网文件通道，did:key 鉴权 + sha256 校验 + 断点续传）"""
    res = net.put_file(address, identity(), path, name=name)
    return _out(res)


def recv_file(address: str, tid: str, dest: str, name: str = "") -> str:
    """从对方拉一个文件到本地"""
    res = net.get_file(address, identity(), tid, dest, name=name)
    return _out(res)


def remote_files(address: str) -> str:
    """看对方有哪些文件（需要对方认得你的 did）"""
    files = net.list_files(address, identity())
    return _out({"ok": True, "count": len(files), "files": files})


def local_files() -> str:
    """我收到了哪些文件（含这台机器上所有节点实例的收件目录）"""
    out = []
    for d in data_dirs():
        fdir = d / "files"
        if not fdir.exists():
            continue
        for meta in sorted(fdir.glob("*.meta.json")):
            try:
                out.append(json.loads(meta.read_text()))
            except Exception:                              # noqa: BLE001
                continue
    return _out({"ok": True, "count": len(out), "files": out})


# ── 发现 ────────────────────────────────────────────────

def resolve(name_or_did: str, registry: str = "http://qi-network.net:7881") -> str:
    """按名字或 did 前缀查 agent 卡片，拿到它的地址和 did"""
    import urllib.parse
    import urllib.request
    q = urllib.parse.quote(name_or_did)
    try:
        with urllib.request.urlopen(f"{registry}/api/v1/agents?q={q}", timeout=15) as r:
            data = json.loads(r.read().decode())
        agents = data.get("agents", [])
        out = []
        for a in agents:
            card = a.get("card") or {}
            out.append({"name": a.get("name"), "node_id": a.get("node_id"),
                        "endpoints": card.get("endpoints", []),
                        "skills": a.get("skills")})
        return _out({"ok": True, "count": len(out), "agents": out})
    except Exception as e:                                 # noqa: BLE001
        return _out({"ok": False, "error": str(e)})
