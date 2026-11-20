#!/usr/bin/env python3
"""
qi_net —— 契网对等节点核心库（agent 无关）

给任何 CLI agent（codex / claude / pi / qwen / opencode …）用的最小投递层：
  * 身份：~/.qi/identity.key（ed25519，did:key）
  * 信封：qinetwork.QiMessage + 本地扩展 `sig_env`（整封签名，防改地址）
  * 传输：HTTP POST /qi/message（局域网或公网直连，暂不加密）
  * 落地：~/.qi/inbox/<时间>-<mid8>.json，按 message_id 去重

刻意不做：邮箱存储转发、附件流、端到端加密、重试队列。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import secrets
import shutil
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.error
import urllib.request

from qinetwork import QiIdentity, QiMessage

DEFAULT_DATA_DIR = Path(os.environ.get("QI_DATA_DIR", "~/.qi")).expanduser()
TS_WINDOW = 300
DEFAULT_TYPE = "qi/knowledge/share"

# —— 进件护栏：签名只证明发件人身份，不证明其可信，故加闸（均可用 env 调）——
FILE_MAX_MB = int(os.environ.get("QI_FILE_MAX_MB", "2048"))        # 单文件大小上限
DIR_QUOTA_GB = float(os.environ.get("QI_FILE_QUOTA_GB", "20"))     # 收件目录总配额
DAILY_GB_PER_DID = float(os.environ.get("QI_FILE_DAILY_GB", "10")) # 每个发件DID当日配额
ALLOW_DIDS = [s.strip() for s in os.environ.get("QI_FILE_ALLOW", "").split(",") if s.strip()]
MSG_MAX_KB = int(os.environ.get("QI_MSG_MAX_KB", "512"))           # 单条消息上限


def _files_used(fdir: Path) -> int:
    """已收文件+未完成的 .part 占用字节数（.part 也计，防半途丢弃堆积）。"""
    total = 0
    for f in fdir.iterdir():
        if f.suffix == ".json" or not f.is_file():
            continue
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


def _sweep_stale_parts(fdir: Path, max_age_s: int = 86400) -> None:
    """删超过24h没写完的 .part。"""
    now = time.time()
    for f in fdir.glob("*.part"):
        try:
            if now - f.stat().st_mtime > max_age_s:
                f.unlink(missing_ok=True)
        except OSError:
            pass


def _today_from_did(fdir: Path, did: str) -> int:
    """某发件DID今天已投进来的字节数（据 meta.json，best-effort）。"""
    total = 0
    today = datetime.date.today().isoformat()
    for meta in fdir.glob("*.meta.json"):
        try:
            m = json.loads(meta.read_text())
            if m.get("from") == did and str(m.get("ts", "")).startswith(today):
                total += int(m.get("size", 0))
        except Exception:  # noqa: BLE001
            continue
    return total


POLICY_FILENAME = "policy.json"


def load_policy(d: str | Path | None = None) -> dict:
    """读取 <data_dir>/policy.json 并与 env 默认合并。schema：
    {
      "disclose": true,                      # 是否应答 GET /qi/policy 探询
      "defaults": {"max_file_mb": 2048, "quota_gb": 20,
                   "daily_gb": 10, "msg_max_kb": 512, "allow_list": []},
      "per_did": {"did:key:...或短前缀": {"max_file_mb": ..., "deny": true}}
    }
    """
    merged = {"disclose": True,
              "defaults": {"max_file_mb": FILE_MAX_MB, "quota_gb": DIR_QUOTA_GB,
                           "daily_gb": DAILY_GB_PER_DID, "msg_max_kb": MSG_MAX_KB,
                           "allow_list": ALLOW_DIDS}}
    try:
        user = json.loads((data_dir(d) / POLICY_FILENAME).read_text())
    except FileNotFoundError:
        user = {}
    except (json.JSONDecodeError, OSError) as e:               # noqa: BLE001
        print(f"⚠️ policy.json 不可读，用默认: {e}", flush=True)
        user = {}
    merged["disclose"] = bool(user.get("disclose", True))
    merged["defaults"].update(user.get("defaults") or {})
    merged["per_did"] = user.get("per_did") or {}
    return merged


def policy_for_did(policy: dict, did: str) -> dict:
    """默认档叠加该 DID 覆盖档。精确匹配优先，其次前缀/后缀匹配。"""
    limits = dict(policy["defaults"])
    best = None
    for key, over in policy.get("per_did", {}).items():
        if did == key:
            best = over
            break
        if best is None and (did.startswith(key) or did.endswith(key)):
            best = over
    if best:
        limits.update(best)
    return limits


def data_dir(d: str | Path | None = None) -> Path:
    p = Path(d).expanduser() if d else DEFAULT_DATA_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def inbox_dir(d: str | Path | None = None) -> Path:
    p = data_dir(d) / "inbox"
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_identity(d: str | Path | None = None) -> QiIdentity:
    return QiIdentity((data_dir(d) / "identity.key").read_bytes())


def load_card(d: str | Path | None = None) -> dict:
    p = data_dir(d) / "agent_card.json"
    return json.loads(p.read_text()) if p.exists() else {}


def my_address(card: dict) -> str:
    """从 Agent Card 挑 http 可达地址（file/ssh 类 endpoint 不用于直连）。"""
    cands = []
    for e in card.get("endpoints") or []:
        if isinstance(e, dict):
            cands.append((str(e.get("transport", "")),
                          str(e.get("url") or e.get("address") or "")))
        else:
            cands.append(("", str(e)))
    for tr, url in cands:
        if tr in ("http", "https", "qi-direct") or url.startswith("http"):
            return url
    return ""   # file/ssh 类 endpoint 不能用于直连投递，宁可返回空


# ── 信封 ────────────────────────────────────────────────

def build_envelope(msg_type: str, payload: dict, to_node: str,
                   identity: QiIdentity) -> dict:
    me = identity.node_id
    msg = QiMessage(msg_type=msg_type, from_node=me, to_node=to_node, payload=payload)
    env = msg.to_dict()
    env["signature"] = msg.sign(identity)
    env["signer"] = me
    env["sig_env"] = identity.sign_json(env)
    return env


def verify_envelope(env: dict) -> tuple[bool, str]:
    for k in ("type", "from", "to", "payload", "signature", "signer", "sig_env"):
        if k not in env:
            return False, f"缺字段 {k}"
    if env["from"] != env["signer"]:
        return False, "from 与 signer 不一致"
    try:
        pub = QiIdentity.public_key_from_node_id(env["signer"])
    except Exception as e:                                   # noqa: BLE001
        return False, f"signer 无法解析为公钥: {e}"
    if not QiIdentity.verify_json(env["payload"], env["signature"], pub):
        return False, "payload 签名无效"
    core = {k: v for k, v in env.items() if k != "sig_env"}
    if not QiIdentity.verify_json(core, env["sig_env"], pub):
        return False, "信封签名无效（from/to/id/timestamp 被改过）"
    try:
        t = datetime.datetime.fromisoformat(
            env.get("timestamp", "").replace("Z", "+00:00")).timestamp()
    except Exception:                                        # noqa: BLE001
        return False, f"timestamp 解析失败: {env.get('timestamp')}"
    drift = abs(time.time() - t)
    if drift > TS_WINDOW:
        return False, f"timestamp 超窗（偏差 {int(drift)}s）"
    return True, "ok"


# ── 收件箱 ──────────────────────────────────────────────

def seen(message_id: str, d: str | Path | None = None) -> bool:
    for f in inbox_dir(d).glob("*.json"):
        try:
            if json.loads(f.read_text()).get("envelope", {}).get("message_id") == message_id:
                return True
        except Exception:                                    # noqa: BLE001
            continue
    return False


def store(env: dict, verified: bool, reason: str, d: str | Path | None = None) -> Path:
    mid = str(env.get("message_id", "")).rsplit(":", 1)[-1][:8]
    p = inbox_dir(d) / f"{time.strftime('%Y%m%d-%H%M%S')}-{mid}.json"
    p.write_text(json.dumps({
        "received_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "verified": verified,
        "verify_reason": reason,
        "envelope": env,
    }, ensure_ascii=False, indent=2))
    # 通知层：写事件（+ 可选桌面弹窗）。失败绝不影响收信 —— 见 qi_notify 的约定。
    try:
        import qi_notify                                            # noqa: PLC0415
        qi_notify.on_store(env, p, verified=verified, reason=reason)
    except Exception:                                               # noqa: BLE001
        pass
    # 自动回执：告诉发送方"已收到，排队待处理"。异步 + 有并发上限，绝不阻塞收信。
    try:
        _spawn_receipt(env, d)
    except Exception:                                               # noqa: BLE001
        pass
    return p


# ── 自动回执 ──────────────────────────────────────────────────────────
# 问题背景（2026-11-20 排查）：收信口 9737 是 `qi serve`，只落盘不回话；
# worker 只认 COVENANT_PROPOSE。发送方拿到的是 HTTP 200 `stored`，
# 但那是"信到了"，不是"有人接了" —— 于是以为对方没反应，反复催办。
#
# 这里在唯一收信落盘入口 store() 挂一层：收到任务类消息立刻回一条
#    qi/task/receipt  {status: received, in_reply_to: <原 id>}
# 走中转信箱（收件方必然在线可自取，比直投可靠）。
#
# 三条硬约定：
#   1. 绝不阻塞收信 —— 独立 daemon 线程 + 信号量限并发
#   2. 绝不回执回执/报告类 —— 天然防循环
#   3. 失败静默 —— 回执是锦上添花，收信才是本职工作
_RECEIPT_TYPES = frozenset({
    "task", "qi/task/request", "qi/knowledge/share",
    "qi/covenant/propose", "qi/covenant/verify",
})
_RECEIPT_MAX_THREADS = 4
_receipt_sem = threading.Semaphore(_RECEIPT_MAX_THREADS)
_receipt_seen: set[str] = set()
_receipt_lock = threading.Lock()


def _receipt_base() -> str:
    """回执走哪个中转站。环境变量优先，否则用已知的中转站。"""
    return (os.environ.get("QI_MAILBOX") or "http://qi-network.net:7883").strip()


def _auto_receipt(env: dict, d: str | Path | None = None) -> None:
    try:
        mid = str(env.get("message_id") or "")
        typ = str(env.get("type") or "")
        frm = str(env.get("from") or "")
        if not (mid and typ and frm):
            return
        if typ not in _RECEIPT_TYPES:
            return                                    # 回执/报告类不回执
        with _receipt_lock:
            if mid in _receipt_seen:
                return
            _receipt_seen.add(mid)
        ident = load_identity(d)
        if frm == ident.node_id:
            return                                    # 自己发的不回执
        payload = {
            "status": "received",
            "in_reply_to": mid,
            "re_type": typ,
            "note": "已收到，排队待处理。处理完会另发执行报告。",
        }
        rec = build_envelope("qi/task/receipt", payload, frm, ident)
        deposit_mailbox(_receipt_base(), frm, rec, ident, timeout=8.0)
    except Exception:                                             # noqa: BLE001
        pass
    finally:
        try:
            _receipt_sem.release()
        except Exception:                                         # noqa: BLE001
            pass


def _spawn_receipt(env: dict, d: str | Path | None = None) -> None:
    """起一个回执线程；并发满了就直接跳过（不排队、不阻塞）。"""
    if not _receipt_sem.acquire(blocking=False):
        return
    threading.Thread(target=_auto_receipt, args=(env, d), daemon=True).start()


def inbox(limit: int = 20, d: str | Path | None = None) -> list[dict]:
    files = sorted(inbox_dir(d).glob("*.json"), reverse=True)[:limit]
    out = []
    for f in files:
        try:
            rec = json.loads(f.read_text())
        except Exception:                                    # noqa: BLE001
            continue
        rec["_file"] = f.name
        out.append(rec)
    return out


# ── 发送 ────────────────────────────────────────────────

def send(peer: str, msg_type: str, payload: dict, identity: QiIdentity,
         to_node: str = "", timeout: float = 5.0, mailbox: str | None = None) -> dict:
    """直连投递。打不通时，若配置了中转（mailbox），把信封暂存到中转站。"""
    if "://" in peer:
        peer = peer.split("://", 1)[1]
    peer = peer.rstrip("/")
    env = build_envelope(msg_type, payload, to_node, identity)
    req = urllib.request.Request(
        f"http://{peer}/qi/message",
        data=json.dumps(env, ensure_ascii=False).encode(),
        method="POST", headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"ok": True, "status": r.status, "ms": int((time.time() - t0) * 1000),
                    "message_id": env["message_id"],
                    "response": json.loads(r.read().decode() or "{}")}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code,
                "error": e.read().decode()[:300], "message_id": env["message_id"]}
    except Exception as e:                                   # noqa: BLE001
        direct_err = f"{type(e).__name__}: {e}"
        # ── 降级：投中转信箱 ──
        relay = mailbox if mailbox is not None else os.environ.get("QI_MAILBOX", "")
        if relay and to_node:
            dep = deposit_mailbox(relay, to_node, env, identity)
            dep["direct_error"] = direct_err
            dep["message_id"] = env["message_id"]
            return dep
        return {"ok": False, "error": direct_err, "message_id": env["message_id"]}


# ── 中转信箱（存储转发）─────────────────────────────────

def deposit_mailbox(base: str, to_node: str, env: dict, identity: QiIdentity,
                    timeout: float = 10.0) -> dict:
    """把已签名信封投到中转站，等收件方上线自取。"""
    url = f"{base.rstrip('/')}/mailbox/{to_node}"
    req = urllib.request.Request(
        url, data=json.dumps(env, ensure_ascii=False).encode(),
        method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"ok": True, "queued": True, "status": r.status,
                    "response": json.loads(r.read().decode() or "{}")}
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "queued": False, "error": f"{type(e).__name__}: {e}"}


def drain_mailbox(base: str, identity: QiIdentity, d: str | Path | None = None,
                  timeout: float = 15.0, limit: int = 50) -> dict:
    """上线后主动补拉：取回中转站里给自己的消息，落进 inbox 并确认。

    取信需要私钥签名（did|ts），别人取不走。
    """
    import urllib.parse

    did = identity.node_id
    ts = int(time.time())
    sig = identity.sign_json({"did": did, "ts": ts})
    qs = urllib.parse.urlencode({"ts": ts, "sig": sig, "limit": limit})
    url = f"{base.rstrip('/')}/mailbox/{did}?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = json.loads(r.read().decode() or "{}")
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "stored": 0}

    stored, skipped = [], 0
    to_ack: list[str] = []
    for item in body.get("messages", []):
        env = item.get("envelope") or {}
        mid = env.get("message_id")
        if not mid:
            skipped += 1
            continue
        if seen(mid, d):
            # 已经在本地了 → 也要 ack，否则中转站每次都会重复投递
            skipped += 1
            to_ack.append(mid)
            continue
        ok, reason = verify_envelope(env)
        if not ok:
            skipped += 1
            continue
        store(env, ok, "mailbox", d)
        stored.append(mid)
        to_ack.append(mid)

    acked = 0
    if to_ack:
        ack_payload = {"ts": ts, "sig": sig, "ids": to_ack}
        req = urllib.request.Request(
            f"{base.rstrip('/')}/mailbox/{did}/ack",
            data=json.dumps(ack_payload, ensure_ascii=False).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                acked = json.loads(r.read().decode() or "{}").get("acked", 0)
        except Exception:                                    # noqa: BLE001
            acked = 0
    return {"ok": True, "fetched": body.get("count", 0), "stored": len(stored),
            "skipped": skipped, "acked": acked}


# ── 文件通道 ────────────────────────────────────────────
#
# 大文件不能塞进消息（信封是 JSON、信箱上限 256KB）。这里给节点加一条
# 带身份签名的二进制通道：
#
#   PUT  /qi/file/<tid>   流式上传，支持 Content-Range 断点续传
#   GET  /qi/policy       发前探询：签名方得到"我对你个人的限额与余量"
#   GET  /qi/file/<tid>   下载，支持 Range
#   GET  /qi/files        已收到的文件清单
#
# 鉴权：请求头 X-Qi-Did / X-Qi-Ts / X-Qi-Sig，签名对象是
#   {did, ts, op, tid, name, sha256}
# 所以"谁能往你这儿放文件"由 did:key 决定，不靠 IP。


def files_dir(d: str | Path | None = None) -> Path:
    p = data_dir(d) / "files"
    p.mkdir(parents=True, exist_ok=True)
    return p


def sha256_file(p: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def verify_signed(payload: dict, sig: str, did: str) -> bool:
    try:
        pub = QiIdentity.public_key_from_node_id(did)
    except Exception:                                        # noqa: BLE001
        return False
    return QiIdentity.verify_json(payload, sig, pub)


def file_headers(identity: QiIdentity, op: str, tid: str, name: str = "",
                 sha: str = "", size: int = 0) -> dict:
    ts = int(time.time())
    payload = {"did": identity.node_id, "ts": ts, "op": op, "tid": tid,
               "name": name, "sha256": sha}
    return {
        "X-Qi-Did": identity.node_id,
        "X-Qi-Ts": str(ts),
        "X-Qi-Sig": identity.sign_json(payload),
        "X-Qi-Name": urllib.parse.quote(name),
        "X-Qi-Sha256": sha,
        "X-Qi-Total": str(size),
    }


def probe_policy(addr: str, identity: QiIdentity, timeout: int = 8) -> dict:
    """投送前问一句：对方对我的限额与余量。旧版节点/拒答则返回 ok=False。"""
    hdrs = file_headers(identity, "policy", "-")
    req = urllib.request.Request(f"http://{addr}/qi/policy", headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "error": str(e)}


def put_file(addr: str, identity: QiIdentity, src: str | Path,
             name: str = "", chunk: int = 4 << 20, timeout: int = 120,
             tid: str | None = None, progress=None,
             probe: bool = True) -> dict:
    """把本地文件推到对端。返回 {tid, sha256, size, response}。
    probe=True 时先发 /qi/policy 探询，明知超限/被拒就不盲传（对方不答则照旧直接投）。"""
    p = Path(src).expanduser()
    if not p.is_file():
        return {"ok": False, "error": f"没有这个文件: {p}"}
    name = name or p.name
    size = p.stat().st_size
    sha = sha256_file(p)
    tid = tid or f"xfer-{int(time.time())}-{secrets.token_hex(4)}"
    if probe:
        pol = probe_policy(addr, identity)
        if pol.get("ok"):
            mb = size / 1048576
            if pol.get("deny"):
                return {"ok": False, "error": "对方对你拒收", "tid": tid}
            if pol.get("max_file_mb") and size > pol["max_file_mb"] * 1048576:
                return {"ok": False, "tid": tid,
                        "error": f"对方对你的单文件上限 {pol['max_file_mb']:g}MB，"
                                 f"文件 {mb:.1f}MB，未发送"}
            if pol.get("quota_left", 1 << 62) < size:
                return {"ok": False, "tid": tid,
                        "error": f"对方收件配额仅余 {pol['quota_left'] >> 20}MB，未发送"}
            if pol.get("daily_left", 1 << 62) < size:
                return {"ok": False, "tid": tid,
                        "error": f"你在对方处今日仅余 {pol['daily_left'] >> 20}MB，未发送"}
    sent = 0
    resp = {}
    with open(p, "rb") as f:
        while sent < size:
            block = f.read(min(chunk, size - sent))
            if not block:
                break
            hdrs = file_headers(identity, "put", tid, name, sha, size)
            hdrs["Content-Type"] = "application/octet-stream"
            hdrs["Content-Range"] = f"bytes {sent}-{sent + len(block) - 1}/{size}"
            req = urllib.request.Request(f"http://{addr}/qi/file/{tid}",
                                         data=block, method="PUT", headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    resp = json.loads(r.read().decode() or "{}")
            except urllib.error.HTTPError as e:
                return {"ok": False, "error": f"HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}",
                        "tid": tid, "sent": sent}
            except urllib.error.URLError as e:
                # 服务端超限/配额拒绝会先响应再断连，半截 body 撞上 RST 即表现为断连
                return {"ok": False, "error": f"发送被服务端拒绝或断连: {e.reason}",
                        "tid": tid, "sent": sent}
            sent += len(block)
            if progress:
                progress(sent, size)
    return {"ok": bool(resp.get("ok")), "tid": tid, "sha256": sha, "size": size,
            "response": resp}


def get_file(addr: str, identity: QiIdentity, tid: str, dest: str | Path,
             name: str = "", timeout: int = 120, progress=None) -> dict:
    """从对端下载文件（支持断点续传）。"""
    dest = Path(dest).expanduser()
    if dest.is_dir():
        dest = dest / (name or tid)
    dest.parent.mkdir(parents=True, exist_ok=True)
    have = dest.stat().st_size if dest.exists() else 0
    # 服务端 GET 分支按空 name / 空 sha 验签（见 do_GET → _file_auth("get", tid)），
    # 这里必须同样用空 name 签，否则 401「签名无效」。name 只用于本地落盘命名。
    hdrs = file_headers(identity, "get", tid)
    if have:
        hdrs["Range"] = f"bytes={have}-"
    req = urllib.request.Request(f"http://{addr}/qi/file/{tid}", headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            total = int(r.headers.get("Content-Length", 0) or 0)
            if r.status == 206 and have:
                total += have
            mode = "ab" if (r.status == 206 and have) else "wb"
            got = have if mode == "ab" else 0
            with open(dest, mode) as f:
                for block in iter(lambda: r.read(1 << 20), b""):
                    f.write(block)
                    got += len(block)
                    if progress:
                        progress(got, total)
            return {"ok": True, "path": str(dest), "size": got, "expected": total,
                    "sha256": sha256_file(dest)}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}"}


def list_files(addr: str, identity: QiIdentity, timeout: int = 15) -> list[dict]:
    hdrs = file_headers(identity, "list", "-", "", "", 0)
    req = urllib.request.Request(f"http://{addr}/qi/files", headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode()).get("files", [])
    except Exception:                                        # noqa: BLE001
        return []


# ── 接收端 ──────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "qi-net/0.2"
    ddir: str | Path | None = None

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- 文件通道鉴权 --
    def _file_auth(self, op: str, tid: str, name: str = "", sha: str = ""):
        did = self.headers.get("X-Qi-Did", "")
        ts = self.headers.get("X-Qi-Ts", "")
        sig = self.headers.get("X-Qi-Sig", "")
        if not (did and ts and sig):
            return "", "缺签名头"
        try:
            tsi = int(ts)
        except ValueError:
            return "", "ts 非法"
        drift = abs(time.time() - tsi)
        if drift > TS_WINDOW:
            return "", f"ts 超窗（{int(drift)}s）"
        payload = {"did": did, "ts": tsi, "op": op, "tid": tid,
                   "name": name, "sha256": sha}
        if not verify_signed(payload, sig, did):
            return "", "签名无效"
        return did, ""

    def do_GET(self):                                        # noqa: N802
        if self.path.startswith("/qi/health"):
            ident = load_identity(self.ddir)
            return self._json(200, {"ok": True, "did": ident.node_id,
                                    "inbox": len(inbox(1000, self.ddir)),
                                    "agent": self.server_version})
        if self.path.startswith("/qi/policy"):
            # 发前探询：签名后回答“我对你个人的限额与余量”，不泄露他人条目
            did, err = self._file_auth("policy", "-")
            pol = load_policy(self.ddir)
            if not pol["disclose"]:
                return self._json(403, {"ok": False, "error": "不提供策略"})
            if err:
                return self._json(401, {"ok": False, "error": err})
            lim = policy_for_did(pol, did)
            fdir = files_dir(self.ddir)
            quota_left = int(lim["quota_gb"] * 2**30) - _files_used(fdir)
            daily_left = int(lim["daily_gb"] * 2**30) - _today_from_did(fdir, did)
            return self._json(200, {"ok": True, "did": did,
                                    "deny": bool(lim.get("deny")),
                                    "max_file_mb": lim["max_file_mb"],
                                    "msg_max_kb": lim["msg_max_kb"],
                                    "quota_left": max(0, quota_left),
                                    "daily_left": max(0, daily_left)})
        if self.path.startswith("/qi/files"):
            did, err = self._file_auth("list", "-")
            if err:
                return self._json(401, {"ok": False, "error": err})
            out = []
            for meta in sorted(files_dir(self.ddir).glob("*.meta.json")):
                try:
                    out.append(json.loads(meta.read_text()))
                except Exception:                            # noqa: BLE001
                    continue
            return self._json(200, {"ok": True, "files": out})
        if self.path.startswith("/qi/file/"):
            tid = urllib.parse.unquote(self.path.rsplit("/", 1)[-1])
            did, err = self._file_auth("get", tid)
            if err:
                return self._json(401, {"ok": False, "error": err})
            fdir = files_dir(self.ddir)
            metas = list(fdir.glob(f"{tid}.meta.json"))
            if not metas:
                return self._json(404, {"ok": False, "error": "没有这个文件"})
            meta = json.loads(metas[0].read_text())
            fp = Path(meta["path"])
            if not fp.exists():
                return self._json(410, {"ok": False, "error": "文件已被清理"})
            size = fp.stat().st_size
            start, status = 0, 200
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes="):
                try:
                    start = int(rng[6:].split("-")[0])
                    status = 206
                except ValueError:
                    start = 0
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(max(0, size - start)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("X-Qi-Sha256", meta.get("sha256", ""))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
            self.end_headers()
            with open(fp, "rb") as f:
                f.seek(start)
                shutil.copyfileobj(f, self.wfile, 1 << 20)
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_PUT(self):                                        # noqa: N802
        if not self.path.startswith("/qi/file/"):
            return self._json(404, {"ok": False, "error": "not found"})
        tid = urllib.parse.unquote(self.path.rsplit("/", 1)[-1])
        name = urllib.parse.unquote(self.headers.get("X-Qi-Name", "")) or tid
        sha = self.headers.get("X-Qi-Sha256", "")
        try:
            total = int(self.headers.get("X-Qi-Total", "0") or 0)
        except ValueError:
            total = 0
        did, err = self._file_auth("put", tid, name, sha)
        if err:
            return self._json(401, {"ok": False, "error": err})
        # —— 护栏：白名单 → 单文件上限 → 目录配额 → DID当日配额（写盘前全部拦截）——
        # 拒绝即断开：不必读完对方 body（那正是要防的洪泛），由 close_connection 收尾
        try:
            declared_len = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            declared_len = 0
        expect = max(total, declared_len)
        pol = load_policy(self.ddir)
        lim = policy_for_did(pol, did)
        al = lim.get("allow_list") or []
        if lim.get("deny") or (al and not any(did.startswith(a) for a in al)):
            self.close_connection = True
            return self._json(403, {"ok": False, "error": "发件方被拒收", "did": did})
        if expect > lim["max_file_mb"] * 1048576:
            self.close_connection = True
            return self._json(413, {"ok": False,
                                    "error": f"单文件超上限 {lim['max_file_mb']:g}MB",
                                    "total": expect})
        fdir0 = files_dir(self.ddir)
        _sweep_stale_parts(fdir0)
        if _files_used(fdir0) + expect > lim["quota_gb"] * 2**30:
            self.close_connection = True
            return self._json(507, {"ok": False,
                                    "error": f"收件目录配额 {lim['quota_gb']}GB 不足"})
        if _today_from_did(fdir0, did) + expect > lim["daily_gb"] * 2**30:
            self.close_connection = True
            return self._json(429, {"ok": False,
                                    "error": f"该发件方今日配额 {lim['daily_gb']}GB 用尽"})

        start = 0
        rng = self.headers.get("Content-Range", "")
        if rng.startswith("bytes "):
            try:
                start = int(rng[6:].split("-")[0])
            except ValueError:
                start = 0
        n = int(self.headers.get("Content-Length", "0") or 0)
        fdir = files_dir(self.ddir)
        part = fdir / f"{tid}.part"
        if start > 0 and part.exists() and part.stat().st_size == start:
            mode = "ab"
        else:
            mode = "wb"
            start = 0
        wrote = 0
        with open(part, mode) as f:
            left = n
            while left > 0:
                block = self.rfile.read(min(1 << 20, left))
                if not block:
                    break
                f.write(block)
                wrote += len(block)
                left -= len(block)

        size = part.stat().st_size
        if total and size < total:
            return self._json(200, {"ok": True, "partial": True, "tid": tid,
                                    "received": size, "total": total})
        got = sha256_file(part)
        if sha and got != sha:
            part.unlink(missing_ok=True)
            return self._json(400, {"ok": False, "error": "sha256 不匹配",
                                    "expected": sha, "got": got})
        safe = "".join(c if c.isalnum() or c in "._-（）()《》 中文" else "_" for c in name)[:120]
        final = fdir / f"{tid}__{safe}"
        part.replace(final)
        meta = {"tid": tid, "name": name, "sha256": got, "size": size,
                "from": did, "path": str(final), "ts": datetime.datetime.now().isoformat(timespec="seconds")}
        (fdir / f"{tid}.meta.json").write_text(json.dumps(meta, ensure_ascii=False))
        print(f"📦 收到文件 {name}  {size/1048576:.1f}MB  from {did[:20]}…", flush=True)
        return self._json(200, {"ok": True, "tid": tid, "sha256": got,
                                "size": size, "name": name, "stored": str(final)})

    def do_POST(self):                                       # noqa: N802
        if not self.path.startswith("/qi/message"):
            return self._json(404, {"ok": False, "error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        if n > load_policy(self.ddir)["defaults"]["msg_max_kb"] * 1024:
            return self._json(413, {"ok": False, "error": "消息超上限"})
        try:
            env = json.loads(self.rfile.read(n).decode())
        except Exception as e:                               # noqa: BLE001
            return self._json(400, {"ok": False, "error": f"bad json: {e}"})
        ok, reason = verify_envelope(env)
        if not ok:
            return self._json(401, {"ok": False, "error": reason})
        if seen(env["message_id"], self.ddir):
            return self._json(200, {"ok": True, "duplicate": True,
                                    "message_id": env["message_id"]})
        p = store(env, ok, reason, self.ddir)
        me = load_identity(self.ddir).node_id
        if env.get("to") not in ("", me):
            return self._json(200, {"ok": True, "warning": "to!=me", "to": env.get("to"),
                                    "me": me, "message_id": env["message_id"]})
        return self._json(200, {"ok": True, "stored": p.name,
                                "message_id": env["message_id"]})

    def log_message(self, format, *args):                     # noqa: A002
        pass                                                   # 静音


class _DualStackServer(ThreadingHTTPServer):
    """监听 "::" 时同时接受 IPv6 与 IPv4（IPv4-mapped），避免只绑 v4 把 v6 挡在外面。"""
    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        super().server_bind()


def serve(port: int, host: str = "0.0.0.0", d: str | Path | None = None):
    inbox_dir(d)
    Handler.ddir = d
    # "::" → 双栈（IPv6 + IPv4-mapped），"0.0.0.0" → 仅 IPv4
    cls = _DualStackServer if ":" in host else ThreadingHTTPServer
    srv = cls((host, port), Handler)
    ident = load_identity(d)
    print(f"契网接收端 {host}:{port} did={ident.node_id}", flush=True)
    srv.serve_forever()
