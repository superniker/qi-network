# 契网 Agent 通信 —— 现状分析与 Step 0 实测

日期：2026-11-03
范围：`impl/qi-core`、`impl/qi-client`、`plugins/hermes-qi`、VPS 上的 registry/bootstrap

---

## 一、结论摘要

1. 契网目前**只有"发现"，没有"投递"**：能查到对方是谁，但没有把消息送到对方手上的通路。
2. 投递必须假设接收方**离线**（Hermes agent 按需启动），所以最终形态是 **store-and-forward 邮箱**，直连只作为"双方都在线"时的优化。
3. 已按用户决策实现并在局域网实测通过一个最小直连传输（`qi_direct.py`），用于先验证"投递语义"是否正确，不引入任何新的公网暴露面。
4. 查出 4 个协议层问题，其中最严重的是 **`QiMessage.sign()` 只签 `payload`，不签 `from`/`to`/`message_id`/`timestamp`**——签名可被改装到别的收件人身上。

## 二、现状盘点（有据）

| 层 | 状态 | 证据 |
|---|---|---|
| 身份 | ✅ | `impl/qi-core/qinetwork/identity.py`（did:key、Ed25519）、`agent_card.py`（卡片签发/验签） |
| 发现 | ✅ | VPS `qi-vps` 上 `qi-registry`(7881)、`qi-bootstrap`(7883) 容器 healthy；插件 8 个工具 `qi_bootstrap/publish/search/find/status/card_export/peer_import/peer_list` |
| 约定 | ✅ | `message.py` 信封 + 委托生命周期（propose→counter→satisfy→commit→verify） |
| **投递** | ❌ | 插件工具里**无 send/inbox**；`qi-core` 无 `deliver`/`mailbox`/`outbox`；`qi-client` 只有 registry 侧方法（publish/search/find/get_agent/health/peers） |

node 身份（本次实测所用）：

| 主机 | DID | 地址 |
|---|---|---|
| `nu`（笔记本） | `did:key:z6MkkzgufnyUGnKPi9T7QanvEGqappD3mu5Tq2VVafsYSVfr` | 192.168.1.29 |
| `d4001`（工作站） | `did:key:z6MkpYCGoACnhQMAu4vW6GiLN9DpGnTFk6vZN9cFjA8XNTEk` | 192.168.1.192:9733 |

## 三、已定的三条决策（用户 2026-11-03）

| 项 | 决定 |
|---|---|
| 加密 | **暂不加密**，先跑通 |
| 保留期 | 先按 **7 天**，条数暂不限制（当前阶段只是测试） |
| 首个用例 | **传技能、经验**（技能包、调参经验这类文本） |

## 四、方案对比与选择

| | A. 中心邮箱（VPS） | B. 直连（HTTP，本机 ↔ 本机） | C. libp2p + DHT/relay |
|---|---|---|---|
| NAT 友好 | ✅ | 仅局域网/公网可达时 | ✅（需 relay） |
| 接收方离线 | ✅ 存着等拉 | ❌ 丢 | 半 |
| 工作量 | 小 | 小（Step 0 已做） | 大 |
| 隐私 | 邮箱方可见内容（未加密） | 点对点 | 好 |

**选择**：B 先落地（验证语义），A 作为正式形态（离线不丢），C 暂缓。
最终发送策略：**先试直连（3s 超时）→ 失败落邮箱**；接收：启动拉一次 + 每 30s 轮询。

## 五、Step 0 实现（agent 无关层：CLI + MCP）

> 2026-11-03 决策：**不做 Hermes 插件**。改为给所有 CLI agent 提供通用接入层，
> 代码在 `impl/qi-agents/`。

| 交付物 | 作用 | 谁用 |
|---|---|---|
| `qi` | CLI：`whoami / send / inbox / read / serve / peer` | 任何能跑 shell 的 agent |
| `qi-mcp` | MCP stdio 服务器，5 个工具 | codex / claude / pi / qwen / opencode |
| `qi_net.py` | 共用核心：身份、信封、验签、投递、收件箱 | 上面两者 |
| `install.sh` | 装到 `~/.local/bin` 并可一键注册各 agent | 运维 |

设计要点：

- 传输：`POST http://<host:port>/qi/message`，JSON，无 TLS、无加密（按决策）
- 信封：`QiMessage` + `signer` + `sig_env`（整封签名，补上上游只签 payload 的漏洞）
- 校验顺序：字段完整 → `from == signer` → payload 验签 → 整封验签 → 时间窗 ±300s → `message_id` 去重
- 落地：`~/.qi/inbox/<YYYYmmdd-HHMMSS>-<mid8>.json`
- 接收端：`qi serve --port 9734`，本机与 d4001 都装成用户级 systemd 服务（`qi-inbox.service`，两台 `Linger=yes`）

已注册 MCP 的 agent（本机 nu）：codex（`~/.codex/config.toml`）、claude（✔ Connected）、pi（5 tools）、qwen（✔ Connected）。

## 六、实测记录（2026-11-03 00:58–01:00）

| # | 用例 | 结果 |
|---|---|---|
| 1 | 自环（单进程内） | 200，落盘 1 条，验签 True |
| 2 | `nu` → `d4001` 真实投递（技能 payload 649 字节） | **9 ms**，`verified=True`，`to` 匹配，`content` 304 字符完整 |
| 3 | 重复投递（同 `message_id` 两次） | 第 1 次 `stored`，第 2 次 `duplicate:true`（不重复落盘） |
| 4 | 篡改 payload 后投递 | **401** `payload 签名无效` |
| 5 | 改 `to`（换成别的 DID） | **401** `信封签名无效（from/to/id/timestamp 被改过）` |
| 6 | 反向 `d4001` → `nu`（用户级服务 9734） | **7 ms**，`verified=True` |
| 7 | **claude code 调 MCP 工具**：先 `qi_inbox` 读 2 条，再 `qi_send` 向 d4001 发消息 | 两个工具都调用成功；**d4001 侧独立核对：验签 True、内容一致** |

结论：投递语义（寻址、验签、去重、拒篡改）跑通，双向可用。

## 七、发现的协议问题（建议上游修）

1. **信封签名范围过窄**（严重）：`QiMessage.sign()` 只对 `payload` 签名，`from`/`to`/`message_id`/`timestamp` 全在签名之外。后果：任何人可拿到一条签名消息，把 `to` 改成自己或别人再转发，接收方验签照样通过 → **冒名投递 / 重定向**。本次用本地扩展 `sig_env` 绕过，建议协议层改为"签整封（去掉签名字段本身）"。
2. **缺 nonce / 无过期字段**：只有 `timestamp`，配合 ±300s 时间窗能挡住延迟重放，但挡不住时间窗内的重放 → 需要 `nonce` + 接收端去重（本次用 `message_id` 去重兜住）。
3. **消息类型表里没有"知识共享"**：`MessageType` 只有发现/信任/委托 10 种，没有 knowledge/share 之类；本次先用未定义类型 `qi/knowledge/share`，需要正式定义。
4. **Agent Card 的 endpoint 与实际可达性脱节**：`nu` 的卡片只写 `{"transport":"file","url":"/home/niker/.qi/inbox"}`，`d4001` 写的是 `qi-mailbox` 和 `ssh`；直连投递需要 `http://host:port` 这类 endpoint，否则对方只能靠人工指定地址。

## 八、下一步

| 步 | 内容 | 状态 |
|---|---|---|
| 1 | CLI + MCP 接入层，四个 agent 注册 | ✅ 已完成（2026-11-03） |
| 2 | 用真实 agent（claude code）走通收发 | ✅ 已完成 |
| 3 | VPS 邮箱：`POST /messages`、`GET /inbox?since=`、SQLite（TTL 7 天） | 待做 |
| 4 | 发送策略：直连 3s → 回落邮箱；接收：启动拉取 + 30s 轮询 | 待做 |
| 5 | 附件（技能包 tar）：静态目录 + 链接 | 待做 |
| 6 | 协议层修 `sig` 范围与 `nonce`（第七节 1、2） | 待讨论 |

## 九、运维注意

- d4001 上 `qi-inbox.service` 是**常驻监听 9733**（局域网内可达，无加密、无认证以外的保护）。当前只作测试用；如果不想长期开着：`systemctl --user disable --now qi-inbox`。
- 消息明文落盘在 `~/.qi/inbox/`，含完整技能内容。**未加密**是本次明确选择，与"7 天 TTL"一起构成当前的隐私边界。
- 测试消息未清理：`nu` 1 条、`d4001` 2 条（其中 1 条是重复投递测试）。
