---
name: jmapx-usage
description: 使用 jmapx 命令行工具连接 JMAP 邮件服务器进行邮件操作：查询邮件总数、按时间/收发件人过滤列出邮件、按 ids 获取邮件详情（含正文与附件清单）、查看并按名称筛选附件列表、按 blobId 并发下载附件到本地目录。适用于任何需要读取 JMAP 邮箱、导出附件、按条件检索邮件的场景。使用前需提供服务器地址与凭据（环境变量或 600 权限的本地配置文件）。
---

# jmapx 使用指南

`jmapx` 是 JMAP 邮件客户端命令行工具，位于项目 `bin/jmapx`，仅依赖 Python 3 标准库。所有命令输出 JSON 到标准输出，诊断信息输出到标准错误。

## 凭据配置

凭据来源优先级：

1. 环境变量（三者必须齐全）：`JMAP_SERVER`、`JMAP_USERNAME`、`JMAP_PASSWORD`
2. 身份配置文件（JSON，默认读取脚本同目录 `jmapx_creds.json`，可用 `--creds-file` 指定其他位置）

配置文件格式：

```json
{
  "server": "https://mail.duy7d0.example",
  "username": "ouyt3615@k6i3ir.example",
  "password": "app-secret"
}
```

配置文件权限**必须为 600**，否则拒绝执行。

## 全局参数

全局参数**必须**放在子命令之前：

```bash
jmapx [--creds-file PATH] [--debug] [--pretty] COMMAND ...
```

> ⚠️ 注意：全局参数放在子命令之后会报错（如 `bin/jmapx emails ... --pretty` 会提示 `unrecognized arguments: --pretty` 并以退出码 2 失败），务必写作 `bin/jmapx --pretty emails ...`。

| 参数 | 说明 |
|---|---|
| `--creds-file PATH` | 指定身份配置文件位置 |
| `--debug` | 输出诊断信息到标准错误（默认关闭） |
| `--pretty` | 格式化输出 JSON（4 空格缩进），默认紧凑格式 |

## 子命令

### total —— 邮件总数（含垃圾箱）

```bash
bin/jmapx total
```

示例输出：

```json
{"total": 173}
```

### emails —— 列出邮件（含垃圾箱），支持过滤

```bash
bin/jmapx emails [--from TEXT] [--to TEXT] [--start DT] [--end DT]
```

| 参数 | 说明 |
|---|---|
| `--start DT` | 开始时间（含）。`YYYY-MM-DD` 或 `YYYY-MM-DDTHH:MM:SS`（精确到秒），可带时区 `Z`/`±HH:MM`，缺省按本地时区 |
| `--end DT` | 结束时间（不含）。仅给日期时视为含当天全天 |
| `--from TEXT` | 发件人精确匹配：邮箱地址（不区分大小写）或显示名，支持 `"Name <email>"` 格式 |
| `--to TEXT` | 收件人精确匹配，规则同 `--from` |

示例：

```bash
bin/jmapx --pretty emails --start 2026-05-11T02:00:00Z --end 2026-05-11T04:00:00Z \
  --from igfai50q@partner.example
```

示例输出（节选）：

```json
{
  "accountId": "k7m2",
  "total": 3,
  "returned": 3,
  "notFound": [],
  "mailboxes": [
    {"id": "m1", "name": "Inbox", "role": "inbox", "totalEmails": 121, "unreadEmails": 4},
    {"id": "m2", "name": "Junk Mail", "role": "junk", "totalEmails": 11, "unreadEmails": 0}
  ],
  "emails": [
    {
      "id": "e3y3o41",
      "subject": "月度结算单",
      "from": [{"name": null, "email": "igfai50q@partner.example"}],
      "to": [{"name": null, "email": "ouyt3615@k6i3ir.example"}],
      "receivedAt": "2026-05-11T03:12:31Z",
      "size": 80215,
      "hasAttachment": true
    }
  ],
  "filter": {"start": "2026-05-11T02:00:00Z", "end": "2026-05-11T04:00:00Z", "from": "igfai50q@partner.example"}
}
```

邮件对象实际还含 `sentAt`（发送时间）、`preview`（正文预览）、`mailboxIds`（所在文件夹映射，键为文件夹 id）、`keywords`（邮件标记，如 `$seen`/`$junk`）等字段。

### detail —— 按 ids 查询邮件详情

```bash
bin/jmapx detail --ids id1,id2,... [--body-max-bytes N]
```

| 参数 | 说明 |
|---|---|
| `--ids LIST` | 逗号分隔的邮件 id（必填，自动去重） |
| `--body-max-bytes N` | 正文内容截断字节数，`0` 不截断（默认 10000） |

返回邮件元数据、正文与附件清单。顶层结构：`accountId`/`requested`（请求数）/`returned`（返回数）/`notFound`（未找到的 id）/`emails`（邮件数组）。`emails` 元素除与 emails 命令相同的字段外，另含三个正文/附件相关字段：

| 字段 | 说明 |
|---|---|
| `textBody`/`htmlBody` | 正文 part 引用**列表**（非正文文本），每项含 `partId`/`blobId`/`type`/`charset` 等 |
| `bodyValues` | 正文内容字典，**键为 partId**，正文文本在其 `value` 字段；`isTruncated` 为 true 表示被 `--body-max-bytes` 截断 |
| `attachments` | 附件列表，每项含 `partId`/`name`/`type`/`size`/`blobId` 等，`blobId` 供下载使用 |

> ⚠️ 正文须从 `bodyValues[partId]["value"]` 读取，`textBody`/`htmlBody` 仅为 part 引用列表，不是正文文本。

示例输出（节选）：

```json
{
  "accountId": "k7m2",
  "requested": 2,
  "returned": 2,
  "notFound": [],
  "emails": [
    {
      "id": "e3y3o41",
      "subject": "月度结算单",
      "textBody": [{"partId": "3", "blobId": "chjzsbdk...1vs7bi", "size": 5, "type": "text/plain", "charset": "UTF-8"}],
      "bodyValues": {"3": {"isEncodingProblem": false, "isTruncated": false, "value": "正文文本"}},
      "attachments": [
        {"partId": "5", "blobId": "2qutgply...7basp1p", "size": 140534, "name": "invoice-igv5fn.pdf", "type": "application/pdf", "disposition": "attachment"}
      ]
    }
  ]
}
```

```bash
bin/jmapx detail --ids e3y3o41,pbc43z2 --body-max-bytes 2000
```

### attachments —— 查看附件列表，可筛选并下载

```bash
bin/jmapx attachments --ids id1,id2,... [--filter GLOB] [--download-dir PATH]
```

| 参数 | 说明 |
|---|---|
| `--ids LIST` | 逗号分隔的邮件 id（必填） |
| `--filter GLOB` | 附件名 glob 过滤（不区分大小写，支持 `*`/`?`/`[...]`）；`!` 前缀反向 |
| `--download-dir PATH` | 提供时下载所有命中（matched 组）的附件到该目录（必须已存在，不自动创建） |

无论是否命中，附件全部返回，分 `matched` / `unmatched` 两组：

```bash
bin/jmapx --pretty attachments --ids e3y3o41 --filter '*.pdf'
```

示例输出（节选）：

```json
{
  "accountId": "k7m2",
  "requested": 1,
  "notFound": [],
  "filter": "*.pdf",
  "emails": [
    {
      "id": "e3y3o41",
      "subject": "月度结算单",
      "from": [{"name": null, "email": "igfai50q@partner.example"}],
      "to": [{"name": null, "email": "ouyt3615@k6i3ir.example"}],
      "matched": [
        {"partId": "p7", "name": "invoice-igv5fn.pdf", "type": "application/pdf", "size": 140534, "blobId": "2qutgplye5grl2tf2grz4gd96694cf0zkd53ldp7ip62u4m8s71bc7basp1p"}
      ],
      "unmatched": [
        {"partId": "p8", "name": "receipt-2qgz8v.xml", "type": "application/octet-stream", "size": 2498, "blobId": "5wuz0piuupbq4tk5f98u4jmd0b79abhgi78xz3m7wcaiwq96e1nv838b9zv4"}
      ]
    }
  ]
}
```

下载命中的附件（按原始附件名落盘，重名自动重命名，多并发下载）：

```bash
mkdir -p /tmp/att-58xm
bin/jmapx attachments --ids e3y3o41 --filter '*.pdf' --download-dir /tmp/att-58xm
```

示例输出（节选）：

```json
{
  "download": {
    "dir": "/tmp/att-58xm",
    "requested": 1,
    "downloaded": [
      {"blobId": "2qutgplye5grl2tf2grz4gd96694cf0zkd53ldp7ip62u4m8s71bc7basp1p", "file": "/tmp/att-58xm/invoice-igv5fn.pdf", "size": 140534, "renamed": false}
    ],
    "failed": []
  }
}
```

### download —— 按 blobId 下载附件

```bash
bin/jmapx download --blob-ids id1,id2,... --dir PATH [--names LIST]
```

| 参数 | 说明 |
|---|---|
| `--blob-ids LIST` | 逗号分隔的 blobId（必填） |
| `--dir PATH` | 目标目录（必填，**不存在则报错，不自动创建**） |
| `--names LIST` | 可选，与 blob-ids 一一对应的文件名（缺省用 blobId 命名） |

```bash
mkdir -p /tmp/dl-yi39
bin/jmapx --pretty download \
  --blob-ids 2qutgplye5grl2tf2grz4gd96694cf0zkd53ldp7ip62u4m8s71bc7basp1p,5wuz0piuupbq4tk5f98u4jmd0b79abhgi78xz3m7wcaiwq96e1nv838b9zv4 \
  --names invoice-igv5fn.pdf,receipt-2qgz8v.xml \
  --dir /tmp/dl-yi39
```

文件重名时自动重命名（`原名-N-完整blobId.扩展名`），绝不覆盖；单个文件失败不影响其他文件，失败项列入 `failed`。

输出 JSON 结构：顶层 `dir`（目标目录）/`requested`（请求数）/`downloaded`（成功数组，每项 `blobId`/`file`/`size`/`renamed`）/`failed`（失败数组，每项 `blobId`/`error`），与 attachments 命令带 `--download-dir` 时的 `download` 对象结构一致。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 凭据/参数/网络/认证错误，或下载部分失败 |
| 2 | 命令行参数错误 |

## 典型链路

```bash
# 1. 查看账户邮件总数
bin/jmapx total

# 2. 按时间与发件人检索邮件
bin/jmapx --pretty emails --start 2026-05-11T02:00:00Z --end 2026-05-11T04:00:00Z --from igfai50q@partner.example

# 3. 查看目标邮件的附件清单，筛选 PDF
bin/jmapx attachments --ids e3y3o41 --filter '*.pdf'

# 4. 一键下载所有命中的附件
mkdir -p /tmp/att-58xm
bin/jmapx attachments --ids e3y3o41 --filter '*.pdf' --download-dir /tmp/att-58xm

# 5. 需要下载单个附件时，直接用 detail 输出的 blobId
bin/jmapx download --blob-ids 2qutgplye5grl2tf2grz4gd96694cf0zkd53ldp7ip62u4m8s71bc7basp1p \
  --names invoice-igv5fn.pdf --dir /tmp/dl-yi39
```
