"""qi_notify —— 消息到达事件层。

设计原则（2026-11-16 定）：
  1. **事件是主通道**：每次收信都往 `~/.qi-node/events.jsonl` 追加一行。
     应用/agent 从这里拿"有新消息"的事实，自己决定弹窗、发邮件、原地处理。
  2. **桌面弹窗是可选后端**：只有显式开了 `QI_NOTIFY_DESKTOP=1` 才调 notify-send。
     默认关闭，因为收信进程可能是 systemd 服务，DISPLAY 不一定在。
  3. **绝不抛异常**：通知失败不能影响收信。整个入口包在 try/except 里。
  4. **不阻塞**：notify-send 用 Popen 不等返回，最多 2 秒超时。

消费侧（应用/agent 要做的）：
  * 想知道有没有新消息 → 读 `events.jsonl` 最后 N 行，或调 `unread_since(marker)`
  * 想"回答" → 读事件里的 message_id，处理完往 `handled.jsonl` 追加一条
  * 想知道未读数 → `unread_count()`

环境变量：
  QI_NOTIFY_DESKTOP=1     开启桌面弹窗（默认关）
  QI_NOTIFY_TITLE         弹窗标题前缀（默认 "契网"）
  QI_NOTIFY_EVENTS        事件文件路径（默认 ~/.qi-node/events.jsonl）
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

# ── 路径 ────────────────────────────────────────────────

def _base(data_dir: str | Path | None = None) -> Path:
    """事件流路径。

    优先级：环境变量 > 显式 data_dir > 与 qi_net.data_dir() 一致的默认（~/.qi）。

    ⚠ 2026-11-20 修复两处：
    1. 原先无参数、硬编码 ~/.qi-node，导致任何身份的节点（如本机 9734 用 ~/.qi）
       收到的消息事件全写进 ~/.qi-node 的账本，`qi unread` 把别人的事件算成自己的未读。
    2. 默认值必须和 qi_net.data_dir() 保持一致。那边默认 ~/.qi，这里原来默认
       ~/.qi-node —— `qi serve` 不传 --data 时身份读 ~/.qi、事件写 ~/.qi-node，
       同一进程两套目录，账本永远对不上。显式传 --data 的（如 9733 worker）
       不受影响。
    """
    p = os.environ.get("QI_NOTIFY_EVENTS")
    if p:
        return Path(p).expanduser()
    if data_dir:
        return Path(data_dir).expanduser() / "events.jsonl"
    return Path.home() / ".qi" / "events.jsonl"


def events_path(data_dir: str | Path | None = None) -> Path:
    return _base(data_dir)


def handled_path(data_dir: str | Path | None = None) -> Path:
    return events_path(data_dir).with_name("handled.jsonl")


# ── 事件写入 ─────────────────────────────────────────────

_MAX_BYTES = 5 * 1024 * 1024      # 5MB 后轮转一次


def _rotate(p: Path) -> None:
    try:
        if p.exists() and p.stat().st_size > _MAX_BYTES:
            p.replace(p.with_suffix(p.suffix + ".1"))
    except Exception:                                    # noqa: BLE001
        pass


def _append(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── 摘要 ────────────────────────────────────────────────

def summarize(env: dict) -> dict:
    """从 envelope 里抽出人看/机读都需要的那几项，不要把整包塞进事件。"""
    payload = env.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {"raw": str(payload)[:200]}

    # 正文可能藏在多个键里，按优先级取第一个非空的
    body = ""
    for k in ("body", "text", "message", "content", "name", "title"):
        v = payload.get(k)
        if v:
            body = str(v)
            break

    frm = str(env.get("from", ""))
    return {
        "message_id": env.get("message_id"),
        "type": env.get("type"),
        "from": frm,
        "from_short": frm.rsplit(":", 1)[-1][:16] if frm else "",
        # 2026-11-20 新增：记下收件人，否则多身份同机时无法判断事件归属谁的账本
        "to": str(env.get("to", "") or ""),
        "to_short": (str(env.get("to", "")) or "").rsplit(":", 1)[-1][:16],
        "ts": env.get("timestamp"),
        "has_body": bool(body),
        "body_preview": body[:200].replace("\n", " "),
        "body_len": len(body),
        "payload_keys": sorted(payload.keys())[:12],
    }


# ── 桌面弹窗（可选后端）──────────────────────────────────

def _desktop_enabled() -> bool:
    return os.environ.get("QI_NOTIFY_DESKTOP", "").strip() in ("1", "true", "yes", "on")


def _notify_send(title: str, body: str) -> None:
    """异步弹一下，失败就失败——不能拖慢收信。"""
    exe = shutil.which("notify-send")
    if not exe:
        return

    def run():
        try:
            subprocess.run(
                [exe, "--app-name=契网", "--urgency=normal", title, body[:300]],
                timeout=2,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:                                # noqa: BLE001
            pass

    threading.Thread(target=run, daemon=True).start()


# ── 对外入口 ─────────────────────────────────────────────

def on_store(envelope: dict, stored_path: str | Path | None = None,
             verified: bool = True, reason: str = "",
             data_dir: str | Path | None = None) -> None:
    """消息落盘后调用。这是唯一入口，**永不抛异常**。

    data_dir 必须传调用方实际使用的数据目录，否则事件会落到别人的账本里。
    """
    try:
        rec = summarize(envelope)
        # 没有 message_id 的不是有效消息（脏数据/半包），不进事件流，免得污染未读计数
        if not rec.get("message_id"):
            return
        rec["event"] = "message.received"
        rec["at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        rec["verified"] = bool(verified)
        rec["verify_reason"] = reason
        if stored_path:
            rec["file"] = Path(stored_path).name

        p = events_path(data_dir)
        _rotate(p)
        _append(p, rec)

        if _desktop_enabled():
            title = os.environ.get("QI_NOTIFY_TITLE", "契网")
            head = f"{rec.get('type') or '消息'}  ←  {rec.get('from_short') or '?'}"
            body = rec.get("body_preview") or "（无正文摘要，需读 payload）"
            _notify_send(f"{title}: {head}", body)
    except Exception:                                    # noqa: BLE001
        pass


# ── 未读统计 ─────────────────────────────────────────────

def _read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:                        # noqa: BLE001
                    continue
    except Exception:                                    # noqa: BLE001
        pass
    return out


def handled_ids(data_dir: str | Path | None = None) -> set[str]:
    return {str(r.get("message_id")) for r in _read_jsonl(handled_path(data_dir)) if r.get("message_id")}


def unread(data_dir: str | Path | None = None) -> list[dict]:
    """还没被处理过的消息事件（按到达顺序）。"""
    done = handled_ids(data_dir)
    return [r for r in _read_jsonl(events_path(data_dir)) if str(r.get("message_id")) not in done]


def unread_count(data_dir: str | Path | None = None) -> int:
    return len(unread(data_dir))


def mark_handled(message_id: str, note: str = "", by: str = "",
                 data_dir: str | Path | None = None) -> None:
    """处理完一条，记一笔。应用/agent 答完消息后应该调这个。"""
    try:
        _append(handled_path(data_dir), {
            "message_id": str(message_id),
            "handled_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "note": note,
            "by": by or os.environ.get("QI_AGENT", ""),
        })
    except Exception:                                    # noqa: BLE001
        pass


# ── CLI（方便手工查）────────────────────────────────────

def _main() -> int:
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "unread"
    if cmd == "unread":
        items = unread()
        if not items:
            print("没有未读消息")
            return 0
        print(f"未读 {len(items)} 条：")
        for r in items:
            print(f"  [{r.get('event', '?')}] {r.get('type', '?')}")
            print(f"    from: {r.get('from_short', '?')}")
            print(f"    at  : {r.get('at', '?')}")
            if r.get("body_preview"):
                print(f"    内容: {r['body_preview'][:120]}")
            print(f"    id  : {r.get('message_id')}")
            print()
        return 0
    if cmd == "count":
        print(unread_count())
        return 0
    if cmd == "handled":
        if len(sys.argv) < 3:
            print("用法: qi_notify.py handled <message_id> [备注]")
            return 2
        mark_handled(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
        print(f"已标记 {sys.argv[2]}")
        return 0
    if cmd == "tail":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        for r in _read_jsonl(events_path())[-n:]:
            print(json.dumps(r, ensure_ascii=False))
        return 0
    print("用法: qi_notify.py {unread|count|handled <id> [备注]|tail [n]}")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
