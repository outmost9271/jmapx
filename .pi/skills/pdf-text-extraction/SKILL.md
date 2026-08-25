---
name: pdf-text-extraction
description: 使用本地 pypdfium2/PDFium 检查并提取 PDF 原生文本层，支持页数检查、指定页、纯文本、JSON、JSONL、区域提取和加密 PDF。凡需读取票据、合同、邮件附件等 PDF 的可复制文字时优先使用；扫描件、乱码、缺字或关键字段复核则切换到 pdf-visual-reading。
license: Apache-2.0，见 ../../../tools/pdftext/LICENSE
compatibility: 需要 /pi/uv/tool/bin/uv；依赖由项目 tools/pdftext/pyproject.toml 和 uv.lock 管理。
---

# PDF 原生文本提取

项目工具位于 `../../../bin/pdftext`。在 `/pi/jmapx` 项目根目录工作时直接调用 `bin/pdftext`。

## 强制隐私规则

- PDF 和提取结果可能包含姓名、手机号、身份证号、税号、订单号、行程和金额；禁止上传到公网、第三方 OCR、公共代码仓库或其他远程文档服务。
- 工具本身只在本地调用 `pypdfium2/PDFium`，不发起网络请求。
- 优先输出到标准输出；确需落盘时，只写入权限受控的 `/tmp/pdftext-XXXXXX` 随机目录。
- 禁止对 PDF、提取结果、调试日志和密码文件执行 `git add`、`git commit` 或 `gh` 上传操作。
- 回复用户时只披露完成任务所需字段；除非用户明确要求，不复述完整身份证号、手机号、税号等敏感值。
- 用户未要求保留时，读取完成后删除临时提取文件。

## 标准决策流程

1. 先用 `inspect` 判断 PDF 是否存在原生文本层。
2. 有文本层时，按所需页码使用 `extract --format json`。
3. 检查替换字符、缺字和阅读顺序，不把“提取成功”等同于“内容完整”。
4. 金额、日期、车次、行程方向等关键字段必须视觉复核。
5. 无文本、乱码、缺字或顺序严重异常时，切换到 `pdf-visual-reading`。

## 1. 检查 PDF

```bash
bin/pdftext inspect "/path/to/input.pdf"
```

重点读取：

- `page_count`：总页数；
- `total_pdfium_char_count`：所选页面的 PDFium 字符总数；
- `textless_pages`：没有文本层的页码；
- `pages[].pdfium_char_count`：每页字符数量；
- `pages[].rotation` 和页面边界。

可只检查目标页：

```bash
bin/pdftext inspect "/path/to/input.pdf" --pages 1,3-5
```

若命令输出 `ok: false`，先处理空文件、文件损坏、密码或资源限制，禁止跳过错误后猜测内容。

如果 `textless_pages` 包含目标页，直接进入视觉读取流程，不要把空文本解释为票据本身为空。

## 2. 按页提取结构化文本

Agent 默认使用 JSON，确保文本与 PDF 页码保持明确映射：

```bash
bin/pdftext extract "/path/to/input.pdf" \
  --pages 1,3-5 \
  --format json
```

读取以下字段：

- `selected_pages`；
- `pages[].page`；
- `pages[].text`；
- `pages[].text_char_count`；
- `pages[].replacement_character_count`；
- `pages[].removed_control_character_count`；
- `textless_pages`。

不得根据输出数组位置猜测原 PDF 页码，必须使用 `pages[].page`。

页数较多时分批提取，避免终端输出或 `read` 工具截断：

```bash
bin/pdftext extract input.pdf --pages 1-10 --format json
bin/pdftext extract input.pdf --pages 11-20 --format json
```

## 3. 纯文本与 JSONL

只需快速阅读少量页面时：

```bash
bin/pdftext extract input.pdf --pages 1-2 --format text
```

默认页码标记形如：

```text
===== PDF 第 1 页 =====
```

不要删除页码标记后再混合多页内容，否则容易把不同页面的金额或日期配错。

需要逐行处理页面时：

```bash
bin/pdftext extract input.pdf --format jsonl
```

JSONL 首行是文档记录，后续每行是一个页面记录。

## 4. 临时文件流程

只有标准输出过长或后续必须使用 `read` 时才落盘：

```bash
OUT_DIR="$(mktemp -d /tmp/pdftext-XXXXXX)"
chmod 700 "$OUT_DIR"

bin/pdftext extract input.pdf \
  --pages 1-10 \
  --format json \
  --output "$OUT_DIR/pages-1-10.json"
```

输出文件权限为 `600`。使用 `read` 读取后立即安排清理。

默认禁止覆盖；只有确认旧结果可以替换时才使用 `--overwrite`。

## 5. 默认提取方法

默认方法是：

```bash
--method bounded
```

它使用 PDFium 区域文本接口，支持完整 Unicode。工具仅统一换行并删除空字符，不默认重排段落、合并空格或删除首尾空白。

如果默认结果可疑，可做兼容性对照：

```bash
bin/pdftext extract input.pdf \
  --pages 1 \
  --method range \
  --format json
```

`range` 使用 PDFium 字符范围接口，只支持 UCS-2。它不是默认方案；两种方法冲突时不得静默任选其一，应视觉核对。

## 6. 区域文本提取

先从 `inspect` 获取 `page_bbox_points`，再使用 PDF 坐标：

```bash
bin/pdftext extract input.pdf \
  --pages 1 \
  --method bounded \
  --bbox 0,396,612,792 \
  --format json
```

坐标顺序是 `left,bottom,right,top`，单位为点，原点位于左下角。

区域提取只适合减少页眉、页脚等干扰。若不确定坐标或页面发生旋转，优先提取整页；不要凭视觉图片的像素坐标直接填入 PDF 点坐标。

## 7. 判断结果是否可信

出现以下任一情况时，文本结果不能单独作为结论：

- `replacement_character_count` 大于零；
- `removed_control_character_count` 大于零；
- 肉眼可见字段在文本中缺失；
- 中文变成乱码、空白或无意义字符；
- 阅读顺序与版面明显不一致；
- 同一文字重复出现；
- 交互表单字段或注释中的可见值没有进入页面文本层；
- 金额小数点、负号、日期分隔符或车次字符可疑；
- 页面只有扫描图片；
- 隐藏文本层与可见内容不一致。

PDFium 不执行段落、表格或票据版面分析。文本出现的先后顺序不一定等于视觉阅读顺序，尤其不能仅凭相邻行关系推断“金额属于哪个行程”。

## 8. 报销关键字段复核

对票据至少执行：

1. 从 PDF 原生文本读取目标字段。
2. 保留其 PDF 页码和票据文件对应关系。
3. 使用视觉图再次读取金额、日期、车次、出发站和到达站。
4. 对照发票与行程单、酒店账单或其他配对票据。
5. 结果冲突时标记待人工确认，不静默选择文本或视觉结果。

不得把开票日期误作出行日期、发车日期、入住日期或退房日期；金额比较保留两位小数。

## 9. 切换到视觉读取

以下情况使用项目 Skill `pdf-visual-reading`：

- `textless_pages` 非空；
- 提取结果为空；
- 中文、标签或关键数字缺失；
- 需要核对版面关系；
- 需要确认文本层是否与实际显示一致。

典型命令：

```bash
IMG_DIR="$(mktemp -d /tmp/pdf-visual-XXXXXX)"
chmod 700 "$IMG_DIR"

bin/pdf2png render input.pdf \
  --pages 1 \
  --mode separate \
  --dpi 200 \
  --output-dir "$IMG_DIR"
```

然后使用 `read` 逐页读取图片。关键小字不清晰时按 `pdf-visual-reading` 的流程进行 300～400 DPI 局部复核，并按该 Skill 的规则清理 `IMG_DIR`。

## 10. 加密 PDF

默认读取 `PDF_PASSWORD`：

```bash
PDF_PASSWORD='...' bin/pdftext inspect encrypted.pdf
```

也可使用 `--password-env`，或权限为 `600`、`400` 的 `--password-file`。禁止在回复、日志、文件名或 Git 中暴露密码。

## 11. 清理

用户未要求保留时：

```bash
case "$OUT_DIR" in
  /tmp/pdftext-*) rm -rf -- "$OUT_DIR" ;;
  *) printf '拒绝清理非预期目录：%s\n' "$OUT_DIR" >&2 ;;
esac
```

必须确认变量非空且目录位于 `/tmp/pdftext-` 下，禁止用未经检查的变量执行递归删除。

## 完整参数

```bash
bin/pdftext inspect --help
bin/pdftext extract --help
```

详细说明见 `../../../tools/pdftext/README.md`。
