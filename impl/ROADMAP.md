# Qi Network — Roadmap

## 已完成 ✅

### Phase 1: Identity & Agent Card
- [x] Ed25519 keypair generation
- [x] DID:key generation
- [x] Agent Card create/sign/verify
- [x] GB/Z 185 identity code compatibility
- [x] Agent Card aliases for multi-standard support

### Phase 2: Peer Store
- [x] SQLite peer database
- [x] Peer add/list/remove/touch
- [x] Interaction history logging

### Phase 3: Message Envelope
- [x] Qi message envelope + 8 message types
- [x] Message signing
- [x] Factory methods for common message types

### Phase 4: Commission (委托) Lifecycle
- [x] Proposal → Negotiate → Satisfy → Commit → Verify → Archive
- [x] Condition system (payment, membership, credential, task, custom)
- [x] Abstract verification interface

### Phase 5: Hermes Plugin
- [x] 6 qi_* tools
- [x] File-based transport (Syncthing)
- [x] Installed to ~/.hermes/plugins/qi-network/

### Phase 6: Registry (注册中心)
- [x] FastAPI REST API
- [x] Agent CRUD (GB/Z 185 Part 4)
- [x] Discovery/Search (GB/Z 185 Part 5)
- [x] Statistics
- [x] SQLite database
- [x] Dockerfile
- [ ] Testing & deployment 🚧

### Phase 7: Protocol Spec
- [x] v0.1 specification (9 scenarios, 3 layers, 6 modes + conditions)
- [x] Design memo
- [x] GB/Z 185 compatibility appendix

## 待建 🚧

### qi-bootstrap (DHT Bootstrap Node)
- [ ] Kademlia DHT implementation
- [ ] Bootstrap node for new peers
- [ ] Agent Card DHT publishing

### qi-relay (NAT Traversal Relay)
- [ ] libp2p circuit relay
- [ ] End-to-end encryption passthrough

### qi-registry (完善)
- [ ] Web UI for browsing agents
- [ ] Federation protocol (ActivityPub sync between registries)
- [ ] API key management UI
- [ ] PostgreSQL migration

### Hermes Plugin (完善)
- [ ] Direct network transport (not just file-based)
- [ ] qi-registry integration (auto-register on startup)
- [ ] Condition verification workflows
- [ ] Partnership mode support
