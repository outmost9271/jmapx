# 第三方组件说明

## pypdfium2

- 项目：https://github.com/pypdfium2-team/pypdfium2
- 许可：`Apache-2.0 OR BSD-3-Clause`
- 用途：提供 PDFium 的 Python 绑定及预编译 PDFium 运行库。

本项目不复制或修改 `pypdfium2` 源码，而是通过 `uv.lock` 从软件包仓库安装。安装后的完整许可文件位于虚拟环境中：

```text
.venv/lib/python*/site-packages/pypdfium2-*.dist-info/licenses/
.venv/lib/python*/site-packages/pypdfium2_raw/
```

`pypdfium2` wheel 还包含 PDFium、ICU、FreeType、libjpeg、libpng、OpenJPEG、zlib 等运行时组件；其逐项许可文本随 wheel 一同安装，请以对应版本安装包中的许可文件为准。
