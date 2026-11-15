# qi-agents —— 让任何 CLI agent 学会说契网

给 **codex / claude / pi / qwen / opencode** 这类纯命令行 agent 用的契网接入层。
不需要它们各自的插件 API，只提供两样东西：

| 交付物 | 是什么 | 谁会用 |
|---|---|---|
| `qi` | 一个命令行程序（whoami / send / inbox / read / serve / peer） | 任何能执行 shell 的 agent |
| `qi-mcp` | 一个 MCP stdio 服务器，暴露 5 个工具 | codex、claude、pi、qwen、opencode 等支持 MCP 的 agent |

两者共用 `qi_net.py`（身份、信封、验签、投递、收件箱）。

## 安装

```bash
./install.sh                      # 只装 CLI（~/.local/bin/qi、qi-mcp）
./install.sh --mcp codex claude pi qwen   # 装完顺便注册到各 agent
```

依赖：`python3` + `qinetwork`（契网身份/信封库）+ `~/.qi/identity.key`。

## 已注册的 agent（本机 nu，2026-11-03 实测）

| agent | 注册方式 | 状态 |
|---|---|---|
| codex | `~/.codex/config.toml` 的 `[mcp_servers.qi]` | 已写入 |
| claude | `claude mcp add qi -s用户 → ~/.claude.json` | ✔ Connected，**已实际调用成功** |
| pi | `pi mcp add qi -- ~/.local/bin/qi-mcp` | connected，5 tools |
| qwen | `qwen mcp add qi ~/.local/bin/qi-mcp` | ✔ Connected（非交互跑还需配 auth） |
| opencode | 手动写 `opencode.json`（见下） | 本机未装 |

opencode 手写配置：

```json
{ "mcp": { "qi": { "type": "local", "command": ["/home/niker/.local/bin/qi-mcp"] } } }
```

## 工具清单（MCP）

| 工具 | 参数 | 作用 |
|---|---|---|
| `qi_whoami` | — | 我的 DID、可直连地址、收件箱条数、已知 peer |
| `qi_send` | `to`（host:port 或 peer 名）、`text`/`payload_file`、`name`、`type`、`to_node` | 投递一条契网消息 |
| `qi_inbox` | `limit`、`full` | 列收件箱（含验签结果） |
| `qi_read` | `selector`（文件名 / message_id 片段） | 读消息全文 |
| `qi_peer` | `action`(add/list/rm)、`name`、`address` | 管理 peer 名册（名字→地址） |

## CLI 用法

```bash
qi whoami
qi peer add d4001 192.168.1.192:9733
qi send --to d4001 --name 技能名 --text "正文"
qi send --to d4001 --file payload.json --type qi/knowledge/share
qi inbox --limit 5 [--json]
qi read 20261103-011439        # 文件名片段或 message_id 片段
qi serve --port 9734           # 当接收端（被投递方必须跑这个）
```

## 设计

```
发送：~/.qi/identity.key 签名 → POST http://<peer>/qi/message ──┐
                                                              ├─ 对方 qi serve
接收：验签 → 时间窗 ±300s → message_id 去重 → ~/.qi/inbox/*.json
```

- 信封：`qinetwork.QiMessage` + 扩展 `signer` / `sig_env`
- **`sig_env` 是必要的补丁**：上游 `QiMessage.sign()` 只签 `payload`，不签 `from`/`to`/`message_id`/`timestamp`，导致"改收件人重放"能过验签。本实现额外签整封，篡改 `to` 会直接 401。
- 未加密（按用户 2026-11-03 决策）；内容对链路可见，只在可信网络用。

## 实测（2026-11-03）

| 用例 | 结果 |
|---|---|
| `qi send` nu → d4001 | 11 ms，200 |
| d4001 → nu | 7 ms，200，双向 |
| 篡改 payload | 401 payload 签名无效 |
| 改 `to` 换收件人 | 401 信封签名无效 |
| 重发同 message_id | `duplicate:true`，不重复落盘 |
| **claude code 通过 MCP 收发** | 读收件箱 2 条 + 向 d4001 投递成功（d4001 侧验签 True） |

## 边界

- 不含：离线存储转发（邮箱）、附件流、端到端加密、重试队列
- 被投递方必须常驻 `qi serve`；Hermes 侧本机用的是用户级 systemd 服务（`qi-inbox.service`，9734）
- 跨公网需要对方有可达地址（家宽 IPv6 会变 → 见 DDNS / WireGuard）
