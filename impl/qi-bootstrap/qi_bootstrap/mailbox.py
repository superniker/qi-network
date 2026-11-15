"""
Qi Mailbox —— 离线节点的存储转发（可选中转层）

契网的直连投递（qi_net.send）打不通对端时，把已签名的信封暂存在中转节点，
等收件方上线后自己来取。这样"派完就走"才成立。

设计取舍（对齐 qi_net「刻意不做」的边界）：
  * 只做存储转发，不做推送、不做附件流、不做内容加密（信封本身已签名）
  * 收件方必须用私钥签一个 did|ts 的挑战才能取信 —— 别人取不走
  * 不保证送达：中转站丢了就是丢了，客户端要能接受降级
  * TTL 到期自动清理（默认 7 天），队列超过上限丢最旧的

端点：
  POST /mailbox/<did>           投递（body = 完整信封）
  GET  /mailbox/<did>?ts=&sig=  取信（不删除）
  POST /mailbox/<did>/ack       确认已收，body {ts, sig, ids:[...]}
  GET  /mailbox-stats           运维统计
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger("qi-mailbox")

DB_PATH = os.environ.get("QI_MAILBOX_DB", "/data/qi_mailbox.db")
TTL_SECONDS = int(os.environ.get("QI_MAILBOX_TTL", str(7 * 24 * 3600)))
MAX_PER_DID = int(os.environ.get("QI_MAILBOX_MAX", "500"))
MAX_BODY = int(os.environ.get("QI_MAILBOX_MAX_BODY", str(256 * 1024)))
CHALLENGE_WINDOW = 300          # 取信签名的有效期


# ── 存储 ────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c


def init_db() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS mailbox (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient   TEXT    NOT NULL,
                message_id  TEXT    NOT NULL,
                envelope    TEXT    NOT NULL,
                received_at REAL    NOT NULL,
                expires_at  REAL    NOT NULL,
                delivered   INTEGER NOT NULL DEFAULT 0
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_mb_rcpt ON mailbox(recipient, delivered)")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mb_mid ON mailbox(message_id)")
    logger.info(f"mailbox db ready: {DB_PATH}")


def _purge(c: sqlite3.Connection) -> None:
    now = time.time()
    c.execute("DELETE FROM mailbox WHERE expires_at < ?", (now,))
    # 每人队列上限
    rows = c.execute(
        "SELECT recipient, COUNT(*) FROM mailbox WHERE delivered=0 GROUP BY recipient"
    ).fetchall()
    for rcpt, n in rows:
        if n > MAX_PER_DID:
            c.execute("""DELETE FROM mailbox WHERE id IN (
                            SELECT id FROM mailbox WHERE recipient=? AND delivered=0
                            ORDER BY id ASC LIMIT ?)""", (rcpt, n - MAX_PER_DID))


# ── 信封验签（与 qi_net.verify_envelope 同规则）─────────

def verify_envelope(env: dict) -> tuple[bool, str]:
    from qinetwork import QiIdentity

    for k in ("type", "from", "to", "payload", "signature", "signer", "sig_env"):
        if k not in env:
            return False, f"缺字段 {k}"
    if env["from"] != env["signer"]:
        return False, "from 与 signer 不一致"
    try:
        pub = QiIdentity.public_key_from_node_id(env["signer"])
    except Exception as e:                                   # noqa: BLE001
        return False, f"signer 无法解析: {e}"
    if not QiIdentity.verify_json(env["payload"], env["signature"], pub):
        return False, "payload 签名无效"
    core = {k: v for k, v in env.items() if k != "sig_env"}
    if not QiIdentity.verify_json(core, env["sig_env"], pub):
        return False, "信封签名无效"
    import datetime
    try:
        t = datetime.datetime.fromisoformat(
            env.get("timestamp", "").replace("Z", "+00:00")).timestamp()
    except Exception:                                        # noqa: BLE001
        return False, "timestamp 解析失败"
    return True, "ok"


def verify_owner(did: str, ts: str, sig: str) -> tuple[bool, str]:
    """证明取信人持有该 did 的私钥：对 'did|ts' 的签名。"""
    from qinetwork import QiIdentity

    if not did.startswith("did:key:z"):
        return False, "did 格式不对"
    try:
        t = int(ts)
    except (TypeError, ValueError):
        return False, "ts 不是整数"
    if abs(time.time() - t) > CHALLENGE_WINDOW:
        return False, f"ts 超窗（偏差 {int(abs(time.time() - t))}s）"
    try:
        pub = QiIdentity.public_key_from_node_id(did)
    except Exception as e:                                   # noqa: BLE001
        return False, f"did 无法解析: {e}"
    if not QiIdentity.verify_json({"did": did, "ts": t}, sig, pub):
        return False, "签名无效"
    return True, "ok"


# ── 操作 ────────────────────────────────────────────────

def deposit(did: str, env: dict) -> tuple[int, str]:
    raw = json.dumps(env, ensure_ascii=False)
    if len(raw.encode()) > MAX_BODY:
        return 413, json.dumps({"error": "envelope too large"}, ensure_ascii=False)

    ok, reason = verify_envelope(env)
    if not ok:
        return 401, json.dumps({"error": f"验签失败: {reason}"}, ensure_ascii=False)

    if env.get("to") and env["to"] != did:
        return 400, json.dumps({"error": "to 与 URL 收件人不一致"}, ensure_ascii=False)

    mid = str(env.get("message_id", ""))
    now = time.time()
    with _conn() as c:
        try:
            c.execute(
                "INSERT INTO mailbox(recipient, message_id, envelope, received_at, expires_at)"
                " VALUES (?,?,?,?,?)",
                (did, mid, raw, now, now + TTL_SECONDS),
            )
        except sqlite3.IntegrityError:
            return 200, json.dumps({"ok": True, "duplicate": True, "message_id": mid},
                                   ensure_ascii=False)
        _purge(c)
    return 200, json.dumps({"ok": True, "queued": True, "message_id": mid,
                            "expires_in": TTL_SECONDS}, ensure_ascii=False)


def fetch(did: str, ts: str, sig: str, limit: int = 50) -> tuple[int, str]:
    ok, reason = verify_owner(did, ts, sig)
    if not ok:
        return 401, json.dumps({"error": reason}, ensure_ascii=False)
    with _conn() as c:
        rows = c.execute(
            "SELECT message_id, envelope, received_at FROM mailbox"
            " WHERE recipient=? AND delivered=0 ORDER BY id ASC LIMIT ?",
            (did, limit),
        ).fetchall()
    msgs = []
    for mid, env_raw, recv in rows:
        try:
            msgs.append({"message_id": mid, "received_at": recv,
                         "envelope": json.loads(env_raw)})
        except Exception:                                    # noqa: BLE001
            continue
    return 200, json.dumps({"ok": True, "count": len(msgs), "messages": msgs},
                           ensure_ascii=False)


def ack(did: str, body: dict) -> tuple[int, str]:
    ok, reason = verify_owner(did, str(body.get("ts", "")), str(body.get("sig", "")))
    if not ok:
        return 401, json.dumps({"error": reason}, ensure_ascii=False)
    ids = body.get("ids") or []
    if not isinstance(ids, list):
        return 400, json.dumps({"error": "ids 必须是数组"}, ensure_ascii=False)
    with _conn() as c:
        n = 0
        for mid in ids:
            cur = c.execute(
                "UPDATE mailbox SET delivered=1 WHERE recipient=? AND message_id=?",
                (did, str(mid)))
            n += cur.rowcount
    return 200, json.dumps({"ok": True, "acked": n}, ensure_ascii=False)


def stats() -> tuple[int, str]:
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM mailbox").fetchone()[0]
        pending = c.execute("SELECT COUNT(*) FROM mailbox WHERE delivered=0").fetchone()[0]
        rcpts = c.execute(
            "SELECT COUNT(DISTINCT recipient) FROM mailbox WHERE delivered=0").fetchone()[0]
    return 200, json.dumps({"total": total, "pending": pending, "recipients": rcpts,
                            "ttl_seconds": TTL_SECONDS, "max_per_did": MAX_PER_DID},
                           ensure_ascii=False)
