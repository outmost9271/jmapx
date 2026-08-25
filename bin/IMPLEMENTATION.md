# jmapx 实现文档

本文档描述 `bin/jmapx` 的实现细节：协议机制、架构决策、实测结论与已知边界。使用方式见 `bin/USAGE.md` 与根 `README.md`。

## 1. 总览

- **零第三方依赖**：仅 Python 3 标准库（`urllib`/`json`/`argparse`/`concurrent.futures`/`fnmatch` 等）
- **协议**：JMAP（RFC 8620 核心 + RFC 8621 邮件），服务端为 Stalwart v1.0.0
- **架构**：`JmapContext` 统一封装认证、Session、账户与服务端能力；公共设施（凭据、HTTP、批量拉取、下载调度、JSON 输出）与五个子命令分离
- **错误模型**：预期错误统一抛出 `JmapxError`，由 `main` 在单一出口转换为 stderr 告警与退出码 1；下载线程中的单文件错误进入 `failed`，不会误触发线程级 `SystemExit`
- **去重原则**：CSV 解析、ids 保序去重、附件下载按 blobId 去重均有独立公共函数，避免子命令重复实现

## 2. 凭据机制

优先级：环境变量 > 身份配置文件。

- 环境变量 `JMAP_SERVER`/`JMAP_USERNAME`/`JMAP_PASSWORD` **三者必须齐全**才采用；部分设置时打印提示并回退配置文件
- 配置文件校验链（`get_creds_from_file`）：
  1. 文件存在性 → 2. **权限必须恰好 600**（`stat.S_IMODE` 比较，Windows 跳过）→ 3. JSON 合法性 → 4. 必填字段完整
  - 任一失败：stderr 告警 + 退出码 1（拒绝执行）
- 默认配置文件名 `jmapx_creds.json`（非隐藏），通过 `os.path.dirname(os.path.abspath(__file__))` 定位脚本同目录——无硬编码绝对路径
- `--creds-file` 覆盖默认位置

## 3. Session 发现与账户选择

- `GET {server}/.well-known/jmap`（Basic Auth，urllib 默认跟随重定向到 `/jmap/session`）
- 账户选择：优先 `session.primaryAccounts["urn:ietf:params:jmap:mail"]`，否则取 `accounts` 第一个 key
- **所有限制从 Session 能力声明动态读取**：`maxObjectsInGet`（500）、`maxCallsInRequest`（16）——不硬编码，服务器调整配置后自动适配
- `downloadUrl` 为 URI Template（level 1），用 `str.format` 直接替换 `{accountId}/{blobId}/{name}?accept={type}`，name 做百分号编码

## 4. HTTP 层

- `http_json`：POST JSON 请求并解析响应；401/429 专项提示；其余错误读响应体前 200 字符
- `jmap_post`：组装 `{using, methodCalls}` 请求，返回 `methodResponses`
- 方法级错误（HTTP 200 + JSON）与请求级错误（HTTP 400 `urn:ietf:params:jmap:error:limit`）分别处理
- 下载走独立 `http_open`（流式、支持 Range 头、Accept octet-stream）

## 5. 子命令实现

### 5.1 total

`Email/query` + `limit: 1` + `calculateTotal: true`，只取 `total`。

> **实测结论**：`limit: 0` 时 Stalwart 会返回全部 id（浪费传输），因此用 `limit: 1` 只取计数。

### 5.2 emails（列表 + 过滤）

**两阶段：服务端过滤（日期）→ 客户端过滤（收发件人）**

- 服务端日期过滤：`after`（含）/`before`（不含）作用于 `receivedAt`（不可变属性），RFC 3339 秒级
  - `parse_datetime_arg`：纯日期 `YYYY-MM-DD` → start=当日 00:00:00、end=次日 00:00:00（补全天）；带时间用 `datetime.fromisoformat`；无时区按本地时区（`astimezone()`）解释后转 UTC
- 客户端精确匹配：JMAP 的 `from`/`to` 过滤条件是**包含式全文搜索**（RFC 8621 §4.4.1 原文 "Looks for the text in..."），无法保证精确 → 拉取后在客户端对 `EmailAddress.email` 做大小写不敏感全等、`name` 精确相等
- query 分页（`query_all_email_ids`）：
  - `position` 逐页推进，`limit` 默认 5000；**响应没有 `hasMore` 字段**（那是 `/changes` 系列），判终条件：返回条数 < 实际 limit，或累计 ≥ total
  - 服务器钳制 limit 时以响应中的 `limit` 字段为准继续翻页
  - `calculateTotal` 仅第一页开启（RFC 提示该计算可能昂贵）
- 批量拉取（`fetch_emails_batched`，emails/detail/attachments 共用）：
  - id 按 `maxObjectsInGet` 分批 → 每 HTTP 请求组合 ≤`maxCallsInRequest` 批 → 剩余批次与降级批次放回队列继续下一请求（**无并发，串行多请求**）
  - `requestTooLarge` 时该批减半重试，批长为 1 仍失败则报错
- 本地排序：RFC 8620 §5.1 允许 `/get` 乱序返回 → 按 `(receivedAt, id)` 倒序重排
- 邮箱清单：`Mailbox/get ids=null`；若超限报 `requestTooLarge`（邮箱数 >500 的极端场景），回退 `Mailbox/query` 拿 id 再分批 get

### 5.3 detail（正文 + 附件）

- 属性白名单：元数据 + `textBody`/`htmlBody`/`bodyValues`/`attachments`
- 正文默认**不返回**，需显式 `fetchTextBodyValues`/`fetchHTMLBodyValues`；`maxBodyValueBytes` 截断保护（RFC：`bodyValues[partId].value` 为截断后的文本）
- 附件列表 `attachments` 为 `EmailBodyPart` 元数据（partId/blobId/name/type/size），附件二进制不走此通道（见 5.5）
- 输出按输入 ids 顺序重排；重复 id 去重

### 5.4 attachments（附件列表 + 过滤 + 集成下载）

- 轻量属性：仅 `id`/`subject`/`from`/`to`/`attachments`，不拉正文
- 过滤语法：`--filter GLOB`，glob 匹配（`fnmatch`），两端 `lower()` 实现大小写不敏感；`!` 前缀反向（`hit = match != invert`）
- **命中与否都返回**：`matched`/`unmatched` 两组；下载只取 matched 组
- 下载集成：收集 matched 组的 `(blobId, name)`（blobId 去重）→ 复用 `download_one_blob` → 结果并入输出 `download` 字段；失败时输出后退出码 1

### 5.5 download（并发下载）

**两级并发**：

1. 文件级：`ThreadPoolExecutor(concurrency)`，默认 16
2. 文件内：Range 分块并行（`--chunks` 默认 4），流式写 part 文件后按序合并

**Range 探测流程**（`download_one_blob`）：

```
GET Range: bytes=0-0
 ├─ 206 + Content-Range: bytes 0-0/N → 支持分块，N 为总长 → 分块并行
 ├─ 200 + Content-Length          → 服务器忽略 Range → 探测响应即全量，直接落盘
 └─ 其他                          → 单连接全量下载
```

> **实测结论**：Stalwart v1.0.0 的 download 端点**忽略 Range 头**（`bytes=0-0` 返回 200 + 全量），因此分块路径当前不触发，自动回退为"探测即全量"；实现保留分块能力以兼容支持 Range 的服务器。

**重名保护**（`unique_dest_path`）：

- 目标名已存在 → `原名-N-完整blobId.ext`（N 递增）
- 超过文件系统 `NAME_MAX`（`os.pathconf` 动态读取，默认 255 字节，UTF-8 字节计数）→ 先截断 blobId（ASCII 安全）→ 仍超限截断原名 stem → 最终兜底 `-N.ext`

**原子性与清理**：每个下载任务使用目标目录内独立的 `TemporaryDirectory(.jmapx-*)`，避免同 blobId 并发下载时临时文件碰撞；检查重名与 `os.replace` 置于同一进程锁中，消除并发同名覆盖竞态；临时目录在成功或异常时自动清理。

## 6. 实测服务器行为（Stalwart v1.0.0，2026-08）

| 行为 | 结论 | 对应处理 |
|---|---|---|
| `maxObjectsInGet` 超限（501 id） | HTTP 200 + 方法级 `requestTooLarge` | 按 Session 值分批 + 降批重试 |
| `maxCallsInRequest` 超限（17 调用） | HTTP 400 `urn:ietf:params:jmap:error:limit` | 按 Session 值组合 |
| `maxSizeRequest` 超限（11MB 请求体） | HTTP 400 + `limit: maxSizeRequest` | 客户端控制请求规模 |
| `maxConcurrentRequests=4` | 8 个短请求均成功（不能据此认定为软限制，可能未真正重叠） | JMAP API 拉取保持串行；附件下载走独立 download 端点 |
| `limit: 0` 的 query | 返回全部 id（非空数组） | total 用 `limit: 1` |
| download 端点 Range | 忽略（返回 200 全量） | 自动回退单连接 |
| `Email/query` 无 filter | 返回账户全部邮件（含 junk/trash） | 列表语义天然含垃圾箱 |

## 7. 已知边界与设计取舍

- **无多 HTTP 并发拉取**：批量依赖单请求多调用（16×500=8000/请求），按需求明确不做并发与拉取期间一致性检测（不保证快照一致，`emails` 输出无 state 字段）
- **`from`/`to` 精确匹配在客户端**：代价是全量拉取后过滤；量级几千封可接受，超大账户（>8 万封）需引入服务端过滤或分页策略
- **urllib 无连接池**：每次请求新建连接；如需 HTTP/2 多路复用需迁移 `httpx[http2]`（引入第三方依赖）
- **重定向携带 Authorization**：`.well-known/jmap` → `/jmap/session` 为同主机重定向，urllib 默认保留请求头，安全
- **权限校验**：仅 POSIX 语义（Windows 跳过），符合目标部署环境

## 8. 测试

测试文件：`tests/test_jmapx.py`，仅使用标准库 `unittest`。

覆盖范围：

- CLI 契约：五个子命令和全部关键参数名、默认并发 16/分块 4、无子命令输出帮助
- 凭据：环境变量优先级、配置文件 600 权限强制
- 解析：秒级时区转换、地址精确匹配、glob 正向/反向过滤
- JMAP：query 分页及服务端 limit 钳制、8001 ids → 17 调用/2 HTTP 请求、requestTooLarge 拆半重试、Mailbox/query 兜底
- 下载：成功顺序、单文件失败隔离、完整 blobId 重名、NAME_MAX 超长截断
- 真实服务器：total/emails/detail/attachments/download 全链路、实际附件大小、重复下载重命名

运行：

```bash
# 离线单元测试（真实服务器测试自动跳过）
uv run python -m unittest discover -s tests -v

# 完整端到端测试（要求 bin/jmapx_creds.json 存在且权限 600）
JMAPX_INTEGRATION=1 uv run python -m unittest discover -s tests -v
```
