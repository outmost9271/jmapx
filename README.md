# jmapx

JMAP 邮件客户端命令行工具（仅 Python 3 标准库，零第三方依赖）。

```
usage: jmapx [-h] [--creds-file PATH] [--debug] [--pretty] COMMAND ...

子命令:
  total        获取邮件总数（含垃圾箱）
  emails       列出所有邮件（含垃圾箱），支持日期/发件人/收件人过滤
  detail       按 ids 查询邮件详细信息（含正文、附件列表）
  attachments  按 ids 查附件列表，glob 过滤分组，可一键下载命中的附件
  download     按 blobId 下载附件（多文件并发 + Range 分块）
```

完整使用文档见 `bin/USAGE.md`。

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
```

## 凭据配置

优先级：环境变量（`JMAP_SERVER`/`JMAP_USERNAME`/`JMAP_PASSWORD`）> 身份配置文件。

身份配置文件为 JSON，**权限必须为 600**，否则拒绝执行（模板见 `jmapx_creds.example.json`；真实凭据文件被 `.gitignore` 排除，不入库）：

```json
{
  "server": "https://mail.example.com",
  "username": "user@example.com",
  "password": "your-app-password"
}
```

## 常用示例

```bash
./bin/jmapx total                                            # 邮件总数
./bin/jmapx emails --start 2026-08-01 --pretty               # 8 月以来邮件
./bin/jmapx emails --from invoice@example.com --to me@x.com  # 收发件人精确匹配
./bin/jmapx detail --ids id1,id2 --body-max-bytes 5000       # 详情（含正文附件）
./bin/jmapx attachments --ids id1 --filter '*.pdf' --download-dir /tmp/att
./bin/jmapx download --blob-ids blob1,blob2 --dir /tmp/att   # 按 blobId 下载
./bin/jmapx --debug emails                                   # 调试（诊断走 stderr）
./bin/jmapx --creds-file /path/creds.json total              # 指定凭据文件
```
