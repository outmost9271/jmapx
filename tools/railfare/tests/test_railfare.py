# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import railfare as tool

# 合成样例：结构与真实凭证一致，但人名、证件号、发票号、公司名、税号
# 均为虚构，未包含任何真实票据信息。
SAMPLE_FORWARD = """发票号码:12345678901234567890 开票日期:2026年08月20日
长沙南 G6049 郴州西
Changshanan Chenzhouxi
2026年08月19日 10:20开 04车11D号
票价:
二等座
1101011990****1234 李四
电子客票号:1234567890123456789012345
购买方名称:北京示例科技有限公司 统一社会信用代码:91110000MA0000000X
买票请到12306 发货请到95306
中国铁路祝您旅途愉快
电子发票（铁路电子客票）
￥174.00
"""

SAMPLE_RETURN = """发票号码:12345678901234567899 开票日期:2026年08月21日
郴州西 G1012 长沙南
Chenzhouxi Changshanan
2026年08月21日 19:16开 07车06D号
票价:
二等座
1101011990****1234 李四
电子客票号:1234567890123456789012346
购买方名称:北京示例科技有限公司 统一社会信用代码:91110000MA0000000X
买票请到12306 发货请到95306
中国铁路祝您旅途愉快
电子发票（铁路电子客票）
￥175.00
"""


class ParseFieldsTests(unittest.TestCase):
    def test_forward_voucher_fields(self) -> None:
        fields, errors = tool.parse_fields(SAMPLE_FORWARD)
        self.assertEqual(errors, [])
        self.assertEqual(fields["invoice_no"], "12345678901234567890")
        self.assertEqual(fields["issue_date"], "2026-08-20")
        self.assertEqual(fields["train_no"], "G6049")
        self.assertEqual(fields["departure_station"], "长沙南")
        self.assertEqual(fields["arrival_station"], "郴州西")
        self.assertEqual(fields["ride_date"], "2026-08-19")
        self.assertEqual(fields["departure_time"], "10:20")
        self.assertEqual(fields["seat"], "04车11D号")
        self.assertEqual(fields["seat_class"], "二等座")
        self.assertEqual(fields["passenger"], "李四")
        self.assertEqual(fields["passport_masked"], "1101011990****1234")
        self.assertEqual(fields["ticket_no"], "1234567890123456789012345")
        self.assertEqual(fields["buyer_name"], "北京示例科技有限公司")
        self.assertEqual(fields["buyer_tax_id"], "91110000MA0000000X")
        self.assertEqual(fields["amount"], "174.00")

    def test_return_voucher_fields(self) -> None:
        fields, errors = tool.parse_fields(SAMPLE_RETURN)
        self.assertEqual(errors, [])
        self.assertEqual(fields["train_no"], "G1012")
        self.assertEqual(fields["departure_station"], "郴州西")
        self.assertEqual(fields["arrival_station"], "长沙南")
        self.assertEqual(fields["amount"], "175.00")

    def test_missing_amount_reports_error(self) -> None:
        text = SAMPLE_FORWARD.replace("￥174.00", "壹佰柒拾肆元整")
        fields, errors = tool.parse_fields(text)
        self.assertNotIn("amount", fields)
        self.assertTrue(any("票面金额" in item for item in errors))

    def test_invalid_issue_date_reports_error(self) -> None:
        text = SAMPLE_FORWARD.replace("2026年08月20日", "2026年13月40日")
        _, errors = tool.parse_fields(text)
        self.assertTrue(any("开票日期" in item for item in errors))


class StructureCheckTests(unittest.TestCase):
    def test_all_lines_matched_on_normal_voucher(self) -> None:
        structure = tool.check_structure(SAMPLE_FORWARD.splitlines())
        self.assertEqual(structure["missing_required"], [])
        self.assertEqual(structure["missing_optional"], [])
        self.assertEqual(structure["unrecognized_lines"], [])
        self.assertEqual(len(structure["matched"]), 13)

    def test_unknown_line_detected(self) -> None:
        text = SAMPLE_FORWARD.replace(
            "电子发票（铁路电子客票）",
            "电子发票（铁路电子客票）\n新的宣传标语",
        )
        structure = tool.check_structure(text.splitlines())
        self.assertEqual(len(structure["unrecognized_lines"]), 1)
        self.assertEqual(structure["unrecognized_lines"][0]["text"], "新的宣传标语")

    def test_missing_required_template_detected(self) -> None:
        text = SAMPLE_FORWARD.replace("电子发票（铁路电子客票）\n", "")
        structure = tool.check_structure(text.splitlines())
        missing = {item["template"] for item in structure["missing_required"]}
        self.assertIn("title", missing)

    def test_line_patterns_signature(self) -> None:
        structure = tool.check_structure(SAMPLE_FORWARD.splitlines())
        patterns = [item["pattern"] for item in structure["line_patterns"]]
        self.assertIn("发票号码:⟨N⟩ 开票日期:⟨N⟩年⟨N⟩月⟨N⟩日", patterns)
        self.assertIn("长沙南 ⟨X⟩⟨N⟩ 郴州西", patterns)
        self.assertIn("￥⟨N⟩.⟨N⟩", patterns)


class AnalyzeTests(unittest.TestCase):
    def test_normal_voucher_is_ok(self) -> None:
        result = tool.analyze(
            SAMPLE_FORWARD, file_name="2026-08-19 长沙南 - 郴州西 174.00元 报销凭证.pdf"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["warnings"], [])

    def test_file_name_amount_mismatch_warns(self) -> None:
        result = tool.analyze(
            SAMPLE_FORWARD, file_name="2026-08-19 长沙南 - 郴州西 200.00元 报销凭证.pdf"
        )
        self.assertTrue(result["ok"])
        self.assertTrue(any("文件名金额" in item for item in result["warnings"]))

    def test_file_name_station_mismatch_warns(self) -> None:
        result = tool.analyze(
            SAMPLE_FORWARD, file_name="2026-08-19 长沙南 - 广州南 174.00元 报销凭证.pdf"
        )
        self.assertTrue(any("文件名站点" in item for item in result["warnings"]))

    def test_issue_date_before_ride_date_warns(self) -> None:
        text = SAMPLE_FORWARD.replace("2026年08月20日", "2026年08月18日")
        result = tool.analyze(text)
        self.assertTrue(result["ok"])
        self.assertTrue(
            any(
                "开票日期" in item and "乘车日期" in item for item in result["warnings"]
            )
        )

    def test_missing_required_line_fails(self) -> None:
        text = SAMPLE_FORWARD.replace("电子发票（铁路电子客票）\n", "")
        result = tool.analyze(text)
        self.assertFalse(result["ok"])
        self.assertTrue(any("必需行模板" in item for item in result["errors"]))

    def test_strict_promotes_warnings_to_errors(self) -> None:
        text = SAMPLE_FORWARD.replace(
            "电子发票（铁路电子客票）",
            "电子发票（铁路电子客票）\n新的宣传标语",
        )
        result = tool.analyze(text, strict=False)
        self.assertTrue(result["ok"])
        strict_result = tool.analyze(text, strict=True)
        self.assertFalse(strict_result["ok"])
        self.assertEqual(strict_result["warnings"], [])

    def test_empty_text_fails(self) -> None:
        result = tool.analyze("")
        self.assertFalse(result["ok"])
        self.assertGreaterEqual(len(result["errors"]), 1)


class MainCliTests(unittest.TestCase):
    def test_main_text_json_exit_zero(self) -> None:
        code = tool.main(["parse", "--text", SAMPLE_FORWARD])
        self.assertEqual(code, 0)

    def test_main_text_format_text(self) -> None:
        code = tool.main(["parse", "--text", SAMPLE_FORWARD, "--format", "text"])
        self.assertEqual(code, 0)

    def test_main_broken_voucher_exit_two(self) -> None:
        code = tool.main(["parse", "--text", "完全不是凭证内容"])
        self.assertEqual(code, 2)

    def test_main_requires_source(self) -> None:
        with self.assertRaises(SystemExit):
            tool.main(["parse"])


if __name__ == "__main__":
    unittest.main()
