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

## PDF 文本提取工具

项目提供基于 `pypdfium2/PDFium` 的本地文本提取工具。它使用独立的 `uv` 子项目，不改变 `jmapx` 邮件客户端仅依赖 Python 标准库的性质，也不会把 PDF 或提取结果发送到远程服务。

```bash
# 检查页数、页面方向和每页文本字符数量
./bin/pdftext inspect /path/to/input.pdf

# 提取全部页面的文本
./bin/pdftext extract /path/to/input.pdf

# 提取指定页面为结构化 JSON
./bin/pdftext extract /path/to/input.pdf \
  --pages 1,3-5 --format json
```

支持纯文本、JSON、JSONL、指定页、区域提取、两种 PDFium 文本接口、加密 PDF、原子输出和字符资源限制。完整文档见 `tools/pdftext/README.md`，对应 Skill 位于 `.pi/skills/pdf-text-extraction/SKILL.md`。

## PDF 转 PNG 工具

项目另带一个本地 PDF 渲染工具，使用独立的 `uv` 子项目，不改变 `jmapx` 邮件客户端仅依赖 Python 标准库的性质。

```bash
# 查看页数、页面尺寸和预计像素
./bin/pdf2png inspect /path/to/input.pdf

# 将全部页面分别生成 PNG（视觉模型读取时推荐）
./bin/pdf2png render /path/to/input.pdf \
  --pages all --mode separate --output-dir /tmp/pdf-pages

# 将前四页拼成长图
./bin/pdf2png render /path/to/input.pdf \
  --pages 1-4 --mode vertical --output-dir /tmp/pdf-long
```

支持 `separate`、`vertical`、`vertical-chunks`、`grid` 和 `tiles` 五种模式，以及 DPI、百分比裁剪、旋转、灰度、后端切换、加密 PDF 和像素安全限制。默认使用 `PyMuPDF`，打开失败时尝试 `pypdfium2/PDFium`。

完整文档见 `tools/pdf2png/README.md`。该工具组件采用 `AGPL-3.0-or-later`，运行时不会上传 PDF 或生成的图片。

## 许可

本仓库包含多个组件，采用以下许可结构：

- `bin/jmapx`、`bin/pdf2png`、`tools/pdf2png/`、`.pi/skills/` 及根目录文档：`AGPL-3.0-or-later`，见根目录 `LICENSE`。
- `bin/pdftext`、`tools/pdftext/`：`Apache-2.0`，见 `tools/pdftext/LICENSE`。
- `tools/pdftext` 依赖的 `pypdfium2` 采用 `Apache-2.0 OR BSD-3-Clause`，其中打包的 PDFium 及第三方运行库许可说明见 `tools/pdftext/THIRD_PARTY_NOTICES.md`。

各 PDF 工具只在本地读取输入文件，不把 PDF、渲染图片或提取文本上传到任何远程服务。
