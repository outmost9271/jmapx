# 高铁报销凭证解析工具（railfare）

解析中国铁路电子客票（报销凭证）PDF，并自动检测版式变化。解析代码只依赖 Python 标准库，不发起网络请求。

## 目录

- `../../bin/railfare`：项目统一命令入口。
- `railfare.py`：主程序（纯标准库）。
- `pyproject.toml`、`uv.lock`：独立 uv 项目与锁定依赖（零第三方依赖）。
- `tests/`：使用虚构数据的单元测试。

## 快速使用

在 `/pi/jmapx` 根目录运行：

```bash
# 直接解析 PDF（自动调用 bin/pdftext 提取文本层）
bin/railfare parse --pdf "高铁/2026-08-19 长沙南 - 郴州西 174.00元 报销凭证.pdf"

# 人类可读输出
bin/railfare parse --pdf "票据.pdf" --format text

# 直接传入已提取的文本（如来自 pdftext extract 的结果）
bin/railfare parse --text "发票号码:... ￥174.00"

# 从标准输入读取文本
cat 票据.txt | bin/railfare parse --stdin

# 严格模式：任何警告都视为错误（适合自动化流水线）
bin/railfare parse --pdf "票据.pdf" --strict
```

退出码：解析成功为 `0`，存在错误（含严格模式下的警告）为 `2`。

## 解析字段

发票号码、开票日期、车次、出发站、到达站、乘车日期、开车时间、座位（车厢/排/号）、座位等级、乘车人姓名、证件号（脱敏）、电子客票号、购买方名称、统一社会信用代码、票面金额。

字段提取使用全文正则扫描，不依赖行顺序，容忍 PDF 文本层行序与视觉顺序不一致。

## 格式变化自动检测

铁路电子客票版式基本固定，但若未来改版，脚本通过三层机制自动报警：

1. **行模板匹配**：13 种预定义行模板逐一匹配文本的每一行。
   - 必需模板缺失（如 `电子发票（铁路电子客票）` 标题行、金额行）→ 错误，`ok: false`；
   - 可选模板缺失（如英文站名、12306 提示、座位等级）→ 警告；
   - 无法识别的行 → 警告，并输出该行原文。
2. **字段级正则**：关键字段（发票号码、日期、车次、站名、金额等）解析失败 → 错误。
3. **交叉校验**（均为警告级别）：
   - 开票日期早于乘车日期；
   - `--pdf` 模式下，文件名中的金额 / 日期 / 站点与票面内容不一致（可用 `--no-file-check` 关闭）。

另外，结果中的 `structure.line_patterns` 输出整篇凭证的「结构签名」（数字归一为 `⟨N⟩`、字母归一为 `⟨X⟩`，中文保留），便于人工逐行对比版式变化。

## JSON 输出结构

```json
{
  "schema_version": 1,
  "ok": true,
  "command": "parse",
  "fields": { "invoice_no": "...", "amount": "174.00" },
  "structure": {
    "matched": [],
    "missing_required": [],
    "missing_optional": [],
    "unrecognized_lines": [],
    "line_patterns": []
  },
  "warnings": [],
  "errors": []
}
```

## 测试

```bash
cd /pi/jmapx && /pi/uv/tool/bin/uv run --project tools/railfare python -m unittest discover -s tools/railfare/tests
```

测试全部使用虚构数据（不包含真实票据信息）。

## 隐私说明

输出含姓名、脱敏证件号、税号、发票号码、行程与金额，属敏感信息；禁止上传公网或第三方服务，临时文件请使用权限受控目录并及时清理。
