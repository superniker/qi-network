#!/usr/bin/env python3
"""契网未读看门狗（n2080 侧）。

背景（2026-11-20 排查）：
  `qi serve` 只收信落盘，`qi_task.py worker` 只认 COVENANT_PROPOSE。
  `task` / `qi/task/request` 类消息没有任何进程消费，全靠 agent 人肉上线才处理，
  于是出现「任务发进去 55 分钟没动静」「催办后才一次性回两条」。

本脚本补上缺失的那一环：定时检查未读，一有新的就通知对端（本机 192.168.1.29:9734），
让 agent 不必靠人想起来才去看 inbox。

设计约束：
  - 只读本地 events.jsonl，不改任何消息状态（ack 仍由处理者负责）
  - 只通知「上次通知之后新出现的」消息，不重复骚扰
  - 发不出去就不记账，下一轮自然重试
  - 单次最多列 8 条摘要，避免消息体爆炸

用法：
  python3 qi_watchdog.py            # 正常跑一次（cron 用）
  python3 qi_watchdog.py --dry-run  # 只打印将发送的内容，不发
  python3 qi_watchdog.py --reset    # 清空已通知记录（下次全量重报）
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

QI_ROOT = Path.home() / "qi-src"
sys.path.insert(0, str(QI_ROOT / "qi-agents"))
sys.path.insert(0, str(QI_ROOT / "qi-core"))

DATA_DIR = os.environ.get("QI_WATCH_DATA", str(Path.home() / ".qi-node"))
STATE_PATH = Path(DATA_DIR) / "watchdog-state.json"
# 本机（xiaomao）的 DID 与内网地址。地址用 IPv4 内网：跨运营商 IPv6 前缀会变，内网不会。
TARGET_DID = os.environ.get(
    "QI_WATCH_TARGET_DID",
    "did:key:z6MkkzgufnyUGnKPi9T7QanvEGqappD3mu5Tq2VVafsYSVfr",
)
TARGET_ADDR = os.environ.get("QI_WATCH_TARGET", "192.168.1.29:9734")
MAILBOX = os.environ.get("QI_MAILBOX", "http://qi-network.net:7883")
NOTIFY_TYPE = "qi/notice/unread"        # 不在自动回执白名单里 → 天然防循环
MAX_KEEP_IDS = 800
MAX_LIST = 8


def load_state() -> dict:
    try:
        st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(st, dict):
            raise ValueError("state not dict")
        st.setdefault("notified", [])
        return st
    except Exception:
        return {"notified": [], "last_check": None, "last_notify": None}


def save_state(st: dict) -> None:
    st["notified"] = list(st.get("notified") or [])[-MAX_KEEP_IDS:]
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except Exception as e:                                   # noqa: BLE001
        print(f"状态写入失败: {e}", file=sys.stderr)


def build_body(fresh: list[dict], total: int) -> str:
    host = os.uname().nodename
    lines = [f"[{host}] {len(fresh)} 条新未读（共 {total} 条待处理）", ""]
    for r in fresh[:MAX_LIST]:
        typ = r.get("type") or "?"
        frm = r.get("from_short") or ""
        prev = (r.get("body_preview") or "").strip()
        lines.append(f"· {typ} ← {frm}")
        if prev:
            lines.append(f"  {prev[:90]}")
    if len(fresh) > MAX_LIST:
        lines.append(f"… 还有 {len(fresh) - MAX_LIST} 条")
    lines += ["", "处理完请：qi --data ~/.qi-node ack <message_id>"]
    return "\n".join(lines)


def main() -> int:
    dry = "--dry-run" in sys.argv
    if "--reset" in sys.argv:
        save_state({"notified": [], "last_check": None, "last_notify": None})
        print("已重置已通知记录")
        return 0

    os.environ.setdefault("QI_NOTIFY_EVENTS", str(Path(DATA_DIR) / "events.jsonl"))
    try:
        import qi_notify                                     # noqa: PLC0415
        import qi_net                                        # noqa: PLC0415
    except Exception as e:                                   # noqa: BLE001
        print(f"导入失败: {e}", file=sys.stderr)
        return 1

    st = load_state()
    seen = {str(x) for x in (st.get("notified") or [])}
    try:
        items = qi_notify.unread()
    except Exception as e:                                   # noqa: BLE001
        print(f"读未读失败: {e}", file=sys.stderr)
        return 1

    fresh = [r for r in items if str(r.get("message_id")) not in seen]
    st["last_check"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    if not fresh:
        save_state(st)
        return 0

    body = build_body(fresh, len(items))
    if dry:
        print(f"── dry-run，将发往 {TARGET_ADDR}（DID {TARGET_DID[-8:]}）──")
        print(body)
        return 0

    try:
        ident = qi_net.load_identity(DATA_DIR)
    except Exception as e:                                   # noqa: BLE001
        print(f"读身份失败: {e}", file=sys.stderr)
        return 1

    r = qi_net.send(
        TARGET_ADDR, NOTIFY_TYPE,
        {"body": body, "count": len(fresh), "total": len(items)},
        ident, to_node=TARGET_DID, timeout=8.0, mailbox=MAILBOX,
    )
    if r.get("ok"):
        st["notified"] = list(seen | {str(x["message_id"]) for x in fresh if x.get("message_id")})
        st["last_notify"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        save_state(st)
        mode = "中转" if r.get("queued") else f"{r.get('ms', '?')}ms"
        print(f"已通知 {len(fresh)} 条 → {TARGET_ADDR}（{mode}）")
        return 0

    save_state(st)          # 没发成功 → 不记已通知，下轮重试
    print(f"通知失败: {r.get('error')}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
