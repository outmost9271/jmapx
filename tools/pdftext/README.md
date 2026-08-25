# PDF 本地文本提取工具

该组件使用 `pypdfium2/PDFium` 读取 PDF 原生文本层，适合在调用视觉模型前快速获取页数、日期、金额、订单号、站名等可复制文字。

工具只在本机读取 PDF。提取代码不会调用网络、云端 OCR 或第三方文档服务；首次同步独立虚拟环境时，`uv` 可能从配置的软件包仓库下载锁定依赖，但不会发送输入 PDF 或提取结果。

## 目录

- `../../bin/pdftext`：项目统一命令入口。
- `pdf_to_text.py`：主程序。
- `pyproject.toml`、`uv.lock`：独立的 `uv` 项目和锁定依赖。
- `tests/`：不含真实票据的合成 PDF 测试。

该组件独立于 `jmapx` 邮件客户端，不会改变后者仅使用 Python 标准库的设计。

## 快速使用

在 `/pi/jmapx` 根目录运行：

```bash
# 查看页数、页面尺寸、方向和每页文本字符数量
bin/pdftext inspect "/path/to/input.pdf"

# 提取全部页面为带页码标记的纯文本
bin/pdftext extract "/path/to/input.pdf"

# 提取指定页面为结构化 JSON
bin/pdftext extract "/path/to/input.pdf" \
  --pages 1,3-5 \
  --format json

# 原子写入权限为 600 的本地文件
bin/pdftext extract "/path/to/input.pdf" \
  --format json \
  --output /tmp/pdftext-output/result.json
```

不指定 `--output` 时，结果写到标准输出，不创建文本文件。

## 命令

### `inspect`

```bash
bin/pdftext inspect INPUT.pdf [选项]
```

输出 JSON，主要字段包括：

- `page_count`：PDF 总页数。
- `selected_pages`：本次检查的页码。
- `total_pdfium_char_count`：PDFium 文本字符总数。
- `textless_pages`：没有原生文本层的页码。
- `pages[].width_points`、`height_points`、`rotation`：页面几何信息。
- `pages[].pdfium_char_count`：该页的 PDFium 内部字符数量。

示例：

```bash
bin/pdftext inspect input.pdf --pages odd
bin/pdftext inspect input.pdf --pages 1,4-7
```

### `extract`

```bash
bin/pdftext extract INPUT.pdf [选项]
```

页码表达式支持：

- `all`
- `odd`
- `even`
- `1`
- `1,3-5`

## 输出格式

### 纯文本

默认格式为 `text`，每页前加入清晰页码标记：

```text
===== PDF 第 1 页 =====
页面文本
```

可关闭页码标记；不同页面会改用换页符分隔：

```bash
bin/pdftext extract input.pdf --format text --no-page-markers
```

### JSON

```bash
bin/pdftext extract input.pdf --format json
```

JSON 保留文档、页码与文本之间的映射，每页包含：

- 页面尺寸、方向和页面边界；
- `pdfium_char_count`；
- 实际输出的 `text_char_count`；
- Unicode 替换字符数量及被移除的不安全控制字符数量；
- 提取方法和区域；
- `text` 原文。

需要紧凑输出时使用 `--compact`。

### JSONL

```bash
bin/pdftext extract input.pdf --format jsonl
```

首行为 `type: document` 的文档记录，后续每行对应一个 `type: page` 的页面记录，适合逐行处理。

## 提取方法

### `bounded`，默认

```bash
bin/pdftext extract input.pdf --method bounded
```

使用 PDFium 的区域文本接口，支持完整 Unicode，也是推荐方式。

可按 PDF 坐标提取局部区域：

```bash
bin/pdftext extract input.pdf \
  --pages 1 \
  --method bounded \
  --bbox 0,396,612,792 \
  --format json
```

`--bbox` 顺序为 `left,bottom,right,top`，单位是 PDF 点，坐标原点位于页面左下角。页面尺寸和有效边界可先通过 `inspect` 查看。

### `range`

```bash
bin/pdftext extract input.pdf --method range
```

使用 PDFium 字符范围接口，适合在默认结果异常时进行兼容性对照。该底层接口只支持 UCS-2，因此默认仍应优先使用 `bounded`。`range` 不支持 `--bbox`。

## 文本处理

默认只执行两项安全规范化：

1. 将 `CRLF` 和 `CR` 换行统一为 `LF`。
2. 保留换行和制表符，删除空字符、终端转义符等不安全的 C0/C1 控制字符。

JSON 中通过 `removed_control_character_count` 记录移除数量。工具不会默认合并空格、重排段落或删除首尾空白，避免悄然改变票据字段。可按需启用：

```bash
--trim                  # 去除每页首尾空白
--collapse-blank-lines  # 压缩连续空白行
```

Unicode 解码错误默认为 `replace`，会使用 `�` 显式标出，且 JSON 中记录 `replacement_character_count`。也可使用：

```bash
--decode-errors strict
--decode-errors ignore
```

关键字段读取不建议使用 `ignore`，因为它会静默丢弃无法解码的字符。

## 扫描件和异常文本层

PDFium 只读取 PDF 内已有文本层，不执行 OCR。以下情况应改用项目的视觉读取工具：

- `textless_pages` 非空或字符数量为零；
- 输出乱码、阅读顺序严重异常；
- 肉眼可见字段没有出现在文本中；
- 字体编码错误导致金额、日期或中文缺失；
- 可见内容来自交互表单字段或注释，而没有进入页面文本层；
- PDF 本身只有扫描图片。

视觉读取入口：

```bash
bin/pdf2png render input.pdf \
  --pages 1 \
  --mode separate \
  --dpi 200 \
  --output-dir /tmp/pdf-pages
```

原生文本提取成功也不代表关键金额和日期绝对正确。报销流程中仍应使用局部高分辨率图片对金额、日期、车次和行程方向进行二次核对。

## 加密 PDF

默认读取 `PDF_PASSWORD`：

```bash
PDF_PASSWORD='...' bin/pdftext inspect encrypted.pdf
```

也可指定其他环境变量：

```bash
bin/pdftext inspect encrypted.pdf --password-env MY_PDF_PASSWORD
```

或从权限为 `600`、`400` 的文件首行读取：

```bash
chmod 600 /tmp/pdf-password
bin/pdftext inspect encrypted.pdf --password-file /tmp/pdf-password
```

密码不会写入输出、文件名或错误消息。不要把密码直接放入会被记录的命令行参数。

## 输出与资源保护

默认限制：

- 输入文件：200 MiB。
- PDF 总页数：500 页。
- 单页 PDFium 字符：1,000,000 个。
- 所选页面累计字符：5,000,000 个。
- 输出内容：50 MiB。

对应参数：

```text
--max-input-mb
--max-pages
--max-page-chars
--max-total-chars
--max-output-mb
```

输出规则：

- 默认不覆盖已有文件，确需覆盖时使用 `--overwrite`。
- 拒绝把结果写回输入 PDF。
- 拒绝写入符号链接。
- 新建输出目录权限为 `700`，输出文件权限为 `600`。
- 文件通过同目录临时文件原子替换。

任一所选页面没有文本时需要直接失败，可使用：

```bash
bin/pdftext extract input.pdf --fail-on-empty
```

## 开发与测试

所有 Python 操作通过受控 `uv` 执行：

```bash
cd /pi/jmapx/tools/pdftext
export PATH=/pi/uv/tool/bin:$PATH
uv sync --locked
uv run python -m unittest discover -s tests -v
```

代码检查：

```bash
cd /pi/jmapx
export PATH=/pi/uv/tool/bin:$PATH
uvx ruff check tools/pdftext/pdf_to_text.py tools/pdftext/tests/test_pdf_to_text.py
uvx ruff format --check tools/pdftext/pdf_to_text.py tools/pdftext/tests/test_pdf_to_text.py
```

## 许可

该组件代码采用 `Apache-2.0`，见 `LICENSE`。

`pypdfium2` 采用 `Apache-2.0 OR BSD-3-Clause`，其安装包包含 PDFium 及所带第三方组件的完整许可文件。详情见 `THIRD_PARTY_NOTICES.md` 和安装环境中的相应许可目录。
