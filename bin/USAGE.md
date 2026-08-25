# jmapx 使用文档

## 全局参数

```
usage: jmapx [-h] [--creds-file PATH] [--debug] [--pretty] COMMAND ...
```

| 参数 | 说明 |
|---|---|
| `--creds-file PATH` | 指定身份配置文件位置（默认：脚本同目录 `jmapx_creds.json`） |
| `--debug` | 调试模式：输出凭据来源、请求统计等诊断信息到 stderr（默认关闭） |
| `--pretty` | 输出格式化 JSON（4 空格缩进），默认紧凑格式 |

全局参数须放在子命令之前：`jmapx --pretty emails`，不能写成 `jmapx emails --pretty`。

## 凭据配置

优先级（高 → 低）：

1. **环境变量**：`JMAP_SERVER` / `JMAP_USERNAME` / `JMAP_PASSWORD`，三者必须齐全，任一缺失回退配置文件并提示
2. **身份配置文件**（JSON）：默认 `脚本同目录/jmapx_creds.json`（非隐藏文件），可用 `--creds-file` 覆盖

文件格式：

```json
{
  "server": "https://mail.example.com",
  "username": "user@example.com",
  "password": "your-app-password"
}
```

**权限强制**：配置文件权限必须恰好 `600`，否则输出告警并拒绝执行（退出码 1）。服务器地址可省略 `https://` 前缀（自动补全）。

## 子命令

### total —— 获取邮件总数（含垃圾箱）

```bash
jmapx total
# {"total": 26}
```

无参数。统计口径为账户唯一邮件数，天然包含垃圾箱/回收站。

### emails —— 列出所有邮件（含垃圾箱），支持过滤

```bash
jmapx emails [--from TEXT] [--to TEXT] [--start DT] [--end DT]
```

| 参数 | 语义 | 执行位置 |
|---|---|---|
| `--start DT` | 开始时间（含），精确到秒 | 服务端 `after` |
| `--end DT` | 结束时间（不含，before 语义） | 服务端 `before` |
| `--from TEXT` | 发件人精确匹配 | 客户端 |
| `--to TEXT` | 收件人精确匹配 | 客户端 |

日期格式：

- `YYYY-MM-DD`：start 视为当日 00:00:00（含）；end 视为次日 00:00:00（即含当天全天）
- `YYYY-MM-DDTHH:MM:SS` 或空格分隔：精确到秒
- 可带时区后缀 `Z` / `±HH:MM`；缺省按本地时区解释

发件人/收件人匹配规则：

- 邮箱地址：不区分大小写全等
- 显示名：精确相等
- 支持 `"Name <email>"` 格式输入（取尖括号内地址匹配）

输出：`{accountId, total, returned, notFound, mailboxes, emails, filter?}`。`total` 为服务端日期过滤后计数，`returned` 为客户端精确过滤后条数；`emails` 按 receivedAt 倒序。

### detail —— 按 ids 查询邮件详细信息

```bash
jmapx detail --ids id1,id2,... [--body-max-bytes N]
```

| 参数 | 说明 |
|---|---|
| `--ids LIST` | 逗号分隔的邮件 id（必填，自动去重） |
| `--body-max-bytes N` | 正文内容截断字节数，0 表示不截断（默认 10000） |

返回元数据（主题/收发件人/时间/大小/关键字）+ 正文（`textBody`/`htmlBody`/`bodyValues`）+ 附件列表（`attachments`：partId/name/type/size/blobId）。输出按输入 ids 顺序排列。

### attachments —— 查附件列表，可筛选并下载

```bash
jmapx attachments --ids id1,id2,... [--filter GLOB] [--download-dir PATH] [--concurrency N] [--chunks N]
```

| 参数 | 说明 |
|---|---|
| `--ids LIST` | 逗号分隔的邮件 id（必填） |
| `--filter GLOB` | 附件名 glob 过滤（不区分大小写）；`!` 前缀反向 |
| `--download-dir PATH` | 提供时下载所有**命中**（matched 组）附件到该目录（必须已存在，不自动创建） |
| `--concurrency N` | 下载并发文件数（默认 16） |
| `--chunks N` | 每文件 Range 分块数（默认 4） |

过滤语法：

- `*.pdf`：命中所有 PDF（不区分大小写，`*.PDF` 等价）
- `!*.pdf`：反向，未命中 `*.pdf` 的进入 matched 组
- 支持 `*`、`?`、`[...]`；中文模式直接使用（如 `*发票*`）

**无论是否命中，附件全部返回**，分 `matched` / `unmatched` 两组；下载只取 matched 组。

下载行为与 `download` 子命令一致：原始附件名落盘、重名自动重命名（`原名-N-完整blobId.ext`，超长自动截断）、失败项进入 `failed`（退出码 1）。

输出：`{accountId, requested, notFound, filter?, emails: [{id, subject, from, to, matched, unmatched}], download?}`。

### download —— 按 blobId 下载附件

```bash
jmapx download --blob-ids id1,id2,... --dir PATH [--names LIST] [--concurrency N] [--chunks N]
```

| 参数 | 说明 |
|---|---|
| `--blob-ids LIST` | 逗号分隔的 blobId（必填） |
| `--dir PATH` | 目标目录（必填，**不存在则报错，不自动创建**） |
| `--names LIST` | 可选，与 blob-ids 一一对应的文件名（缺省用 blobId 命名） |
| `--concurrency N` | 同时下载的文件数（默认 16） |
| `--chunks N` | 每文件 Range 分块数（默认 4） |

行为：

- 多文件并发下载；每个文件优先 Range 分块并行拉取，服务器不支持时自动回退单连接
- 文件名冲突时自动重命名 `原名-N-完整blobId.ext`，绝不覆盖；超过文件系统 `NAME_MAX`（通常 255 字节）时先截断 blobId、仍超限再截断原名
- 下载失败：该项进入 `failed` 数组，输出后退出码 1（成功项保留）

输出：`{dir, requested, downloaded: [{blobId, file, size, renamed}], failed}`。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 凭据/参数/网络/认证错误，或下载部分失败 |
| 2 | 命令行参数错误 |

## 典型链路

```bash
# 找邮件 → 查附件 → 下载
jmapx emails --start 2026-08-01 --from invoice@x.com --pretty > list.json
jmapx detail --ids <从 list.json 取 id> > detail.json
jmapx attachments --ids <id> --filter '*.pdf' --download-dir /tmp/att
```
