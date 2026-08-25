# jmapx

JMAP 邮件客户端命令行工具（零第三方依赖，仅 Python 3 标准库）。

## 子命令

| 子命令 | 功能 |
|---|---|
| `total` | 获取邮件总数（含垃圾箱） |
| `emails` | 列出所有邮件（含垃圾箱），支持日期/发件人/收件人过滤 |
| `detail` | 按 ids 查询邮件详细信息（含正文、附件列表） |
| `attachments` | 按 ids 查附件列表，glob 过滤分组，可一键下载命中的附件 |
| `download` | 按 blobId 下载附件（多文件并发 + Range 分块） |

## 快速开始

```bash
# 环境变量方式（优先级最高，三者必须齐全）
export JMAP_SERVER=mail.example.com
export JMAP_USERNAME=user@example.com
export JMAP_PASSWORD=app-xxx
./bin/jmapx total

# 身份配置文件方式（默认读脚本同目录 jmapx_creds.json，权限必须为 600）
./bin/jmapx total
./bin/jmapx emails --start 2026-08-01 --from someone@example.com
./bin/jmapx --pretty emails --to me@example.com

# 指定配置文件位置
./bin/jmapx --creds-file /path/to/creds.json total
```

## 凭据配置

优先级：环境变量（`JMAP_SERVER`/`JMAP_USERNAME`/`JMAP_PASSWORD`）> 身份配置文件。

身份配置文件为 JSON，**权限必须为 600**，否则拒绝执行：

```json
{
  "server": "https://mail.example.com",
  "username": "user@example.com",
  "password": "your-app-password"
}
```

模板见 `jmapx_creds.example.json`；实际凭据文件被 `.gitignore` 排除，不会入库。

## 常用示例

```bash
# 邮件总数
./bin/jmapx total

# 列出 8 月以来某发件人的邮件（含垃圾箱），JSON 输出
./bin/jmapx emails --start 2026-08-01 --from invoice@example.com --pretty

# 查两封邮件的详情（含正文与附件）
./bin/jmapx detail --ids id1,id2 --body-max-bytes 5000

# 查附件列表，只筛 PDF，并把命中的下载到目录
./bin/jmapx attachments --ids id1 --filter '*.pdf' --download-dir /tmp/att

# 按 blobId 下载附件
./bin/jmapx download --blob-ids blob1,blob2 --dir /tmp/att

# 调试模式（诊断信息走 stderr，stdout 保持纯净 JSON）
./bin/jmapx --debug emails
```

## 设计要点

- 批量拉取：单请求多调用（≤`maxCallsInRequest`），上限从 Session 能力声明动态读取
- 过滤：日期在服务端精确过滤（秒级），发件人/收件人在客户端精确匹配（JMAP 的 from/to 为包含式搜索）
- 下载：文件级并发（默认 16）+ 每文件 Range 分块（服务器不支持时自动回退）；重名自动重命名 `原名-N-完整blobId.ext`（超长截断兜底）
- 输出：stdout 输出 JSON（`--pretty` 格式化），诊断信息走 stderr
