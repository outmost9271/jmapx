---
name: pdf-visual-reading
description: 将本地 PDF 安全地渲染为 PNG，并使用 Pi agent 的 read 工具逐页进行视觉读取。适用于 PDF 文本层缺失、乱码、提取顺序异常、票据字段需视觉核对，以及需要单页、长图、缩略图或切片输出的场景。
license: AGPL-3.0-or-later，见 ../../../tools/pdf2png/LICENSE
compatibility: 需要 /pi/uv/tool/bin/uv；依赖由项目 tools/pdf2png/pyproject.toml 管理。
---

# PDF 视觉读取

项目工具位于 `../../../bin/pdf2png`。在 `/pi/jmapx` 项目根目录工作时可直接调用 `bin/pdf2png`。

## 强制隐私规则

- PDF、PNG、JSON 清单可能包含姓名、手机号、身份证号、税号、订单号和行程信息；禁止上传到公网、第三方 OCR、公开仓库或其他远程服务。
- 默认把临时图片放在 `/tmp` 下权限受控的随机目录，不放进 Git 工作区。
- 禁止对生成图片和清单执行 `git add`、`git commit` 或 `gh` 上传操作。
- 只有用户明确要求保留时才把图片写入用户指定目录；否则完成读取后删除临时目录。
- 工具运行时只读取本地文件，不访问网络。

## 标准工作流

### 1. 查看 PDF

先运行 `inspect`，确认页数、页面方向和 200 DPI 下的预计像素：

```bash
bin/pdf2png inspect "/path/to/input.pdf"
```

若命令输出 `ok: false`，先处理密码、文件损坏或安全限制，禁止跳过错误后猜测内容。

### 2. 创建临时输出目录

```bash
OUT_DIR="$(mktemp -d /tmp/pdf-visual-XXXXXX)"
chmod 700 "$OUT_DIR"
```

记录该目录，任务结束时清理。

### 3. 选择渲染方式

视觉读取默认使用逐页模式：

```bash
bin/pdf2png render "/path/to/input.pdf" \
  --pages all \
  --mode separate \
  --dpi 200 \
  --output-dir "$OUT_DIR"
```

- 1～5 页：通常直接逐页渲染并依次调用 `read`。
- 页数较多：先生成 96 DPI 网格总览，确定目标页，再单独渲染目标页。
- 多页长图只在用户明确要求或页面很少时使用；视觉模型通常会缩小超长图片。

网格总览：

```bash
bin/pdf2png render "/path/to/input.pdf" \
  --mode grid \
  --dpi 96 \
  --grid-columns 3 \
  --output-dir "$OUT_DIR/grid"
```

指定页面：

```bash
bin/pdf2png render "/path/to/input.pdf" \
  --pages 1,3-5 \
  --mode separate \
  --dpi 200 \
  --output-dir "$OUT_DIR/pages"
```

### 4. 使用 read 逐图读取

读取 `render` 输出 JSON 中 `outputs[].file` 指向的图片。以 JSON 清单中的 `pages` 和 `page_regions` 为准建立图片与 PDF 页码映射，不从文件列表顺序猜页码。

关键数字必须逐字符读取，尤其是：

- 日期与时间；
- 金额及小数点；
- 发票号码、订单号和车次；
- 出发站、到达站、酒店名称；
- 正负金额和税额。

不得因为视觉模型置信感很强就省略复核。

### 5. 不确定时局部重渲染

如果整页小字不清晰，优先提高目标区域分辨率，不要无限提高整页 DPI。

渲染下半页：

```bash
bin/pdf2png render "/path/to/input.pdf" \
  --pages 1 \
  --mode separate \
  --dpi 350 \
  --crop-percent 0,50,100,100 \
  --output-dir "$OUT_DIR/crop"
```

页面密集或很长时切片：

```bash
bin/pdf2png render "/path/to/input.pdf" \
  --pages 1 \
  --mode tiles \
  --dpi 300 \
  --tile-width 1600 \
  --tile-height 1600 \
  --tile-overlap 100 \
  --output-dir "$OUT_DIR/tiles"
```

相邻切片存在重叠，读取时利用重叠文字确认没有漏行或重复计数。

### 6. 怀疑缺字时切换后端

默认 `auto` 优先使用 PyMuPDF。若图片中字段标签、中文或特殊字体异常消失，分别尝试：

```bash
bin/pdf2png render "/path/to/input.pdf" \
  --pages 1 --backend pymupdf --mode separate --dpi 200 \
  --prefix pymupdf --output-dir "$OUT_DIR/backends"

bin/pdf2png render "/path/to/input.pdf" \
  --pages 1 --backend pdfium --mode separate --dpi 200 \
  --prefix pdfium --output-dir "$OUT_DIR/backends"
```

比较两张图片。项目样例中的铁路电子客票已确认：PDFium 会漏掉中文站名，而 PyMuPDF 能完整渲染。渲染结果缺失的内容无法由视觉模型恢复。

### 7. 关键字段复核

对报销票据至少执行以下复核：

1. 在整页图中读取一次。
2. 在局部高分辨率图中再次读取日期、时间和金额。
3. 对照同订单其他票据或 PDF 原生文本；冲突时标记待人工确认，不静默选择任一结果。
4. 金额比较保留两位小数，不把开票日期误当作出行日期、发车日期或入住日期。

### 8. 清理

用户未要求保留时：

```bash
rm -rf -- "$OUT_DIR"
```

删除前确认变量非空且路径位于 `/tmp/pdf-visual-` 下，禁止使用未经检查的空变量执行递归删除。

## 其他模式

单张纵向长图：

```bash
bin/pdf2png render input.pdf --pages 1-4 --mode vertical --output-dir "$OUT_DIR/long"
```

自动分段长图：

```bash
bin/pdf2png render input.pdf \
  --mode vertical-chunks --chunk-pages 4 --max-long-height 16000 \
  --output-dir "$OUT_DIR/chunks"
```

默认禁止覆盖。只有确认旧输出可以删除时才使用 `--overwrite`。

## 加密 PDF

默认可从 `PDF_PASSWORD` 读取密码：

```bash
PDF_PASSWORD='...' bin/pdf2png inspect encrypted.pdf
```

也可使用 `--password-env` 或权限为 `600` 的 `--password-file`。禁止在回复、日志、文件名或 Git 中暴露密码。

## 完整参数

```bash
bin/pdf2png inspect --help
bin/pdf2png render --help
```

详细说明见 `../../../tools/pdf2png/README.md`。
