"""
契网 Hermes Plugin — Qi Network Plugin for Hermes Agent

Provides Qi Network tools for Hermes Agent:
  qi_bootstrap     — connect to the DHT network
  qi_publish       — publish Agent Card to registry + DHT
  qi_search        — search registry for agents by capability
  qi_find          — find agent by DID via DHT
  qi_status        — show network status
  qi_peer_list     — list known peers
  qi_card_export   — export signed Agent Card
  qi_peer_import   — import a peer's Agent Card

Powered by qi-client SDK (cross-platform, zero system deps).
"""

import json
import os
import logging
import sys
from pathlib import Path

# ── 依赖兜底：各机器的 qi-core / qi-client 位置不同，先挂到 sys.path ──
_HERE = Path(__file__).resolve()
_CANDS = [
    os.environ.get("QI_CORE_DIR", ""),
    str(_HERE.parents[3] / "impl" / "qi-core"),          # 仓库内 plugins/hermes-qi/../../
    str(Path.home() / "qi-src" / "qi-core"),
    str(Path.home() / "hot4" / "开发" / "qi-network" / "impl" / "qi-core"),
]
_CLIENT_CANDS = [
    os.environ.get("QI_CLIENT_DIR", ""),
    str(_HERE.parents[3] / "impl" / "qi-client"),
    str(Path.home() / "qi-src" / "qi-client"),
    str(Path.home() / "hot4" / "开发" / "qi-network" / "impl" / "qi-client"),
]
for _c in _CANDS + _CLIENT_CANDS:
    if _c and Path(_c).is_dir() and _c not in sys.path:
        sys.path.insert(0, _c)

logger = logging.getLogger("hermes-qi")

try:
    from qinetwork import QiIdentity, AgentCard, Skill, Endpoint, PeerStore
    _HAVE_CORE = True
except ImportError as _e:                                   # noqa: BLE001
    logger.warning("qinetwork 不可用（发现层工具将不可用）: %s", _e)
    _HAVE_CORE = False

try:
    from qi_client import QiClient, QiConfig
    _HAVE_CLIENT = True
except ImportError:                                         # noqa: BLE001
    _HAVE_CLIENT = False


# ─── Node State ──────────────────────────────────────────

class QiNode:
    """Hermes Agent as a Qi Network node — backed by qi-client SDK."""

    def __init__(self, data_dir: str = "~/.qi"):
        self.data_dir = Path(data_dir).expanduser()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.key_path = self.data_dir / "identity.key"
        self.card_path = self.data_dir / "agent_card.json"
        self.peers_db = self.data_dir / "peers.db"
        self.config_path = self.data_dir / "config.json"

        # Identity
        self.identity = self._load_or_create_identity()

        # Peer store (local cache)
        self.peer_store = PeerStore(self.peers_db)

        # Agent Card
        self.card = self._load_or_create_card()

        # Config (addresses, bootstrap URL, etc.)
        self.config_data = self._load_config()

        # Network client
        if not _HAVE_CLIENT:
            raise RuntimeError(
                "契网的发现层工具需要 qi-client SDK，本机没装。"
                "通讯与文件工具（qi_serve / qi_inbox / qi_send_task / qi_send_file 等）不受影响。")

        self.client = QiClient(
            identity=self.identity,
            addresses=self.config_data.get("addresses", []),
            config=QiConfig(
                bootstrap_url=self.config_data.get("bootstrap_url", "http://qi-network.net:7883"),
                registry_url=self.config_data.get("registry_url", "http://qi-network.net:7881"),
                auto_connect=False,  # we connect explicitly
            ),
        )

    # ── Identity ────────────────────────────────────────

    def _load_or_create_identity(self) -> QiIdentity:
        if self.key_path.exists():
            seed = self.key_path.read_bytes()
            return QiIdentity.from_seed(seed)
        identity = QiIdentity.generate()
        self.key_path.write_bytes(identity.private_key_bytes)
        os.chmod(self.key_path, 0o600)
        return identity

    # ── Config ──────────────────────────────────────────

    def _load_config(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {}

    def _save_config(self):
        self.config_path.write_text(json.dumps(self.config_data, indent=2))

    def set_addresses(self, addresses: list[str]):
        """Update this node's reachable addresses."""
        self.config_data["addresses"] = addresses
        self.client.addresses = addresses
        self._save_config()

    # ── Agent Card ──────────────────────────────────────

    def _load_or_create_card(self) -> AgentCard:
        if self.card_path.exists():
            return AgentCard.from_json(self.card_path.read_text())
        # Create a minimal card
        card = AgentCard.create(
            identity=self.identity,
            master_identity=self.identity,
            name="hermes-agent",
            description="A Hermes Agent on Qi Network",
            master_name=os.environ.get("USER", "unknown"),
        )
        self._save_card(card)
        return card

    def _save_card(self, card: AgentCard = None):
        if card is None:
            card = self.card
        self.card_path.write_text(card.signed_json(self.identity))

    def export_card(self, output_path: str = "") -> str:
        signed = self.card.signed_json(self.identity)
        if output_path:
            Path(output_path).expanduser().write_text(signed)
            return f"Agent Card exported to {output_path}"
        return signed

    # ── Network Operations ──────────────────────────────

    def bootstrap(self) -> str:
        """Connect to Qi Network bootstrap."""
        if not self.client.addresses:
            return (
                "⚠️  No addresses configured. Set them first:\n"
                "   qi_bootstrap(addresses=['2409:abcd::1:9733', '1.2.3.4:9733'])"
            )

        result = self.client.connect()
        if result.success:
            return (
                f"✅ Connected to Qi Network!\n"
                f"   Bootstrap: {result.bootstrap_node_id[:20]}...\n"
                f"   Peers discovered: {result.peers_discovered}\n"
                f"   DID: {self.identity.node_id[:50]}..."
            )
        return f"❌ Bootstrap failed: {result.error}"

    def publish(self) -> str:
        """Publish Agent Card to registry + DHT."""
        signed = self.card.signed_json(self.identity)
        result = self.client.publish(signed)

        lines = ["📤 Publishing Agent Card..."]
        if result.registry:
            lines.append("   ✅ Registry — searchable by name/skills")
        elif result.registry_error:
            lines.append(f"   ⚠️  Registry — {result.registry_error[:100]}")

        if result.dht_stored:
            lines.append("   ✅ DHT — distributed storage")
        elif result.dht_error:
            lines.append(f"   ⚠️  DHT — {result.dht_error[:100]}")

        if not result.registry and not result.dht_stored:
            lines.append("   ❌ Network unreachable. Check connectivity.")

        return "\n".join(lines)

    def search(self, query: str = "", skill: str = "", verified: bool | None = None) -> str:
        """Search registry for agents."""
        try:
            result = self.client.search(query=query, skill=skill, verified=verified)
        except Exception as e:
            return f"❌ Search failed: {e}"

        if not result.agents:
            q = query or skill or "anything"
            return f"🔍 No agents found for '{q}'. Try a broader search."

        lines = [f"🔍 {result.total} agents found:"]
        for a in result.agents:
            skills_str = ", ".join(a.skills[:3])
            verified_str = "🔒" if a.verified else ""
            lines.append(
                f"   {a.name} ({a.master_name}) {verified_str}\n"
                f"      Skills: {skills_str}\n"
                f"      DID: {a.node_id[:40]}..."
            )
        if result.has_more:
            lines.append(f"   ... and more (page {result.page} of {result.total // result.page_size + 1})")
        return "\n".join(lines)

    def find(self, did: str) -> str:
        """Find agent by DID via DHT."""
        try:
            result = self.client.find(did)
        except Exception as e:
            return f"❌ DHT lookup failed: {e}"

        if result.status == "found":
            try:
                card_data = json.loads(result.value)
                name = card_data.get("identity", {}).get("name", "unknown")
                desc = card_data.get("identity", {}).get("description", "")
                return (
                    f"🎯 Found in DHT:\n"
                    f"   Name: {name}\n"
                    f"   Description: {desc}\n"
                    f"   Publisher: {result.publisher_did[:40]}..."
                )
            except json.JSONDecodeError:
                return f"🎯 Found in DHT (raw value, {len(result.value)} chars)"

        # Not found — show closest peers
        lines = ["🔎 Not on this node. Try these closer peers:"]
        for p in result.closest_peers:
            lines.append(f"   {p.did[:40]}... → {p.addresses}")
        return "\n".join(lines)

    # ── Peer Management ─────────────────────────────────

    def import_peer(self, card_json: str) -> str:
        """Import a peer's Agent Card."""
        from qinetwork import AgentCard
        card = AgentCard.from_json(card_json)
        raw = json.loads(card_json)
        sig_data = raw.get("signatures", {}).get("master", {})
        sig = sig_data.get("signature", "") if sig_data else ""
        verified = card.verify(sig) if sig else False

        if sig and not verified:
            return f"⚠️  Signature FAILED for '{card.name}'"

        self.peer_store.add_peer(card)
        name = card.name
        skills = [s.get("name", s.get("id", "?")) for s in card.capabilities.get("skills", [])] if hasattr(card, 'capabilities') else []
        return (
            f"✅ Peer '{name}' added.\n"
            f"   Master: {card.master_name}\n"
            f"   Skills: {skills}\n"
            f"   Verified: {verified}"
        )

    def list_peers(self) -> str:
        peers = self.peer_store.list_peers()
        if not peers:
            return "No peers yet. Import one with qi_peer_import or find them with qi_search."
        lines = [f"🕸️  {len(peers)} peers:"]
        for p in peers:
            lines.append(f"   {p.name} ({p.master_name}) — trust: {p.trust_score:.1f}")
        return "\n".join(lines)

    def status(self) -> str:
        """Full node status."""
        h = self.client.health()
        peers_local = self.peer_store.list_peers()
        return (
            f"🕸️  Qi Node Status\n"
            f"   DID: {self.identity.node_id[:50]}...\n"
            f"   Registry: {'🟢' if h['registry'] else '🔴'} qi-network.net:7881\n"
            f"   Bootstrap: {'🟢' if h['bootstrap'] else '🔴'} qi-network.net:7883\n"
            f"   DHT peers: {h['peers']}\n"
            f"   Local peers: {len(peers_local)}\n"
            f"   Addresses: {self.client.addresses or '(none configured)'}"
        )


# ─── Singleton ────────────────────────────────────────────

_node: QiNode | None = None

def _get_node() -> QiNode:
    global _node
    if _node is None:
        _node = QiNode()
    return _node


# ─── Plugin Entrypoint ─────────────────────────────────────

def register(ctx):
    """Register Qi Network tools with Hermes."""

    # ── 统一参数适配 ──
    # Hermes 有时以位置传参（整个 dict）、有时以关键字传参；旧工具全是
    # `lambda **kw:` 形式，遇到位置传参就会炸。这里在入口处统一包一层，
    # 新旧工具一起受益。
    _orig_register_tool = ctx.register_tool

    def _auto_adapt(handler):
        if handler is None or getattr(handler, "_qi_adapted", False):
            return handler

        def _adapt(*args, **kw):
            params = {}
            if args and isinstance(args[0], dict):
                params.update(args[0])
            params.update(kw)
            return handler(**params)

        setattr(_adapt, "_qi_adapted", True)
        return _adapt

    def _register_tool(**kwargs):
        kwargs["handler"] = _auto_adapt(kwargs.get("handler"))
        return _orig_register_tool(**kwargs)

    ctx.register_tool = _register_tool

    # qi_bootstrap
    def _qi_bootstrap(addresses_json: str = "", **kw):
        node = _get_node()
        if addresses_json:
            try:
                addresses = json.loads(addresses_json)
                node.set_addresses(addresses)
            except json.JSONDecodeError:
                return f"❌ Invalid JSON for addresses: {addresses_json}"
        return node.bootstrap()

    ctx.register_tool(
        name="qi_bootstrap",
        toolset="qi",
        schema={
            "name": "qi_bootstrap",
            "description": "Connect this Hermes agent to the Qi Network DHT via a bootstrap node. After connecting, the agent can discover and be discovered by other agents. Call qi_publish next to announce yourself.",
            "parameters": {
                "type": "object",
                "properties": {
                    "addresses_json": {
                        "type": "string",
                        "description": 'JSON array of your reachable addresses, ordered by priority: ["2409:abcd::1:9733", "1.2.3.4:9733"]. First call sets them, subsequent calls reuse stored addresses.'
                    },
                },
                "required": [],
            },
        },
        handler=_qi_bootstrap,
    )

    # qi_publish
    def _qi_publish(**kw):
        return _get_node().publish()

    ctx.register_tool(
        name="qi_publish",
        toolset="qi",
        schema={
            "name": "qi_publish",
            "description": "Publish this agent's signed Agent Card to the Qi Network registry (searchable) and DHT (distributed storage). Call after qi_bootstrap.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        handler=_qi_publish,
    )

    # qi_search
    def _qi_search_handler(query: str = "", skill: str = "", verified: bool | None = None, **kw):
        return _get_node().search(query=query, skill=skill, verified=verified)

    ctx.register_tool(
        name="qi_search",
        toolset="qi",
        schema={
            "name": "qi_search",
            "description": "Search the Qi Network registry for agents by capability, name, or description. Use to find OCR agents, code reviewers, data analysts, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Free-text search (name + description + skills)."},
                    "skill": {"type": "string", "description": "Filter by skill name, e.g. 'ocr', 'code-review'."},
                    "verified": {"type": "boolean", "description": "Only show verified agents."},
                },
                "required": [],
            },
        },
        handler=_qi_search_handler,
    )

    # qi_find
    def _qi_find_handler(did: str, **kw):
        return _get_node().find(did)

    ctx.register_tool(
        name="qi_find",
        toolset="qi",
        schema={
            "name": "qi_find",
            "description": "Find an agent's full Agent Card via the DHT by their DID:key. Use this to get details about a specific agent you discovered via qi_search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "did": {"type": "string", "description": "The agent's DID:key identifier (e.g. did:key:z6Mk...)."},
                },
                "required": ["did"],
            },
        },
        handler=_qi_find_handler,
    )

    # qi_status
    ctx.register_tool(
        name="qi_status",
        toolset="qi",
        schema={
            "name": "qi_status",
            "description": "Show Qi Network node status: identity, connection state, peer count, network health.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        handler=lambda **kw: _get_node().status(),
    )

    # qi_card_export
    ctx.register_tool(
        name="qi_card_export",
        toolset="qi",
        schema={
            "name": "qi_card_export",
            "description": "Export this node's signed Agent Card as JSON. Share with other agents to establish trust.",
            "parameters": {
                "type": "object",
                "properties": {
                    "output_path": {"type": "string", "description": "Optional file path. If omitted, returns the JSON string."},
                },
                "required": [],
            },
        },
        handler=lambda output_path="", **kw: _get_node().export_card(output_path),
    )

    # qi_peer_import
    ctx.register_tool(
        name="qi_peer_import",
        toolset="qi",
        schema={
            "name": "qi_peer_import",
            "description": "Import a peer's Agent Card JSON to add them to your local peer store.",
            "parameters": {
                "type": "object",
                "properties": {
                    "card_json": {"type": "string", "description": "The full signed Agent Card JSON string."},
                },
                "required": ["card_json"],
            },
        },
        handler=lambda card_json, **kw: _get_node().import_peer(card_json),
    )

    # qi_peer_list
    ctx.register_tool(
        name="qi_peer_list",
        toolset="qi",
        schema={
            "name": "qi_peer_list",
            "description": "List all known Qi Network peers in your local store.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        handler=lambda **kw: _get_node().list_peers(),
    )

    # ── 通讯与文件：给 agent 的原语，不预设流程 ──────────
    from . import messaging as _m

    def _reg(name, desc, props, handler, required=None):
        # Hermes 可能以位置传参（整个 dict）或以关键字传参，两种都兼容
        def _adapt(*args, **kw):
            params = {}
            if args and isinstance(args[0], dict):
                params.update(args[0])
            params.update(kw)
            return handler(**params)

        ctx.register_tool(
            name=name, toolset="qi",
            schema={"name": name, "description": desc,
                    "parameters": {"type": "object", "properties": props,
                                   "required": required or []}},
            handler=_adapt)

    _reg("qi_serve",
         "Start this agent's Qi Network listener so other agents can reach it. "
         "Incoming messages land in your inbox, incoming files in ~/.qi-node/files/. "
         "Idempotent — safe to call again.",
         {"port": {"type": "integer", "description": "Port to listen on, e.g. 9733"}},
         lambda port=9733, **kw: _m.serve(int(port)),
         ["port"])

    _reg("qi_inbox",
         "Read messages other agents have sent you. Each item has message_id, type, "
         "from (their DID), and payload. Use this to see work requests.",
         {"limit": {"type": "integer", "description": "How many to return (default 20)"}},
         lambda limit=20, **kw: _m.inbox(int(limit)))

    _reg("qi_send",
         "Send a message to another agent by DID. address is host:port "
         "(IPv6 as [addr]:port). Falls back to the relay mailbox when unreachable.",
         {"to_did": {"type": "string", "description": "Recipient DID"},
          "address": {"type": "string", "description": "host:port of the recipient"},
          "msg_type": {"type": "string", "description": "e.g. qi/task/request"},
          "payload_json": {"type": "string", "description": "JSON object of the payload"},
          "mailbox": {"type": "string", "description": "Optional relay mailbox base URL"}},
         lambda to_did, address, msg_type, payload_json="{}", mailbox="", **kw:
             _m.send(to_did, address, msg_type, payload_json, mailbox),
         ["to_did", "address", "msg_type"])

    _reg("qi_send_task",
         "Ask another agent to do something. Say WHAT you need and let it decide HOW — "
         "it has its own tools (files, network, its own judgement). This is how you "
         "delegate to a peer agent.",
         {"to_did": {"type": "string", "description": "The agent you're asking"},
          "address": {"type": "string", "description": "host:port of that agent"},
          "goal": {"type": "string", "description": "What you want done, in one sentence"},
          "detail": {"type": "string", "description": "Any context it needs (paths, sizes, constraints)"},
          "reply_to": {"type": "string", "description": "message_id you're responding to, if any"}},
         lambda to_did, address, goal, detail="", reply_to="", **kw:
             _m.send_task(to_did, address, goal, detail, reply_to),
         ["to_did", "address", "goal"])

    _reg("qi_reply",
         "Report back to the agent that asked you something.",
         {"to_did": {"type": "string"}, "address": {"type": "string"},
          "in_reply_to": {"type": "string", "description": "The message_id you're answering"},
          "body": {"type": "string", "description": "What you did / what happened"},
          "status": {"type": "string", "description": "done | failed | partial"}},
         lambda to_did, address, in_reply_to, body, status="done", **kw:
             _m.reply(to_did, address, in_reply_to, body, status),
         ["to_did", "address", "in_reply_to", "body"])

    _reg("qi_send_file",
         "Push a file to another agent over the Qi file channel "
         "(did:key signed, sha256 verified, resumable). Tell them the returned tid.",
         {"address": {"type": "string", "description": "host:port of the recipient"},
          "path": {"type": "string", "description": "Local file path to send"},
          "name": {"type": "string", "description": "Filename as they will see it"}},
         lambda address, path, name="", **kw: _m.send_file(address, path, name),
         ["address", "path"])

    _reg("qi_recv_file",
         "Pull a file from another agent by its transfer id (tid).",
         {"address": {"type": "string"}, "tid": {"type": "string"},
          "dest": {"type": "string", "description": "Local destination path or directory"},
          "name": {"type": "string"}},
         lambda address, tid, dest, name="", **kw: _m.recv_file(address, tid, dest, name),
         ["address", "tid", "dest"])

    _reg("qi_remote_files",
         "List files another agent has received (so you can pull them by tid).",
         {"address": {"type": "string"}},
         lambda address, **kw: _m.remote_files(address),
         ["address"])

    _reg("qi_local_files",
         "List files you have received from other agents.",
         {},
         lambda **kw: _m.local_files())

    _reg("qi_resolve",
         "Look up an agent's DID and endpoints by name or DID prefix (via the registry).",
         {"name_or_did": {"type": "string"}},
         lambda name_or_did, **kw: _m.resolve(name_or_did),
         ["name_or_did"])

    # 发现层（qi-client）可能在某些机器上没装，不影响通讯/文件工具
    try:
        node = _get_node()
        print(f"[契网 Qi] Plugin loaded. DID: {node.identity.node_id[:30]}... "
              f"通讯/文件: qi_serve qi_inbox qi_send_task qi_reply qi_send_file")
    except Exception as _e:                                 # noqa: BLE001
        print(f"[契网 Qi] 发现层未就绪（{_e}）；通讯/文件工具已注册")
