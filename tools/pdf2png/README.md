# pdf2png

将本地 PDF 页面渲染为适合视觉模型读取的 PNG。运行时不访问网络，也不上传 PDF 或图片。

## 运行

从仓库根目录调用包装命令：

```bash
bin/pdf2png inspect input.pdf
bin/pdf2png render input.pdf --pages 1,3-5 --mode separate --output-dir /tmp/pdf-pages
```

包装命令固定通过 `/pi/uv/tool/bin/uv` 使用本目录独立的 Python 环境，不向系统 Python 安装依赖。

## 查看 PDF

```bash
bin/pdf2png inspect input.pdf
bin/pdf2png inspect input.pdf --dpi 300 --backend pymupdf
```

输出 JSON，包括：

- 文件大小与 SHA-256；
- 页数、每页尺寸及旋转角度；
- 指定 DPI 下每页预计像素和总像素；
- 实际使用的渲染后端；
- 加密和修复状态（后端可提供时）。

## 页面选择

`--pages` 使用从 1 开始的页码：

```text
all       全部页面
odd       奇数页
even      偶数页
3         第 3 页
1,3-5     第 1、3、4、5 页
```

## 输出模式

### 独立页面

```bash
bin/pdf2png render input.pdf \
  --pages all \
  --mode separate \
  --output-dir /tmp/pdf-pages
```

生成 `input-p001.png`、`input-p002.png` 等文件。这是视觉模型读取 PDF 时的默认推荐模式。

### 单张纵向长图

```bash
bin/pdf2png render input.pdf \
  --pages 1-4 \
  --mode vertical \
  --output-dir /tmp/pdf-long
```

页面保持宽高比并居中排列，默认在每页上方增加 `Page N` 标签。长图超过 `--max-long-height` 时拒绝生成。

### 自动分段长图

```bash
bin/pdf2png render input.pdf \
  --pages all \
  --mode vertical-chunks \
  --chunk-pages 4 \
  --max-long-height 16000 \
  --output-dir /tmp/pdf-chunks
```

### 缩略图网格

```bash
bin/pdf2png render input.pdf \
  --mode grid \
  --dpi 96 \
  --grid-columns 3 \
  --output-dir /tmp/pdf-grid
```

适合先判断多页 PDF 中哪些页面值得单独高分辨率渲染。

### 页面切片

```bash
bin/pdf2png render input.pdf \
  --pages 2 \
  --mode tiles \
  --dpi 300 \
  --tile-width 1600 \
  --tile-height 1600 \
  --tile-overlap 100 \
  --output-dir /tmp/pdf-tiles
```

适合小字密集、超长或超大页面。

## 裁剪与增强读取

按渲染后图片的百分比坐标裁剪：

```bash
bin/pdf2png render input.pdf \
  --pages 1 \
  --dpi 350 \
  --crop-percent 0,50,100,100 \
  --mode separate \
  --output-dir /tmp/pdf-bottom-half
```

坐标依次为 `left,top,right,bottom`，范围为 0 到 100。还支持：

- `--rotate 90`：额外顺时针旋转；
- `--grayscale`：灰度输出；
- `--trim-whitespace`：裁掉与背景色一致的外边缘；
- `--background '#FFFFFF'`：指定背景色；
- `--no-annotations`：隐藏 PDF 注释。

## 渲染后端

```text
auto      优先 PyMuPDF，打开失败时尝试 PDFium
pymupdf   使用 PyMuPDF
pdfium    使用 pypdfium2/PDFium
```

项目样例中的铁路电子客票存在异常字体定义：PDFium 渲染时会漏掉中文字段，而 PyMuPDF 可以完整显示。因此默认优先使用 PyMuPDF。若结果看起来缺字，应显式切换后端并比较，而不是直接相信单次渲染。

## 加密 PDF

默认读取 `PDF_PASSWORD`。也可以指定其他环境变量或权限受控的密码文件：

```bash
PDF_PASSWORD='secret' bin/pdf2png inspect encrypted.pdf
bin/pdf2png inspect encrypted.pdf --password-env MY_PDF_PASSWORD
bin/pdf2png inspect encrypted.pdf --password-file /run/secrets/pdf-password
```

不建议把密码直接写进命令行参数。

## 输出清单与权限

`render` 默认在输出目录生成 `*-manifest.json`，记录源文件哈希、页码、DPI、输出图片尺寸以及页面在拼图中的坐标。使用 `--no-manifest` 可关闭。

新建输出目录权限设为 `700`，PNG 和 JSON 文件权限设为 `600`。默认不覆盖已有文件；如确需覆盖，显式使用 `--overwrite`。

## 安全限制

默认限制：

- 输入不超过 200 MiB；
- PDF 不超过 500 页；
- 单页不超过 40 MP；
- 单个拼图不超过 80 MP；
- 一次任务总渲染量不超过 500 MP；
- 长图高度不超过 16000 像素；
- 输出不超过 1000 个文件。

对应参数均可显式调整。不要对来源不明的大型 PDF 盲目取消限制。

## 测试

```bash
cd tools/pdf2png
/pi/uv/tool/bin/uv run python -m unittest discover -s tests -v
```

测试只生成无敏感信息的临时 PDF 和图片。

## 许可

本工具采用 `AGPL-3.0-or-later`。PyMuPDF 同时提供 AGPL 与商业许可；本工具选择在 AGPL 条款下使用。
