#!/usr/bin/env python3
"""qi_task —— 契约的发起与执行

契网的消息层（qinetwork.message）已经有 propose / counter / commit / satisfy /
verify 六步，这个文件补上「谁来真正执行」：

  worker  接单 → 提交(commit) → 跑命令 → 交证据(satisfy) → 等验收
  propose 发契约 → 等回执 → 打印完整链路
  verify  验收 → 发 verdict（可带 trust_delta）

安全边界：**远端只能指定模板名和参数，不能指定命令**。模板由本机运维在
templates.json 里定义，propose 里的 template 不在白名单就直接拒绝。

用法：
  qi_task.py worker --port 9733 --templates ~/.qi/templates.json
  qi_task.py propose --to did:key:z... --addr n2080.so.caizhen.xin:9733 \
      --template shui5_block --args '{"url":"...","name":"HeBei","out":"/tmp/x"}' \
      --goal "抓河北块" --wait 600
  qi_task.py status
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qi_net as net                                          # noqa: E402
from qinetwork import MessageType, QiIdentity                 # noqa: E402

MAILBOX = os.environ.get("QI_MAILBOX", "http://qi-network.net:7883")


# ── 状态存储 ────────────────────────────────────────────

def tasks_dir(d=None) -> Path:
    p = net.data_dir(d) / "tasks"
    p.mkdir(parents=True, exist_ok=True)
    return p


def task_file(d, cid: str) -> Path:
    return tasks_dir(d) / f"{cid.replace(':', '_')}.json"


def load_task(d, cid: str) -> dict:
    p = task_file(d, cid)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_task(d, cid: str, rec: dict) -> None:
    rec["updated_at"] = datetime.now(timezone.utc).isoformat()
    task_file(d, cid).write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                                 encoding="utf-8")


# ── 证据 ────────────────────────────────────────────────

def collect_evidence(paths: list[str]) -> dict:
    """对产物目录做一份可复核的指纹：文件数、总字节、清单哈希。"""
    files, total = [], 0
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            continue
        if p.is_file():
            files.append(p)
        else:
            files.extend(x for x in p.rglob("*") if x.is_file())
    for f in sorted(files):
        try:
            total += f.stat().st_size
        except OSError:
            pass
    h = hashlib.sha256()
    for f in sorted(files):
        h.update(str(f.name).encode("utf-8"))
        try:
            h.update(str(f.stat().st_size).encode())
        except OSError:
            pass
    return {
        "file_count": len(files),
        "total_bytes": total,
        "manifest_sha256": h.hexdigest() if files else "",
        "sample": [str(f.name) for f in sorted(files)[:5]],
    }


# ── 模板 ────────────────────────────────────────────────

def load_templates(path: str) -> dict:
    p = Path(path).expanduser()
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def render_cmd(tpl: dict, args: dict) -> list[str]:
    """把模板里的 {placeholder} 用 args 填好。只填已知键，缺键直接报错。"""
    def sub(s: str) -> str:
        try:
            return s.format(**args)
        except KeyError as e:
            raise SystemExit(f"模板参数缺失: {e}")

    return [sub(str(x)) for x in tpl["cmd"]]


# ── worker：接单执行 ────────────────────────────────────

class Worker:
    def __init__(self, data=None, port=9733, templates_path="", auto_accept=True):
        self.d = data
        self.port = port
        self.ident = QiIdentity((net.data_dir(data) / "identity.key").read_bytes())
        self.templates_path = templates_path
        self.templates = load_templates(templates_path) if templates_path else {}
        self._tpl_mtime = 0.0
        self.auto_accept = auto_accept
        self.seen_cids: set[str] = set()
        self._stop = False

    def reload_templates_if_changed(self):
        """模板文件改了就直接生效 —— 不必重启 worker（长任务跑到一半也能加新模板）。"""
        p = self.templates_path
        if not p:
            return
        try:
            mt = Path(p).stat().st_mtime
        except OSError:
            return
        if mt > self._tpl_mtime:
            try:
                self.templates = load_templates(p)
                self._tpl_mtime = mt
                print(f"模板已热加载: {list(self.templates)}", flush=True)
            except Exception as e:                             # noqa: BLE001
                print(f"模板热加载失败（沿用旧的）: {e}", flush=True)

    # -- 收信轮询 --
    def scan(self) -> list[dict]:
        """返回还没处理过的 propose。"""
        out = []
        for item in net.inbox(200, self.d):
            env = item.get("envelope") or {}
            if env.get("type") != MessageType.COVENANT_PROPOSE:
                continue
            cid = (env.get("payload") or {}).get("covenant_id")
            if not cid or cid in self.seen_cids:
                continue
            self.seen_cids.add(cid)
            out.append(env)
        return out

    def handle(self, env: dict) -> None:
        payload = env.get("payload") or {}
        cid = payload.get("covenant_id", "")
        prop = payload.get("proposal") or {}
        frm = env.get("from", "")
        mode = payload.get("mode", "")
        template = prop.get("template", "")
        args = prop.get("args") or {}

        rec = {"covenant_id": cid, "from": frm, "mode": mode, "proposal": prop,
               "state": "received", "created_at": datetime.now(timezone.utc).isoformat()}
        save_task(self.d, cid, rec)
        print(f"[{cid[:20]}] 收到契约 from={frm[:20]}… mode={mode} tpl={template}", flush=True)

        # 只接委托
        if mode != "commission":
            self._reply(frm, "counter", cid, {"reason": f"不支持的模式: {mode}"})
            rec["state"] = "rejected"; save_task(self.d, cid, rec)
            return

        # 模板白名单（先热加载，运维改了 templates.json 不必重启）
        self.reload_templates_if_changed()
        if template not in self.templates:
            self._reply(frm, "counter", cid, {"reason": f"未知模板: {template}"})
            rec["state"] = "rejected"; save_task(self.d, cid, rec)
            print(f"[{cid[:20]}] ❌ 模板不在白名单", flush=True)
            return

        # 提交
        commit = {"status": "accepted", "finalized_proposal": prop,
                  "worker": self.ident.node_id,
                  "committed_at": datetime.now(timezone.utc).isoformat()}
        self._reply(frm, "commit", cid, commit)
        rec["state"] = "committed"; save_task(self.d, cid, rec)
        print(f"[{cid[:20]}] ✅ 已提交，开始执行", flush=True)

        # 执行（后台线程，主循环还能继续收信）
        threading.Thread(target=self._run, args=(cid, frm, template, args, rec),
                         daemon=True).start()

    def _run(self, cid, frm, template, args, rec):
        tpl = self.templates[template]
        try:
            cmd = render_cmd(tpl, args)
        except SystemExit as e:
            self._fail(cid, frm, rec, str(e)); return

        cwd = tpl.get("cwd") or str(Path.home())
        timeout = int(tpl.get("timeout", 3600))
        log_path = tasks_dir(self.d) / f"{cid.replace(':', '_')}.log"
        t0 = time.time()
        rec.update({"state": "running", "cmd": cmd, "cwd": cwd, "log": str(log_path)})
        save_task(self.d, cid, rec)

        # 前置步骤（建目录等），失败即判任务失败
        pre = tpl.get("pre")
        if pre:
            try:
                pre_cmd = render_cmd({"cmd": pre}, args)
                spec = subprocess.run(pre_cmd, cwd=cwd, capture_output=True, timeout=120)
                if spec.returncode != 0:
                    self._fail(cid, frm, rec,
                               f"pre 失败 rc={spec.returncode}: "
                               f"{spec.stderr.decode('utf-8', 'replace')[:200]}")
                    return
            except Exception as e:                           # noqa: BLE001
                self._fail(cid, frm, rec, f"pre 异常: {e}")
                return

        print(f"[{cid[:20]}] ▶ {' '.join(shlex.quote(c) for c in cmd)}", flush=True)

        try:
            with open(log_path, "wb") as lf:
                pr = subprocess.run(cmd, cwd=cwd, stdout=lf, stderr=subprocess.STDOUT,
                                    timeout=timeout)
            rc = pr.returncode
        except subprocess.TimeoutExpired:
            rc = -9
        except Exception as e:                               # noqa: BLE001
            rc = -1
            log_path.write_text(f"exec error: {e}", encoding="utf-8")

        duration = round(time.time() - t0, 1)
        paths = [p.format(**args) for p in tpl.get("evidence_paths", [])]
        ev = collect_evidence(paths)
        ev.update({"exit_code": rc, "duration_s": duration,
                   "log_tail": _tail(log_path, 800),
                   "task": template, "args": args})

        rec.update({"state": "satisfied" if rc == 0 else "failed",
                    "evidence": ev, "duration_s": duration})
        save_task(self.d, cid, rec)

        self._reply(frm, "satisfy", cid, {"conditions": [
            {"id": "exit", "ok": rc == 0, "detail": f"exit_code={rc}"},
            {"id": "output", "ok": ev["file_count"] > 0, "detail": f"{ev['file_count']} 个文件"},
            {"id": "duration", "ok": True, "detail": f"{duration}s"},
        ], "evidence": ev})
        print(f"[{cid[:20]}] 📦 已交证据 rc={rc} 文件={ev['file_count']} 耗时={duration}s",
              flush=True)

    def _fail(self, cid, frm, rec, why):
        rec["state"] = "failed"; rec["error"] = why
        save_task(self.d, cid, rec)
        self._reply(frm, "satisfy", cid, {"conditions": [
            {"id": "exit", "ok": False, "detail": why}], "evidence": {"error": why}})

    def _reply(self, to: str, kind: str, cid: str, extra: dict):
        """把 commit/satisfy 发回委托方；打不通就投信箱。"""
        peer = self._peer_addr(to)
        if kind == "commit":
            payload = {"covenant_id": cid, "commitment": extra}
            mt = MessageType.COVENANT_COMMIT
        elif kind == "counter":
            payload = {"covenant_id": cid, "response": {"status": "counter", "adjustments": extra}}
            mt = MessageType.COVENANT_COUNTER
        else:
            payload = {"covenant_id": cid, **extra}
            mt = MessageType.COVENANT_SATISFY
        if peer:
            r = net.send(peer, mt, payload, self.ident, to_node=to,
                         timeout=10, mailbox=MAILBOX)
        else:
            env = net.build_envelope(mt, payload, to, self.ident)
            r = net.deposit_mailbox(MAILBOX, to, env, self.ident)
        if not r.get("ok"):
            print(f"[{cid[:20]}] ⚠️  回执失败: {r.get('error')}", flush=True)

    def _peer_addr(self, did: str) -> str:
        """从本地 peers 表 / 卡片里找对方地址。"""
        p = net.data_dir(self.d) / "peers.json"
        try:
            peers = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
            return (peers.get(did) or {}).get("addr", "")
        except Exception:                                    # noqa: BLE001
            return ""

    def serve_forever(self):
        threading.Thread(target=net.serve, args=(self.port, "::", self.d),
                         daemon=True).start()
        print(f"worker 监听 :{self.port}  did={self.ident.node_id}", flush=True)
        print(f"模板: {list(self.templates)}  中转: {MAILBOX}", flush=True)
        while not self._stop:
            try:
                net.drain_mailbox(MAILBOX, self.ident, self.d)   # 离线期间的信
            except Exception:                                # noqa: BLE001
                pass
            for env in self.scan():
                self.handle(env)
            time.sleep(3)


def _tail(p: Path, n: int) -> str:
    try:
        data = p.read_bytes()
        return data[-n:].decode("utf-8", errors="replace")
    except OSError:
        return ""


# ── 委托方 ──────────────────────────────────────────────

def cmd_propose(a):
    ident = QiIdentity((net.data_dir(a.data) / "identity.key").read_bytes())
    cid = f"cov-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    deadline = (datetime.now(timezone.utc) + timedelta(minutes=a.deadline_min)).isoformat()
    args = json.loads(a.args) if a.args else {}
    proposal = {
        "goal": a.goal,
        "template": a.template,
        "args": args,
        "expected_output": a.expected,
        "deadline": deadline,
    }
    payload = {"covenant_id": cid, "mode": a.mode, "proposal": proposal}
    rec = {"covenant_id": cid, "role": "proposer", "to": a.to, "proposal": proposal,
           "state": "proposed", "created_at": datetime.now(timezone.utc).isoformat()}
    save_task(a.data, cid, rec)

    print(f"契约 {cid}")
    print(f"  目标 {a.to[:44]}…  模板 {a.template}")
    r = net.send(a.addr, MessageType.COVENANT_PROPOSE, payload, ident,
                 to_node=a.to, timeout=a.timeout, mailbox=MAILBOX)
    print(f"  发送: ok={r.get('ok')} queued={r.get('queued')} {r.get('error', '')}")

    if not a.wait:
        print(f"\n用这个命令看后续：qi_task.py status")
        return

    print(f"\n等待回执（最多 {a.wait}s）…")
    t0 = time.time()
    got = {"commit": False, "satisfy": False}
    while time.time() - t0 < a.wait and not got["satisfy"]:
        net.drain_mailbox(MAILBOX, ident, a.data)
        for item in net.inbox(50, a.data):
            env = item.get("envelope") or {}
            if (env.get("payload") or {}).get("covenant_id") != cid:
                continue
            t = env.get("type", "")
            if t == MessageType.COVENANT_COUNTER and not got["commit"]:
                why = (env["payload"].get("response") or {}).get("adjustments", {})
                print(f"  ⛔ 对方拒绝: {json.dumps(why, ensure_ascii=False)}")
                return
            if t == MessageType.COVENANT_COMMIT and not got["commit"]:
                got["commit"] = True
                print(f"  ✅ 已提交 @ {env['timestamp']}")
            if t == MessageType.COVENANT_SATISFY and not got["satisfy"]:
                got["satisfy"] = True
                ev = (env["payload"].get("evidence") or {})
                print(f"  📦 收到证据 @ {env['timestamp']}")
                for k in ("exit_code", "file_count", "total_bytes", "duration_s"):
                    print(f"     {k:14s} {ev.get(k)}")
                print(f"     manifest       {str(ev.get('manifest_sha256'))[:24]}…")
                rec.update({"state": "satisfied", "evidence": ev,
                            "satisfied_at": env["timestamp"]})
                save_task(a.data, cid, rec)
                print(f"\n  验收命令: qi_task.py verify --to {a.to[:20]}… "
                      f"--addr {a.addr} --covenant {cid} --verdict accepted")
                return
        time.sleep(5)
    print("  超时，没等到完整回执（消息可能在信箱里，用 qi_node.py drain 再试）")


def cmd_verify(a):
    ident = QiIdentity((net.data_dir(a.data) / "identity.key").read_bytes())
    payload = {"covenant_id": a.covenant,
               "verdict": {"status": a.verdict, "comments": a.comments}}
    if a.trust_delta is not None:
        payload["verdict"]["trust_update"] = {"delta": a.trust_delta}
    r = net.send(a.addr, MessageType.COVENANT_VERIFY, payload, ident,
                 to_node=a.to, timeout=a.timeout, mailbox=MAILBOX)
    rec = load_task(a.data, a.covenant)
    rec.update({"state": f"verified:{a.verdict}", "verdict": payload["verdict"]})
    save_task(a.data, a.covenant, rec)
    print(f"验收已发出 ok={r.get('ok')} queued={r.get('queued')}")


def cmd_status(a):
    d = tasks_dir(a.data)
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print("（没有契约记录）")
        return
    for f in files[: a.limit]:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except Exception:                                    # noqa: BLE001
            continue
        ev = r.get("evidence") or {}
        print(f"{r.get('covenant_id', '?')[:34]:36s} {r.get('state', '?'):12s} "
              f"{r.get('role', 'worker'):9s} files={ev.get('file_count', '-'):<6} "
              f"{r.get('updated_at', '')[:19]}")


def main():
    ap = argparse.ArgumentParser(description="契网契约执行器")
    ap.add_argument("--data", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("worker", help="接单并执行")
    p.add_argument("--port", type=int, default=9733)
    p.add_argument("--templates", default=str(Path.home() / ".qi" / "templates.json"))
    p.set_defaults(func=lambda a: Worker(a.data, a.port, a.templates).serve_forever())

    p = sub.add_parser("propose", help="发起契约")
    p.add_argument("--to", required=True)
    p.add_argument("--addr", required=True)
    p.add_argument("--template", required=True)
    p.add_argument("--args", default="{}")
    p.add_argument("--goal", required=True)
    p.add_argument("--expected", default="")
    p.add_argument("--mode", default="commission")
    p.add_argument("--deadline-min", type=int, default=240)
    p.add_argument("--wait", type=int, default=0, help="等待回执秒数，0=不等")
    p.add_argument("--timeout", type=float, default=15.0)
    p.set_defaults(func=cmd_propose)

    p = sub.add_parser("verify", help="验收")
    p.add_argument("--to", required=True)
    p.add_argument("--addr", required=True)
    p.add_argument("--covenant", required=True)
    p.add_argument("--verdict", choices=["accepted", "rejected"], default="accepted")
    p.add_argument("--comments", default="")
    p.add_argument("--trust-delta", type=float, default=None)
    p.add_argument("--timeout", type=float, default=15.0)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("status")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_status)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
