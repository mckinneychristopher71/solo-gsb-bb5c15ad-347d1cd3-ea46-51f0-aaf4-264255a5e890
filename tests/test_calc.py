"""计算核心测试（仅依赖标准库，可在未安装 FastAPI 时运行）。

运行：python -m pytest tests/test_calc.py  或  python -m unittest tests.test_calc
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from app.calc import RULE_ID, compute_invoice, recompute_matches


def line(net: str, rate: str, desc: str | None = None) -> dict:
    return {"net": net, "tax_rate": rate, "description": desc}


class RoundingAndResidualTests(unittest.TestCase):
    def test_basic_half_up_and_no_residual(self):
        r = compute_invoice(
            {"currency": "USD", "lines": [line("100.00", "0.10"), line("50.00", "0.20")]}
        )
        self.assertEqual(r["lines"][0]["tax"], "10.00")
        self.assertEqual(r["lines"][1]["tax"], "10.00")
        self.assertEqual(r["totals"]["total_tax"], "20.00")
        self.assertEqual(r["totals"]["total_net"], "150.00")
        self.assertEqual(r["totals"]["total_gross"], "170.00")
        self.assertEqual(r["rounding"]["residual_units"], 0)
        self.assertEqual(r["rounding"]["allocations"], [])

    def test_residual_one_cent_largest_remainder(self):
        # 1.005 -> HALF_UP 1.01 ; 2.005 -> 2.01；行和 3.02，总额(30.10*0.1=3.01) -> 残差 -1
        r = compute_invoice(
            {
                "currency": "USD",
                "lines": [line("10.05", "0.10"), line("20.05", "0.10")],
            }
        )
        taxes = [l["tax"] for l in r["lines"]]
        # 零头相同（都是 0.005），负残差从零头小的先扣，相同则行号小的先扣
        self.assertEqual(taxes, ["1.00", "2.01"])
        self.assertEqual(sum(Decimal(t) for t in taxes), Decimal(r["totals"]["total_tax"]))
        self.assertEqual(r["rounding"]["residual_units"], -1)
        self.assertEqual(r["rounding"]["allocations"][0]["line_no"], 1)

    def test_largest_remainder_picks_biggest_fraction(self):
        # 经典案例：三行各 10.00 @3% -> 精确税 0.30/行，总额 0.90，无残差；
        # 改为净额 3.33 等使零头不同
        r = compute_invoice(
            {
                "currency": "USD",
                "lines": [
                    line("1.00", "0.10"),  # 0.10 exact
                    line("1.01", "0.10"),  # 0.101 -> 0.10, rem .001
                    line("1.05", "0.10"),  # 0.105 -> 0.11, rem .005
                ],
            }
        )
        # exact total = 0.306 -> 0.31；行舍入和 = 0.10+0.10+0.11 = 0.31，无残差
        self.assertEqual(r["totals"]["total_tax"], "0.31")
        self.assertEqual(r["rounding"]["residual_units"], 0)

    def test_positive_residual_allocated_to_largest_remainder(self):
        # 构造：每行精确税额零头都 < 0.5，但总额零头 >= 0.5
        # 两行 net 各 0.02 @0.25 不可行（net 须 0.01 整数倍）
        # net 0.03 @ 0.20 = 0.006 -> 0.01；三行：行和 0.03，精确总额 0.018 -> 0.02，残差 -1
        # 正残差场景：net 0.01 @ 0.30 = 0.003 -> 0.00(rounds down? HALF_UP .003 -> .00)
        # 4 行：行和 0.00，总额 0.012 -> 0.01，残差 +1，分给零头最大者(并列->line_no 1)
        r = compute_invoice(
            {
                "currency": "USD",
                "lines": [line("0.01", "0.30") for _ in range(4)],
            }
        )
        self.assertEqual(r["totals"]["total_tax"], "0.01")
        taxes = [l["tax"] for l in r["lines"]]
        self.assertEqual(taxes, ["0.01", "0.00", "0.00", "0.00"])
        self.assertEqual(r["rounding"]["residual_units"], 1)
        self.assertEqual(r["rounding"]["allocations"][0]["line_no"], 1)
        # 不变量：行税合计 == 总税
        self.assertEqual(
            sum(Decimal(t) for t in taxes), Decimal(r["totals"]["total_tax"])
        )

    def test_zero_decimal_currency_jpy(self):
        r = compute_invoice(
            {"currency": "JPY", "lines": [line("100", "0.10"), line("200", "0.10")]}
        )
        self.assertEqual(r["currency_digits"], 0)
        self.assertEqual(r["rounding"]["minor_unit"], "1")
        self.assertEqual(r["totals"]["total_tax"], "30")

    def test_three_decimal_currency_kwd(self):
        r = compute_invoice({"currency": "KWD", "lines": [line("1.001", "0.10")]})
        self.assertEqual(r["currency_digits"], 3)
        self.assertEqual(r["lines"][0]["tax"], "0.100")

    def test_half_up_005_rounds_up(self):
        # 0.005 HALF_UP -> 0.01
        r = compute_invoice({"currency": "USD", "lines": [line("0.05", "0.10")]})
        self.assertEqual(r["lines"][0]["exact_tax"], "0.005")
        self.assertEqual(r["lines"][0]["tax"], "0.01")

    def test_negative_net_credit_note(self):
        r = compute_invoice(
            {
                "currency": "USD",
                "lines": [line("-100.00", "0.10"), line("-100.00", "0.10")],
            }
        )
        self.assertEqual(r["totals"]["total_tax"], "-20.00")
        self.assertEqual(r["totals"]["total_gross"], "-220.00")

    def test_zero_tax_rate(self):
        r = compute_invoice({"currency": "USD", "lines": [line("100.00", "0")]})
        self.assertEqual(r["lines"][0]["tax"], "0.00")
        self.assertEqual(r["rounding"]["residual_units"], 0)


class FxTests(unittest.TestCase):
    def test_fx_keeps_original_and_converts(self):
        r = compute_invoice(
            {
                "currency": "USD",
                "target_currency": "CNY",
                "exchange_rate": "7.123456",
                "lines": [line("100.00", "0.13"), line("50.00", "0.06")],
            }
        )
        # 原币保留
        self.assertEqual(r["totals"]["total_net"], "150.00")
        self.assertEqual(r["lines"][0]["tax"], "13.00")
        # 换算：100*7.123456 = 712.3456 -> 712.35
        self.assertEqual(r["lines"][0]["converted"]["net"], "712.35")
        # 精确税 100*0.13*7.123456 = 92.604928 -> 92.60；
        # 行2: 50*0.06*7.123456 = 21.370368 -> 21.37；行舍入和 113.97，
        # 精确总额 113.975296 -> 113.98，残差 +1 分给零头更大的第 1 行 -> 92.61
        self.assertEqual(r["lines"][0]["converted"]["tax"], "92.61")
        self.assertEqual(r["lines"][1]["converted"]["tax"], "21.37")
        self.assertEqual(r["fx"]["total_tax"], "113.98")
        self.assertEqual(r["fx"]["residual_units"], 1)
        self.assertEqual(r["fx"]["target_currency"], "CNY")
        self.assertEqual(r["fx"]["exchange_rate"], "7.123456")
        self.assertIn("1 USD = 7.123456 CNY", r["fx"]["rate_direction"])
        # 目标币行税合计 == 目标币总税
        conv_tax_sum = sum(Decimal(l["converted"]["tax"]) for l in r["lines"])
        self.assertEqual(conv_tax_sum, Decimal(r["fx"]["total_tax"]))

    def test_fx_to_zero_digit_currency(self):
        r = compute_invoice(
            {
                "currency": "USD",
                "target_currency": "JPY",
                "exchange_rate": "150",
                "lines": [line("1.00", "0.10"), line("1.00", "0.10")],
            }
        )
        # 1*150 = 150；税 15/行，总额 30，无残差
        self.assertEqual(r["lines"][0]["converted"]["net"], "150")
        self.assertEqual(r["fx"]["total_tax"], "30")

    def test_fx_requires_rate(self):
        with self.assertRaises(ValueError):
            compute_invoice({"currency": "USD", "target_currency": "CNY",
                             "lines": [line("100.00", "0.1")]})

    def test_fx_same_currency_rejected(self):
        with self.assertRaises(ValueError):
            compute_invoice({"currency": "USD", "target_currency": "USD",
                             "exchange_rate": "1", "lines": [line("100.00", "0.1")]})


class ValidationTests(unittest.TestCase):
    def test_unknown_currency(self):
        with self.assertRaises(ValueError):
            compute_invoice({"currency": "ZZZ", "lines": [line("1.00", "0.1")]})

    def test_float_rejected(self):
        with self.assertRaises(ValueError):
            compute_invoice({"currency": "USD", "lines": [{"net": 1.0, "tax_rate": "0.1"}]})

    def test_net_not_multiple_of_minor_unit(self):
        with self.assertRaises(ValueError):
            compute_invoice({"currency": "USD", "lines": [line("1.005", "0.1")]})

    def test_rate_out_of_range(self):
        with self.assertRaises(ValueError):
            compute_invoice({"currency": "USD", "lines": [line("1.00", "1.5")]})

    def test_empty_lines(self):
        with self.assertRaises(ValueError):
            compute_invoice({"currency": "USD", "lines": []})


class DeterminismTests(unittest.TestCase):
    def test_recompute_is_identical(self):
        payload = {
            "currency": "USD",
            "target_currency": "CNY",
            "exchange_rate": "7.123456",
            "lines": [
                line("3.33", "0.13"),
                line("7.77", "0.06"),
                line("9.99", "0.13"),
            ],
        }
        r1 = compute_invoice(payload)
        r2 = compute_invoice(payload)
        self.assertEqual(r1, r2)
        self.assertTrue(recompute_matches(r1, payload))
        self.assertEqual(r1["rule_id"], RULE_ID)

    def test_tie_break_by_line_order(self):
        # 四行完全相同：正残差 1 时必须分给第 1 行（零头相同按行号）
        r = compute_invoice(
            {"currency": "USD", "lines": [line("0.01", "0.30") for _ in range(4)]}
        )
        assigned = [a["line_no"] for a in r["rounding"]["allocations"]]
        self.assertEqual(assigned, [1])


if __name__ == "__main__":
    unittest.main()
