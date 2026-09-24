"""端到端测试：真实 uvicorn 起服务 + stdlib urllib 调用，覆盖
创建 / 查询 / 分页 / 健康检查 / 422 / 复算标志 / 收款与冲销 / SQLite 持久化。

运行：先 pip install -r requirements.txt，再
    python -m unittest tests.test_api -v
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import uvicorn


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerThread(threading.Thread):
    def __init__(self, db_file: str, port: int) -> None:
        super().__init__(daemon=True)
        self.db_file = db_file
        self.port = port

    def run(self) -> None:
        uvicorn.run(
            "app.main:app",
            host="127.0.0.1",
            port=self.port,
            log_level="error",
        )


class ApiTests(unittest.TestCase):
    server: ServerThread
    tmpdir: tempfile.TemporaryDirectory
    base: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.TemporaryDirectory()
        db_file = str(Path(cls.tmpdir.name) / "test.db")
        os.environ["DB_PATH"] = db_file
        port = _free_port()
        cls.base = f"http://127.0.0.1:{port}"
        cls.server = ServerThread(db_file, port)
        cls.server.start()
        cls._wait_ready()

    @classmethod
    def _wait_ready(cls, timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{cls.base}/health", timeout=1) as resp:
                    if resp.status == 200:
                        return
            except Exception as exc:  # 启动期间连接被拒是正常的
                last = exc
                time.sleep(0.2)
        raise RuntimeError(f"服务未在 {timeout}s 内就绪: {last}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmpdir.cleanup()
        os.environ.pop("DB_PATH", None)

    def _request(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_01_health(self) -> None:
        status, body = self._request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok", "database": "up"})

    def test_02_create_and_get(self) -> None:
        payload = {
            "currency": "USD",
            "lines": [
                {"description": "books", "net": "10.05", "tax_rate": "0.10"},
                {"description": "pens", "net": "20.05", "tax_rate": "0.10"},
            ],
        }
        status, body = self._request("POST", "/api/v1/invoices", payload)
        self.assertEqual(status, 201, body)
        self.assertIn("id", body)
        self.assertEqual(body["totals"]["total_tax"], "3.01")
        self.assertEqual(body["rounding"]["residual_units"], -1)
        self.assertTrue(body["recompute_matched"])

        invoice_id = body["id"]
        status2, fetched = self._request("GET", f"/api/v1/invoices/{invoice_id}")
        self.assertEqual(status2, 200)
        self.assertEqual(fetched["totals"], body["totals"])
        self.assertTrue(fetched["recompute_matched"])
        self.assertEqual(len(fetched["lines"]), 2)

    def test_03_create_fx(self) -> None:
        payload = {
            "currency": "USD",
            "target_currency": "JPY",
            "exchange_rate": "150.25",
            "lines": [{"net": "1.00", "tax_rate": "0.10"}],
        }
        status, body = self._request("POST", "/api/v1/invoices", payload)
        self.assertEqual(status, 201, body)
        self.assertEqual(body["fx"]["target_currency"], "JPY")
        # 150.25 -> 150 JPY；税 15.025 -> 15 JPY
        self.assertEqual(body["lines"][0]["converted"]["net"], "150")
        self.assertEqual(body["lines"][0]["converted"]["tax"], "15")
        # 原币保留
        self.assertEqual(body["lines"][0]["net"], "1.00")
        self.assertEqual(body["lines"][0]["tax"], "0.10")

    def test_04_validation_error(self) -> None:
        # 净额不是最小单位整数倍
        status, body = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "1.005", "tax_rate": "0.1"}]},
        )
        self.assertEqual(status, 422)
        self.assertIn("detail", body)

        # 未知币种
        status, body = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "ZZZ", "lines": [{"net": "1.00", "tax_rate": "0.1"}]},
        )
        self.assertEqual(status, 422)

        # pydantic 校验：金额必须是字符串
        status, body = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": 1.0, "tax_rate": "0.1"}]},
        )
        self.assertEqual(status, 422)

    def test_05_not_found(self) -> None:
        status, body = self._request("GET", "/api/v1/invoices/999999")
        self.assertEqual(status, 404)

    def test_06_pagination_and_persistence(self) -> None:
        # 再建 3 张，配合前两张共 5 张
        for i in range(3):
            status, body = self._request(
                "POST",
                "/api/v1/invoices",
                {"currency": "CNY", "lines": [{"net": "100.00", "tax_rate": "0.13"}]},
            )
            self.assertEqual(status, 201)

        status, body = self._request("GET", "/api/v1/invoices?page=1&page_size=2")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 5)
        self.assertEqual(body["pages"], 3)
        self.assertEqual(body["page"], 1)
        self.assertEqual(body["page_size"], 2)
        self.assertEqual(len(body["items"]), 2)
        # 按 id 倒序
        self.assertGreater(body["items"][0]["id"], body["items"][1]["id"])
        self.assertEqual(body["items"][0]["line_count"], 1)
        self.assertEqual(body["items"][0]["currency"], "CNY")

        status, body = self._request("GET", "/api/v1/invoices?page=3&page_size=2")
        self.assertEqual(len(body["items"]), 1)

        # 分页参数越界 -> 422
        status, _ = self._request("GET", "/api/v1/invoices?page=0")
        self.assertEqual(status, 422)

    def test_07_persisted_db_file(self) -> None:
        # SQLite 文件确实落盘
        self.assertTrue(os.path.exists(os.environ["DB_PATH"]))
        self.assertGreater(os.path.getsize(os.environ["DB_PATH"]), 0)

    def test_08_payments(self) -> None:
        # 建一张 USD 发票：total_gross = 33.11
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {
                "currency": "USD",
                "lines": [
                    {"net": "10.05", "tax_rate": "0.10"},
                    {"net": "20.05", "tax_rate": "0.10"},
                ],
            },
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        self.assertEqual(inv["totals"]["total_gross"], "33.11")

        # 初始：无收款，未收余额 = 含税总额
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(status, 200)
        self.assertEqual(ov["total_gross"], "33.11")
        self.assertEqual(ov["paid_total"], "0.00")
        self.assertEqual(ov["outstanding"], "33.11")
        self.assertEqual(ov["payment_count"], 0)
        self.assertEqual(ov["payments"], [])

        # 登记第一笔收款
        status, pay = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "20"}
        )
        self.assertEqual(status, 201, pay)
        self.assertEqual(pay["invoice_id"], invoice_id)
        self.assertEqual(pay["amount"], "20.00")  # 按币种最小单位规范化
        self.assertEqual(pay["currency"], "USD")
        self.assertIn("created_at", pay)
        self.assertEqual(pay["paid_total"], "20.00")
        self.assertEqual(pay["outstanding"], "13.11")

        # 第二笔：恰好收满
        status, pay = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "13.11"}
        )
        self.assertEqual(status, 201, pay)
        self.assertEqual(pay["paid_total"], "33.11")
        self.assertEqual(pay["outstanding"], "0.00")

        # 查询：两笔记录，按登记顺序返回，均带登记时间
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(status, 200)
        self.assertEqual(ov["payment_count"], 2)
        self.assertEqual([p["amount"] for p in ov["payments"]], ["20.00", "13.11"])
        for p in ov["payments"]:
            self.assertIn("created_at", p)
        self.assertEqual(ov["paid_total"], "33.11")
        self.assertEqual(ov["outstanding"], "0.00")

        # 已收满，再收 0.01 -> 422，且不留下记录
        status, body = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "0.01"}
        )
        self.assertEqual(status, 422)
        self.assertIn("detail", body)
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(ov["payment_count"], 2)
        self.assertEqual(ov["paid_total"], "33.11")

    def test_09_payment_validation(self) -> None:
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "10.00", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        path = f"/api/v1/invoices/{invoice_id}/payments"

        # 超额（累计 > 含税总额 10.00）
        status, _ = self._request("POST", path, {"amount": "10.01"})
        self.assertEqual(status, 422)
        # 零 / 负数
        status, _ = self._request("POST", path, {"amount": "0"})
        self.assertEqual(status, 422)
        status, _ = self._request("POST", path, {"amount": "-5.00"})
        self.assertEqual(status, 422)
        # 非十进制字符串 / 非字符串
        status, _ = self._request("POST", path, {"amount": "abc"})
        self.assertEqual(status, 422)
        status, _ = self._request("POST", path, {"amount": 1.5})
        self.assertEqual(status, 422)
        # 小于币种最小单位
        status, _ = self._request("POST", path, {"amount": "0.001"})
        self.assertEqual(status, 422)

        # 以上全部失败，不得留下任何记录
        status, ov = self._request("GET", path)
        self.assertEqual(ov["payment_count"], 0)
        self.assertEqual(ov["paid_total"], "0.00")
        self.assertEqual(ov["outstanding"], "10.00")

        # 不存在的发票：登记 404、查询 404
        status, _ = self._request(
            "POST", "/api/v1/invoices/999999/payments", {"amount": "1.00"}
        )
        self.assertEqual(status, 404)
        status, _ = self._request("GET", "/api/v1/invoices/999999/payments")
        self.assertEqual(status, 404)

    def test_10_concurrent_payments_no_overpay(self) -> None:
        # total_gross = 100.00，10 个并发请求各付 20.00：
        # 同事务校验+写入，恰好 5 笔成功、5 笔 422，最终不超收
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "100.00", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]

        codes: list[int] = []
        lock = threading.Lock()

        def pay() -> None:
            code, _ = self._request(
                "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "20.00"}
            )
            with lock:
                codes.append(code)

        threads = [threading.Thread(target=pay) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(codes), [201] * 5 + [422] * 5)
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(ov["payment_count"], 5)
        self.assertEqual(ov["paid_total"], "100.00")
        self.assertEqual(ov["outstanding"], "0.00")

    def test_11_due_date_persisted(self) -> None:
        # 带 due_date 创建并持久化，GET 单张时原样返回，且复算仍然一致
        payload = {
            "currency": "USD",
            "due_date": "2026-10-01",
            "lines": [{"net": "10.00", "tax_rate": "0.10"}],
        }
        status, body = self._request("POST", "/api/v1/invoices", payload)
        self.assertEqual(status, 201, body)
        self.assertEqual(body["due_date"], "2026-10-01")
        self.assertTrue(body["recompute_matched"])
        invoice_id = body["id"]

        status, fetched = self._request("GET", f"/api/v1/invoices/{invoice_id}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["due_date"], "2026-10-01")
        self.assertTrue(fetched["recompute_matched"])

        # 不传 due_date：响应中为 null，不影响其他字段
        status, body = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "10.00", "tax_rate": "0.10"}]},
        )
        self.assertEqual(status, 201, body)
        self.assertIsNone(body["due_date"])

        # 非法日期一律 422：格式不符 / 不存在的日期 / 带时间成分
        for bad in ("2026-10", "2026-10-01T00:00:00Z", "2026/10/01",
                    "2026-13-01", "2026-02-30", "not-a-date"):
            status, resp = self._request(
                "POST",
                "/api/v1/invoices",
                {
                    "currency": "USD",
                    "due_date": bad,
                    "lines": [{"net": "10.00", "tax_rate": "0.10"}],
                },
            )
            self.assertEqual(status, 422, bad)
            self.assertIn("detail", resp)

    def test_12_overdue_query(self) -> None:
        # 构造发票集合，固定 as_of=2026-09-23
        def make(due_date, currency="USD", lines=None, pay=None):
            payload = {"currency": currency, "lines": lines or [{"net": "10.00", "tax_rate": "0"}]}
            if due_date is not None:
                payload["due_date"] = due_date
            status, inv = self._request("POST", "/api/v1/invoices", payload)
            self.assertEqual(status, 201, inv)
            if pay:
                status, pay_resp = self._request(
                    "POST", f"/api/v1/invoices/{inv['id']}/payments", {"amount": pay}
                )
                self.assertEqual(status, 201, pay_resp)
            return inv["id"]

        id_a = make("2026-09-22", lines=[
            {"net": "10.05", "tax_rate": "0.10"},
            {"net": "20.05", "tax_rate": "0.10"},
        ])  # gross 33.11，逾期未付
        make("2026-09-23")  # due_date == as_of，不逾期
        make("2026-09-10", pay="10.00")  # 已收满，排除
        id_d = make("2026-09-01", currency="JPY",
                    lines=[{"net": "1000", "tax_rate": "0.10"}])  # gross 1100，0 位小数
        id_e = make("2026-09-20",
                    lines=[{"net": "100.00", "tax_rate": "0"}],
                    pay="40.00")  # gross 100.00，部分收款
        make(None)  # 无到期日，排除
        make("2026-12-31")  # 未来到期，排除
        id_h = make("2000-01-01")  # 远古到期，缺省 as_of（UTC 今日）下也必逾期

        # 固定 as_of：共 4 张逾期（早前测试创建的发票均无 due_date，不计入）
        status, body = self._request(
            "GET", "/api/v1/invoices/overdue?as_of=2026-09-23&page=1&page_size=2"
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["as_of"], "2026-09-23")
        self.assertEqual(body["total"], 4)
        self.assertEqual(body["pages"], 2)
        self.assertEqual(body["page"], 1)
        self.assertEqual(body["page_size"], 2)
        # 按 id 倒序：H(最晚建) 在前，A 最后
        self.assertEqual([it["id"] for it in body["items"]], [id_h, id_e])

        status, page2 = self._request(
            "GET", "/api/v1/invoices/overdue?as_of=2026-09-23&page=2&page_size=2"
        )
        self.assertEqual(status, 200)
        self.assertEqual([it["id"] for it in page2["items"]], [id_d, id_a])
        self.assertEqual(page2["total"], 4)

        by_id = {it["id"]: it for it in body["items"] + page2["items"]}

        # 部分收款：精确未收余额 = 100.00 - 40.00 = 60.00
        e = by_id[id_e]
        self.assertEqual(e["currency"], "USD")
        self.assertEqual(e["due_date"], "2026-09-20")
        self.assertEqual(e["total_gross"], "100.00")
        self.assertEqual(e["paid_total"], "40.00")
        self.assertEqual(e["outstanding"], "60.00")
        self.assertIn("created_at", e)

        # JPY：0 位小数，金额不带小数点
        d = by_id[id_d]
        self.assertEqual(d["currency"], "JPY")
        self.assertEqual(d["total_gross"], "1100")
        self.assertEqual(d["paid_total"], "0")
        self.assertEqual(d["outstanding"], "1100")

        # 未付 USD 发票
        a = by_id[id_a]
        self.assertEqual(a["total_gross"], "33.11")
        self.assertEqual(a["paid_total"], "0.00")
        self.assertEqual(a["outstanding"], "33.11")

        # 缺省 as_of：取 UTC 当前日期；远古到期的 H 必在结果中
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        status, body = self._request(
            "GET", "/api/v1/invoices/overdue?page_size=100"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["as_of"], today)
        self.assertIn(id_h, [it["id"] for it in body["items"]])
        # 回显的每个条目都必须满足：due_date < as_of、outstanding > 0
        from decimal import Decimal
        for it in body["items"]:
            self.assertLess(it["due_date"], today)
            self.assertGreater(Decimal(it["outstanding"]), 0)

        # 非法 as_of -> 422
        for bad in ("2026-09", "2026-09-23T00:00:00Z", "2026/09/23",
                    "2026-13-01", "not-a-date"):
            status, resp = self._request(
                "GET", f"/api/v1/invoices/overdue?as_of={bad}"
            )
            self.assertEqual(status, 422, bad)
            self.assertIn("detail", resp)

        # 分页参数越界 -> 422
        status, _ = self._request("GET", "/api/v1/invoices/overdue?page=0")
        self.assertEqual(status, 422)
        status, _ = self._request("GET", "/api/v1/invoices/overdue?page_size=101")
        self.assertEqual(status, 422)

    def test_13_payment_allocation(self) -> None:
        # 待结发票：
        #   A: USD 11.00 due 2026-09-10
        #   B: USD 22.00 due 2026-09-05（最早到期）
        #   C: USD 33.00 无到期日（排最后）
        #   D: USD 44.00 due 2026-09-10（与 A 同日，id 大于 A）
        #   J: JPY 1000   due 2026-08-01
        def make(currency, gross, due_date):
            payload = {"currency": currency, "lines": [{"net": gross, "tax_rate": "0"}]}
            if due_date is not None:
                payload["due_date"] = due_date
            status, inv = self._request("POST", "/api/v1/invoices", payload)
            self.assertEqual(status, 201, inv)
            return inv["id"]

        id_a = make("USD", "11.00", "2026-09-10")
        id_b = make("USD", "22.00", "2026-09-05")
        id_c = make("USD", "33.00", None)
        id_d = make("USD", "44.00", "2026-09-10")
        id_j = make("JPY", "1000", "2026-08-01")
        self.assertLess(id_a, id_d)  # 同日按 id 升序

        # 传入顺序故意打乱；分配顺序仍为 B -> A -> D -> C
        status, body = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "usd", "amount": "50", "invoice_ids": [id_c, id_d, id_b, id_a]},
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["currency"], "USD")
        self.assertEqual(body["amount"], "50.00")
        self.assertEqual(body["allocated_total"], "50.00")
        self.assertIn("created_at", body)
        self.assertEqual(
            [(it["invoice_id"], it["allocated"], it["outstanding"])
             for it in body["allocations"]],
            [
                (id_b, "22.00", "0.00"),
                (id_a, "11.00", "0.00"),
                (id_d, "17.00", "27.00"),
                (id_c, "0.00", "33.00"),
            ],
        )
        # 实际抵扣的三张各带 payment_id；未抵扣的 C 不产生流水
        self.assertIsNone(body["allocations"][3]["payment_id"])
        for it in body["allocations"][:3]:
            self.assertIsInstance(it["payment_id"], int)
        # 到期日按顺序回显
        self.assertEqual(
            [it["due_date"] for it in body["allocations"]],
            ["2026-09-05", "2026-09-10", "2026-09-10", None],
        )

        # 单张收款查询必须看得到分配结果（沿用 payments 流水）
        status, ov = self._request("GET", f"/api/v1/invoices/{id_b}/payments")
        self.assertEqual(status, 200)
        self.assertEqual(ov["payment_count"], 1)
        self.assertEqual(ov["payments"][0]["amount"], "22.00")
        self.assertEqual(ov["payments"][0]["currency"], "USD")
        self.assertEqual(ov["paid_total"], "22.00")
        self.assertEqual(ov["outstanding"], "0.00")

        status, ov = self._request("GET", f"/api/v1/invoices/{id_d}/payments")
        self.assertEqual(ov["payment_count"], 1)
        self.assertEqual(ov["paid_total"], "17.00")
        self.assertEqual(ov["outstanding"], "27.00")

        # 未抵扣：没有流水
        status, ov = self._request("GET", f"/api/v1/invoices/{id_c}/payments")
        self.assertEqual(ov["payment_count"], 0)
        self.assertEqual(ov["paid_total"], "0.00")
        self.assertEqual(ov["outstanding"], "33.00")

        # 逾期查询反映分配后的余额：A/B 已收满被排除，D 仍在
        status, overdue = self._request(
            "GET", "/api/v1/invoices/overdue?as_of=2026-09-23&page_size=100"
        )
        self.assertEqual(status, 200)
        overdue_ids = {it["id"]: it for it in overdue["items"]}
        self.assertNotIn(id_a, overdue_ids)
        self.assertNotIn(id_b, overdue_ids)
        self.assertNotIn(id_c, overdue_ids)  # 无到期日
        d = overdue_ids[id_d]
        self.assertEqual(d["paid_total"], "17.00")
        self.assertEqual(d["outstanding"], "27.00")

        # ---- 整笔拒绝场景：任何失败都不得留下部分收款 ----

        # 总未收不足：D(27) + C(33) = 60，请求 60.01
        status, resp = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "60.01", "invoice_ids": [id_d, id_c]},
        )
        self.assertEqual(status, 422)
        self.assertIn("detail", resp)
        status, ov = self._request("GET", f"/api/v1/invoices/{id_d}/payments")
        self.assertEqual(ov["payment_count"], 1)  # 仍是原来的 17.00
        self.assertEqual(ov["paid_total"], "17.00")
        status, ov = self._request("GET", f"/api/v1/invoices/{id_c}/payments")
        self.assertEqual(ov["payment_count"], 0)

        # 币种不符（JPY 列表里混了 USD 发票）
        status, resp = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "JPY", "amount": "1000", "invoice_ids": [id_j, id_c]},
        )
        self.assertEqual(status, 422)
        self.assertIn("USD", resp["detail"])
        status, ov = self._request("GET", f"/api/v1/invoices/{id_j}/payments")
        self.assertEqual(ov["payment_count"], 0)

        # 发票不存在 -> 404，且不写入列表里其他发票
        status, resp = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "1.00",
             "invoice_ids": [id_c, 999999]},
        )
        self.assertEqual(status, 404)
        self.assertIn("999999", resp["detail"])
        status, ov = self._request("GET", f"/api/v1/invoices/{id_c}/payments")
        self.assertEqual(ov["payment_count"], 0)

        # 列表重复 / 为空
        status, _ = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "1.00",
             "invoice_ids": [id_c, id_c]},
        )
        self.assertEqual(status, 422)
        status, _ = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "1.00", "invoice_ids": []},
        )
        self.assertEqual(status, 422)

        # 金额：≤0 / 小于最小单位 / 非十进制
        for bad in ("0", "-1.00", "0.001", "abc"):
            status, _ = self._request(
                "POST",
                "/api/v1/payment-allocations",
                {"currency": "USD", "amount": bad, "invoice_ids": [id_c]},
            )
            self.assertEqual(status, 422, bad)

        # 0 位小数币种：JPY 600 -> 分配 600，余 400，不带小数位
        status, body = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "JPY", "amount": "600", "invoice_ids": [id_j]},
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["amount"], "600")
        self.assertEqual(body["allocations"][0]["allocated"], "600")
        self.assertEqual(body["allocations"][0]["outstanding"], "400")
        # JPY 最小单位是 1，0.5 必须拒绝
        status, _ = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "JPY", "amount": "0.5", "invoice_ids": [id_j]},
        )
        self.assertEqual(status, 422)
        # 剩余 400 恰好收满
        status, body = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "JPY", "amount": "400", "invoice_ids": [id_j]},
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["allocations"][0]["outstanding"], "0")

        # 恰好 60 收满 D+C：两张都清零
        status, body = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "60.00",
             "invoice_ids": [id_c, id_d]},
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(
            [(it["invoice_id"], it["allocated"], it["outstanding"])
             for it in body["allocations"]],
            [(id_d, "27.00", "0.00"), (id_c, "33.00", "0.00")],
        )

    def test_14_allocation_concurrent_no_overpay(self) -> None:
        # gross 100.00：7 个分配请求（列表各只含该发票）+ 3 个单张收款，
        # 每个 20.00。BEGIN IMMEDIATE 串行化 -> 恰好 5 笔成功、5 笔 422，不超收
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD",
             "due_date": "2026-08-01",
             "lines": [{"net": "100.00", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]

        codes: list[int] = []
        lock = threading.Lock()

        def allocate() -> None:
            code, _ = self._request(
                "POST",
                "/api/v1/payment-allocations",
                {"currency": "USD", "amount": "20.00", "invoice_ids": [invoice_id]},
            )
            with lock:
                codes.append(code)

        def single() -> None:
            code, _ = self._request(
                "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "20.00"}
            )
            with lock:
                codes.append(code)

        threads = [threading.Thread(target=allocate) for _ in range(7)]
        threads += [threading.Thread(target=single) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(codes), [201] * 5 + [422] * 5)
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(ov["paid_total"], "100.00")
        self.assertEqual(ov["outstanding"], "0.00")
        self.assertEqual(ov["payment_count"], 5)

    def test_15_payment_reversals(self) -> None:
        # 建一张 USD 发票：total_gross = 33.11，先收款 20.00
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {
                "currency": "USD",
                "lines": [
                    {"net": "10.05", "tax_rate": "0.10"},
                    {"net": "20.05", "tax_rate": "0.10"},
                ],
            },
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]

        status, pay = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "20.00"}
        )
        self.assertEqual(status, 201, pay)
        payment_id = pay["id"]
        # 新收款：未冲销，净入账 = 原金额
        self.assertEqual(pay["reversed_total"], "0.00")
        self.assertEqual(pay["net_amount"], "20.00")

        # 第一次部分冲销 5.00（"5" 规范化为 "5.00"）
        status, rev = self._request(
            "POST", f"/api/v1/payments/{payment_id}/reversals", {"amount": "5"}
        )
        self.assertEqual(status, 201, rev)
        self.assertEqual(rev["payment_id"], payment_id)
        self.assertEqual(rev["invoice_id"], invoice_id)
        self.assertEqual(rev["amount"], "5.00")
        self.assertEqual(rev["currency"], "USD")
        self.assertIn("created_at", rev)
        self.assertEqual(rev["reversed_total"], "5.00")
        self.assertEqual(rev["net_amount"], "15.00")

        # 单张收款查询：流水显示已冲销与净入账，余额按净入账额回升
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(status, 200)
        self.assertEqual(ov["paid_total"], "15.00")
        self.assertEqual(ov["reversed_total"], "5.00")
        self.assertEqual(ov["outstanding"], "18.11")
        self.assertEqual(ov["payment_count"], 1)
        p0 = ov["payments"][0]
        self.assertEqual(p0["amount"], "20.00")
        self.assertEqual(p0["reversed_total"], "5.00")
        self.assertEqual(p0["net_amount"], "15.00")

        # 第二次冲销 15.00：同一笔分次冲销，恰好冲完
        status, rev = self._request(
            "POST", f"/api/v1/payments/{payment_id}/reversals", {"amount": "15.00"}
        )
        self.assertEqual(status, 201, rev)
        self.assertEqual(rev["reversed_total"], "20.00")
        self.assertEqual(rev["net_amount"], "0.00")

        # 已冲完，再冲 0.01 -> 422 超额，且不写入
        status, body = self._request(
            "POST", f"/api/v1/payments/{payment_id}/reversals", {"amount": "0.01"}
        )
        self.assertEqual(status, 422)
        self.assertIn("detail", body)
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(ov["reversed_total"], "20.00")
        self.assertEqual(ov["paid_total"], "0.00")
        self.assertEqual(ov["outstanding"], "33.11")
        # 原收款流水仍在（冲销不删除原收款）
        self.assertEqual(ov["payment_count"], 1)
        self.assertEqual(ov["payments"][0]["amount"], "20.00")
        self.assertEqual(ov["payments"][0]["net_amount"], "0.00")

        # 冲销后余额回升，可按净入账额再次收满
        status, pay2 = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "33.11"}
        )
        self.assertEqual(status, 201, pay2)
        self.assertEqual(pay2["paid_total"], "33.11")
        self.assertEqual(pay2["outstanding"], "0.00")

        # 0 位小数币种：JPY 收款 1000，冲销 600 -> 净入账 400，金额不带小数位
        status, inv_j = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "JPY", "lines": [{"net": "1000", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv_j)
        status, pay_j = self._request(
            "POST", f"/api/v1/invoices/{inv_j['id']}/payments", {"amount": "1000"}
        )
        self.assertEqual(status, 201, pay_j)
        status, rev_j = self._request(
            "POST", f"/api/v1/payments/{pay_j['id']}/reversals", {"amount": "600"}
        )
        self.assertEqual(status, 201, rev_j)
        self.assertEqual(rev_j["amount"], "600")
        self.assertEqual(rev_j["reversed_total"], "600")
        self.assertEqual(rev_j["net_amount"], "400")
        # JPY 最小单位是 1，冲销 0.5 必须拒绝
        status, _ = self._request(
            "POST", f"/api/v1/payments/{pay_j['id']}/reversals", {"amount": "0.5"}
        )
        self.assertEqual(status, 422)

    def test_16_reversal_validation(self) -> None:
        # 未知流水 -> 404
        status, body = self._request(
            "POST", "/api/v1/payments/999999/reversals", {"amount": "1.00"}
        )
        self.assertEqual(status, 404)
        self.assertIn("detail", body)

        # 建发票并收款 10.00
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "10.00", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        status, pay = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "10.00"}
        )
        self.assertEqual(status, 201, pay)
        path = f"/api/v1/payments/{pay['id']}/reversals"

        # 超额（累计冲销 > 原收款 10.00）
        status, _ = self._request("POST", path, {"amount": "10.01"})
        self.assertEqual(status, 422)
        # 零 / 负数
        status, _ = self._request("POST", path, {"amount": "0"})
        self.assertEqual(status, 422)
        status, _ = self._request("POST", path, {"amount": "-5.00"})
        self.assertEqual(status, 422)
        # 非十进制字符串 / 非字符串
        status, _ = self._request("POST", path, {"amount": "abc"})
        self.assertEqual(status, 422)
        status, _ = self._request("POST", path, {"amount": 1.5})
        self.assertEqual(status, 422)
        # 小于币种最小单位
        status, _ = self._request("POST", path, {"amount": "0.001"})
        self.assertEqual(status, 422)

        # 以上全部失败，不得留下任何冲销记录
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(ov["reversed_total"], "0.00")
        self.assertEqual(ov["paid_total"], "10.00")
        self.assertEqual(ov["outstanding"], "0.00")
        self.assertEqual(ov["payments"][0]["reversed_total"], "0.00")
        self.assertEqual(ov["payments"][0]["net_amount"], "10.00")

    def test_17_reversal_affects_overdue_and_allocation(self) -> None:
        # 发票 100.00、已到期：收满后不逾期；冲销 40.00 后重新逾期；
        # 后续来款分配按净入账额再收满
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {
                "currency": "USD",
                "due_date": "2026-09-01",
                "lines": [{"net": "100.00", "tax_rate": "0"}],
            },
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        status, pay = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "100.00"}
        )
        self.assertEqual(status, 201, pay)
        payment_id = pay["id"]

        def overdue_by_id() -> dict:
            status, body = self._request(
                "GET", "/api/v1/invoices/overdue?as_of=2026-09-23&page_size=100"
            )
            self.assertEqual(status, 200)
            return {it["id"]: it for it in body["items"]}

        # 已收满：不逾期
        self.assertNotIn(invoice_id, overdue_by_id())

        # 冲销 40.00 -> 净入账 60.00，未收余额 40.00，重新进入逾期列表
        status, rev = self._request(
            "POST", f"/api/v1/payments/{payment_id}/reversals", {"amount": "40.00"}
        )
        self.assertEqual(status, 201, rev)
        item = overdue_by_id()[invoice_id]
        self.assertEqual(item["paid_total"], "60.00")
        self.assertEqual(item["outstanding"], "40.00")

        # 后续来款分配按净入账额计算：恰好再分配 40.00 收满
        status, body = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "40.00", "invoice_ids": [invoice_id]},
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["allocations"][0]["allocated"], "40.00")
        self.assertEqual(body["allocations"][0]["outstanding"], "0.00")
        self.assertNotIn(invoice_id, overdue_by_id())

        # 余额已按净额算清，多分配 0.01 也整笔拒绝
        status, _ = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "0.01", "invoice_ids": [invoice_id]},
        )
        self.assertEqual(status, 422)

    def test_18_concurrent_reversals_no_overreverse(self) -> None:
        # 原收款 20.00：10 个并发冲销各 3.00，
        # 同事务校验+写入，恰好 6 笔成功（累计 18.00）、4 笔 422，不超原收款
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "20.00", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        status, pay = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "20.00"}
        )
        self.assertEqual(status, 201, pay)
        payment_id = pay["id"]

        codes: list[int] = []
        lock = threading.Lock()

        def reverse() -> None:
            code, _ = self._request(
                "POST", f"/api/v1/payments/{payment_id}/reversals", {"amount": "3.00"}
            )
            with lock:
                codes.append(code)

        threads = [threading.Thread(target=reverse) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(codes), [201] * 6 + [422] * 4)
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(ov["reversed_total"], "18.00")
        self.assertEqual(ov["paid_total"], "2.00")
        self.assertEqual(ov["outstanding"], "18.00")

    def test_19_payment_snapshot(self) -> None:
        import sqlite3
        from decimal import Decimal

        # total_gross = 33.11
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {
                "currency": "USD",
                "lines": [
                    {"net": "10.05", "tax_rate": "0.10"},
                    {"net": "20.05", "tax_rate": "0.10"},
                ],
            },
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]

        # 直接改 SQLite 中流水的秒级时间戳，构造确定性的历史序列
        db_file = os.environ["DB_PATH"]

        def set_time(table: str, row_id: int, ts: str) -> None:
            # 两张流水表均以 id 为主键；row_id 一律传该行自己的 id
            c = sqlite3.connect(db_file, timeout=30)
            try:
                c.execute(
                    f"UPDATE {table} SET created_at = ? WHERE id = ?",
                    (ts, row_id),
                )
                c.commit()
            finally:
                c.close()

        def set_invoice_created(ts: str) -> None:
            c = sqlite3.connect(db_file, timeout=30)
            try:
                c.execute(
                    "UPDATE invoices SET created_at = ? WHERE id = ?",
                    (ts, invoice_id),
                )
                c.commit()
            finally:
                c.close()

        created = "2026-01-01T00:00:00Z"
        set_invoice_created(created)

        t1 = "2026-02-01T10:00:00Z"
        t2 = "2026-02-02T10:00:00Z"
        t3 = "2026-02-03T10:00:00Z"
        t4 = "2026-02-04T10:00:00Z"

        # 收款 20.00（t1）
        status, pay1 = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "20.00"}
        )
        self.assertEqual(status, 201, pay1)
        pid1 = pay1["id"]
        set_time("payments", pid1, t1)
        # 冲销 5.00（t2）
        status, rev1 = self._request(
            "POST", f"/api/v1/payments/{pid1}/reversals", {"amount": "5.00"}
        )
        self.assertEqual(status, 201, rev1)
        rid1 = rev1["id"]
        set_time("payment_reversals", rid1, t2)
        # 再收款 13.11（t3），净额 20-5+13.11 = 28.11，收满在冲销后
        status, pay2 = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "13.11"}
        )
        self.assertEqual(status, 201, pay2)
        pid2 = pay2["id"]
        set_time("payments", pid2, t3)

        def snap(at: str):
            return self._request(
                "GET",
                f"/api/v1/invoices/{invoice_id}/payment-snapshot"
                f"?at={urllib.parse.quote(at, safe='')}",
            )

        # 时点 == 发票创建：无任何流水
        status, s = snap(created)
        self.assertEqual(status, 200, s)
        self.assertEqual(s["at"], created)
        self.assertEqual(s["invoice_created_at"], created)
        self.assertEqual(s["total_gross"], "33.11")
        self.assertEqual(s["received_total"], "0.00")
        self.assertEqual(s["reversed_total"], "0.00")
        self.assertEqual(s["net_paid"], "0.00")
        self.assertEqual(s["outstanding"], "33.11")
        self.assertEqual(s["payment_count"], 0)
        self.assertEqual(s["reversal_count"], 0)
        self.assertEqual(s["payments"], [])
        self.assertEqual(s["reversals"], [])

        # 早于发票创建 -> 422
        status, body = snap("2025-12-31T23:59:59Z")
        self.assertEqual(status, 422)
        self.assertIn("detail", body)

        # t1 之前一秒：无流水
        status, s = snap("2026-02-01T09:59:59Z")
        self.assertEqual(status, 200)
        self.assertEqual(s["received_total"], "0.00")
        self.assertEqual(s["outstanding"], "33.11")

        # t1 当秒（闭区间）：收款 20 计入，冲销不计入
        status, s = snap(t1)
        self.assertEqual(status, 200, s)
        self.assertEqual(s["received_total"], "20.00")
        self.assertEqual(s["reversed_total"], "0.00")
        self.assertEqual(s["net_paid"], "20.00")
        self.assertEqual(s["outstanding"], "13.11")
        self.assertEqual(s["payment_count"], 1)
        self.assertEqual(s["reversal_count"], 0)
        self.assertEqual(
            [(p["id"], p["reversed_total"], p["net_amount"]) for p in s["payments"]],
            [(pid1, "0.00", "20.00")],
        )

        # t2 当秒：冲销 5 计入（同秒闭区间）
        status, s = snap(t2)
        self.assertEqual(status, 200, s)
        self.assertEqual(s["received_total"], "20.00")
        self.assertEqual(s["reversed_total"], "5.00")
        self.assertEqual(s["net_paid"], "15.00")
        self.assertEqual(s["outstanding"], "18.11")
        self.assertEqual(s["payment_count"], 1)
        self.assertEqual(s["reversal_count"], 1)
        self.assertEqual(
            [(r["id"], r["payment_id"], r["amount"]) for r in s["reversals"]],
            [(rid1, pid1, "5.00")],
        )
        p0 = s["payments"][0]
        self.assertEqual(p0["reversed_total"], "5.00")
        self.assertEqual(p0["net_amount"], "15.00")

        # t3 当秒：第二笔收款计入
        status, s = snap(t3)
        self.assertEqual(status, 200, s)
        self.assertEqual(s["received_total"], "33.11")
        self.assertEqual(s["reversed_total"], "5.00")
        self.assertEqual(s["net_paid"], "28.11")
        self.assertEqual(s["outstanding"], "5.00")
        self.assertEqual(s["payment_count"], 2)
        self.assertEqual([p["id"] for p in s["payments"]], [pid1, pid2])

        # ---- 截止后的流水不得改变历史结果 ----
        # 对 pid2 冲销 13.11（t4），随后再查 t3 的快照必须与之前完全一致
        status, rev2 = self._request(
            "POST", f"/api/v1/payments/{pid2}/reversals", {"amount": "13.11"}
        )
        self.assertEqual(status, 201, rev2)
        set_time("payment_reversals", rev2["id"], t4)

        status, s = snap(t3)
        self.assertEqual(status, 200, s)
        self.assertEqual(s["received_total"], "33.11")
        self.assertEqual(s["reversed_total"], "5.00")
        self.assertEqual(s["net_paid"], "28.11")
        self.assertEqual(s["outstanding"], "5.00")
        self.assertEqual(s["reversal_count"], 1)
        self.assertEqual(
            [p["net_amount"] for p in s["payments"]], ["15.00", "13.11"]
        )

        # t4 当秒：第二笔冲销计入，净已收 15.00，未收回到 18.11
        status, s = snap(t4)
        self.assertEqual(status, 200, s)
        self.assertEqual(s["received_total"], "33.11")
        self.assertEqual(s["reversed_total"], "18.11")
        self.assertEqual(s["net_paid"], "15.00")
        self.assertEqual(s["outstanding"], "18.11")
        self.assertEqual(s["reversal_count"], 2)

        # 未来时点：与当前全量一致
        status, s_now = snap("2030-01-01T00:00:00Z")
        self.assertEqual(status, 200, s_now)
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(status, 200)
        self.assertEqual(s_now["received_total"], "33.11")
        self.assertEqual(s_now["reversed_total"], ov["reversed_total"])
        self.assertEqual(s_now["net_paid"], ov["paid_total"])
        self.assertEqual(s_now["outstanding"], ov["outstanding"])
        self.assertEqual(s_now["payment_count"], ov["payment_count"])

        # 必填参数缺失 -> 422
        status, _ = self._request(
            "GET", f"/api/v1/invoices/{invoice_id}/payment-snapshot"
        )
        self.assertEqual(status, 422)

        # 时间格式错误 -> 422
        for bad in (
            "2026-02-01T10:00:00+00:00",  # 偏移而非 Z
            "2026-02-01 10:00:00",        # 空格分隔
            "2026-02-01T10:00:00.000Z",   # 毫秒
            "2026-02-01T10:00Z",          # 缺秒
            "2026-02-01",                 # 仅日期
            "2026-13-01T10:00:00Z",       # 月份不存在
            "2026-02-30T10:00:00Z",       # 日期不存在
            "not-a-time",
        ):
            status, resp = snap(bad)
            self.assertEqual(status, 422, bad)
            self.assertIn("detail", resp)

        # 发票不存在 -> 404（即使时间合法/非法均按 404 处理）
        status, body = self._request(
            "GET", "/api/v1/invoices/999999/payment-snapshot?at=2030-01-01T00:00:00Z"
        )
        self.assertEqual(status, 404)
        self.assertIn("detail", body)

    def test_20_payment_snapshot_jpy(self) -> None:
        # 0 位小数币种：快照金额不带小数位
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "JPY", "lines": [{"net": "1000", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        created = inv["created_at"]

        status, pay = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "1000"}
        )
        self.assertEqual(status, 201, pay)
        status, rev = self._request(
            "POST", f"/api/v1/payments/{pay['id']}/reversals", {"amount": "600"}
        )
        self.assertEqual(status, 201, rev)

        status, s = self._request(
            "GET",
            f"/api/v1/invoices/{invoice_id}/payment-snapshot?at=2030-01-01T00:00:00Z",
        )
        self.assertEqual(status, 200, s)
        self.assertEqual(s["currency"], "JPY")
        self.assertEqual(s["total_gross"], "1000")
        self.assertEqual(s["received_total"], "1000")
        self.assertEqual(s["reversed_total"], "600")
        self.assertEqual(s["net_paid"], "400")
        self.assertEqual(s["outstanding"], "600")
        # 快照时点不得早于发票创建
        self.assertEqual(s["invoice_created_at"], created)

    def test_21_installment_plan_create_and_get(self) -> None:
        # total_gross = 33.11 USD，3 期：10.00 / 13.11 / 10.00
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {
                "currency": "USD",
                "lines": [
                    {"net": "10.05", "tax_rate": "0.10"},
                    {"net": "20.05", "tax_rate": "0.10"},
                ],
            },
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        path = f"/api/v1/invoices/{invoice_id}/installment-plan"
        payload = {
            "installments": [
                {"due_date": "2026-08-01", "amount": "10.00"},
                {"due_date": "2026-09-01", "amount": "13.11"},
                {"due_date": "2026-10-15", "amount": "10.00"},
            ]
        }

        status, plan = self._request("POST", path, payload)
        self.assertEqual(status, 201, plan)
        self.assertEqual(plan["invoice_id"], invoice_id)
        self.assertIn("plan_id", plan)
        self.assertIn("created_at", plan)
        self.assertEqual(plan["as_of"], "2026-09-24")
        self.assertEqual(plan["currency"], "USD")
        self.assertEqual(plan["total_gross"], "33.11")
        self.assertEqual(plan["net_paid"], "0.00")
        self.assertEqual(plan["covered_total"], "0.00")
        self.assertEqual(plan["uncovered_total"], "33.11")
        # 创建时 as_of=2026-09-24：前两期已到期
        self.assertEqual(plan["overdue_total"], "23.11")
        self.assertEqual(
            [(it["installment"], it["due_date"], it["amount"],
              it["covered"], it["uncovered"], it["overdue"])
             for it in plan["installments"]],
            [
                (1, "2026-08-01", "10.00", "0.00", "10.00", "10.00"),
                (2, "2026-09-01", "13.11", "0.00", "13.11", "13.11"),
                (3, "2026-10-15", "10.00", "0.00", "10.00", "0.00"),
            ],
        )

        # GET 与 POST 返回同构视图
        status, fetched = self._request("GET", f"{path}?as_of=2026-09-24")
        self.assertEqual(status, 200, fetched)
        self.assertEqual(fetched, plan)

        # 到期日当天不算逾期：as_of=2026-08-01 时第 1 期 overdue=0
        status, at_due = self._request("GET", f"{path}?as_of=2026-08-01")
        self.assertEqual(status, 200)
        self.assertEqual(at_due["overdue_total"], "0.00")
        self.assertEqual(at_due["installments"][0]["overdue"], "0.00")

        # 一天之后：第 1 期全额逾期
        status, after_due = self._request("GET", f"{path}?as_of=2026-08-02")
        self.assertEqual(status, 200)
        self.assertEqual(after_due["overdue_total"], "10.00")

        # 全部到期后：所有未覆盖额都逾期
        status, late = self._request("GET", f"{path}?as_of=2030-01-01")
        self.assertEqual(late["overdue_total"], "33.11")

        # 期次合计 + 覆盖汇总恒等
        from decimal import Decimal
        for body in (at_due, after_due, late):
            self.assertEqual(
                sum(Decimal(it["amount"]) for it in body["installments"]),
                Decimal("33.11"),
            )
            self.assertEqual(
                Decimal(body["covered_total"]) + Decimal(body["uncovered_total"]),
                Decimal(body["total_gross"]),
            )

        # 缺省 as_of：回显 UTC 当日
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        status, default_view = self._request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(default_view["as_of"], today)

    def test_22_installment_plan_fifo_coverage(self) -> None:
        # 3 期 10.00 / 13.11 / 10.00 = 33.11；收款逐期 FIFO 抵扣
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {
                "currency": "USD",
                "lines": [
                    {"net": "10.05", "tax_rate": "0.10"},
                    {"net": "20.05", "tax_rate": "0.10"},
                ],
            },
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        path = f"/api/v1/invoices/{invoice_id}/installment-plan"
        status, _ = self._request(
            "POST",
            path,
            {
                "installments": [
                    {"due_date": "2026-08-01", "amount": "10.00"},
                    {"due_date": "2026-09-01", "amount": "13.11"},
                    {"due_date": "2026-10-15", "amount": "10.00"},
                ]
            },
        )
        self.assertEqual(status, 201)

        def view(as_of: str = "2026-09-24"):
            status, body = self._request("GET", f"{path}?as_of={as_of}")
            self.assertEqual(status, 200, body)
            return body

        # 收款 20.00：第 1 期覆盖满，第 2 期覆盖 10.00/未覆盖 3.11
        status, _ = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "20.00"}
        )
        self.assertEqual(status, 201)
        v = view()
        self.assertEqual(v["net_paid"], "20.00")
        self.assertEqual(v["covered_total"], "20.00")
        self.assertEqual(v["uncovered_total"], "13.11")
        # 仅第 2 期未覆盖的 3.11 逾期（第 1 期已覆盖，第 3 期未到期）
        self.assertEqual(v["overdue_total"], "3.11")
        self.assertEqual(
            [(it["covered"], it["uncovered"], it["overdue"])
             for it in v["installments"]],
            [("10.00", "0.00", "0.00"),
             ("10.00", "3.11", "3.11"),
             ("0.00", "10.00", "0.00")],
        )

        # 再收 13.11 恰好收满：全部覆盖，逾期归零
        status, _ = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "13.11"}
        )
        self.assertEqual(status, 201)
        v = view("2030-01-01")
        self.assertEqual(v["net_paid"], "33.11")
        self.assertEqual(v["covered_total"], "33.11")
        self.assertEqual(v["uncovered_total"], "0.00")
        self.assertEqual(v["overdue_total"], "0.00")
        self.assertTrue(all(it["covered"] == it["amount"] for it in v["installments"]))

    def test_23_installment_plan_reversal_reopens(self) -> None:
        # 收满后冲销：净已收回降，后期次重新打开；原计划期次/金额不变
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "100.00", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        path = f"/api/v1/invoices/{invoice_id}/installment-plan"
        status, created = self._request(
            "POST",
            path,
            {
                "installments": [
                    {"due_date": "2026-08-01", "amount": "40.00"},
                    {"due_date": "2026-09-01", "amount": "60.00"},
                ]
            },
        )
        self.assertEqual(status, 201, created)

        # 来款分配也走 payments 表，计划抵扣必须可见
        status, alloc = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "100.00", "invoice_ids": [invoice_id]},
        )
        self.assertEqual(status, 201, alloc)
        payment_id = alloc["allocations"][0]["payment_id"]

        status, full = self._request("GET", f"{path}?as_of=2026-09-24")
        self.assertEqual(status, 200)
        self.assertEqual(full["covered_total"], "100.00")
        self.assertEqual(full["uncovered_total"], "0.00")
        self.assertEqual(full["overdue_total"], "0.00")

        # 冲销 70.00：净已收 30.00 -> 第 1 期覆盖 30/未覆盖 10，第 2 期完全重新打开
        status, rev = self._request(
            "POST",
            f"/api/v1/payments/{payment_id}/reversals",
            {"amount": "70.00"},
        )
        self.assertEqual(status, 201, rev)
        status, reopened = self._request("GET", f"{path}?as_of=2026-09-24")
        self.assertEqual(status, 200, reopened)
        self.assertEqual(reopened["net_paid"], "30.00")
        self.assertEqual(reopened["covered_total"], "30.00")
        self.assertEqual(reopened["uncovered_total"], "70.00")
        self.assertEqual(reopened["overdue_total"], "70.00")
        self.assertEqual(
            [(it["installment"], it["amount"], it["covered"], it["uncovered"])
             for it in reopened["installments"]],
            [(1, "40.00", "30.00", "10.00"), (2, "60.00", "0.00", "60.00")],
        )
        # 原计划行未被改写：期次号、到期日、金额与创建时一致
        self.assertEqual(
            [(it["installment"], it["due_date"], it["amount"])
             for it in reopened["installments"]],
            [(it["installment"], it["due_date"], it["amount"])
             for it in created["installments"]],
        )

    def test_24_installment_plan_validation(self) -> None:
        # gross 10.00 USD
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "10.00", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        path = f"/api/v1/invoices/{invoice_id}/installment-plan"

        def post(installments):
            return self._request("POST", path, {"installments": installments})

        # 期次合计不等于含税总额 -> 422
        status, body = post([{"due_date": "2026-08-01", "amount": "9.00"}])
        self.assertEqual(status, 422, body)
        self.assertIn("含税总额", body["detail"])

        # 合计相等但存在 0 / 负数金额 -> 422
        for bad in ("0", "-1.00"):
            status, _ = post(
                [{"due_date": "2026-08-01", "amount": bad},
                 {"due_date": "2026-09-01", "amount": "10.00"}]
            )
            self.assertEqual(status, 422, bad)

        # 小于币种最小单位 -> 422
        status, _ = post(
            [{"due_date": "2026-08-01", "amount": "9.999"},
             {"due_date": "2026-09-01", "amount": "0.001"}]
        )
        self.assertEqual(status, 422)

        # 到期日未按升序排列 -> 422（pydantic 校验，detail 为错误列表）
        status, body = post(
            [{"due_date": "2026-09-01", "amount": "5.00"},
             {"due_date": "2026-08-01", "amount": "5.00"}]
        )
        self.assertEqual(status, 422)
        self.assertIn("升序", json.dumps(body, ensure_ascii=False))

        # 期次为空 -> 422
        status, _ = post([])
        self.assertEqual(status, 422)

        # 到期日格式非法（带时间 / 不存在的日期）-> 422
        for bad in ("2026-08", "2026-08-01T00:00:00Z", "2026/08/01", "not-a-date"):
            status, _ = post([{"due_date": bad, "amount": "10.00"}])
            self.assertEqual(status, 422, bad)

        # 所有失败都不得创建计划
        status, _ = self._request("GET", path)
        self.assertEqual(status, 404)

        # 成功创建一份
        status, plan = post(
            [{"due_date": "2026-08-01", "amount": "4.00"},
             {"due_date": "2026-08-01", "amount": "6.00"}]  # 同日多期允许
        )
        self.assertEqual(status, 201, plan)

        # 每张发票只允许一份：再次创建 -> 409
        status, body = post([{"due_date": "2026-12-01", "amount": "10.00"}])
        self.assertEqual(status, 409)
        self.assertIn("只允许一份", body["detail"])

        # 发票不存在：创建 404、查询 404
        status, _ = self._request(
            "POST",
            "/api/v1/invoices/999999/installment-plan",
            {"installments": [{"due_date": "2026-08-01", "amount": "1.00"}]},
        )
        self.assertEqual(status, 404)
        status, body = self._request(
            "GET", "/api/v1/invoices/999999/installment-plan"
        )
        self.assertEqual(status, 404)
        self.assertIn("不存在", body["detail"])

        # 查询时 as_of 非法 -> 422
        status, _ = self._request("GET", f"{path}?as_of=2026-13-40")
        self.assertEqual(status, 422)

    def test_25_installment_plan_jpy_and_concurrent(self) -> None:
        # 0 位小数币种：金额不带小数位，非整数最小单位（0.5）被拒绝
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "JPY", "lines": [{"net": "1000", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        jpy_id = inv["id"]
        path = f"/api/v1/invoices/{jpy_id}/installment-plan"
        status, body = self._request(
            "POST",
            path,
            {
                "installments": [
                    {"due_date": "2026-08-01", "amount": "400"},
                    {"due_date": "2026-09-01", "amount": "600"},
                ]
            },
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["total_gross"], "1000")
        self.assertEqual(body["uncovered_total"], "1000")
        self.assertEqual(body["installments"][0]["amount"], "400")

        status, _ = self._request(
            "POST", f"/api/v1/invoices/{jpy_id}/payments", {"amount": "500"}
        )
        self.assertEqual(status, 201)
        status, v = self._request("GET", f"{path}?as_of=2026-09-24")
        self.assertEqual(status, 200)
        self.assertEqual(v["net_paid"], "500")
        self.assertEqual(
            [(it["covered"], it["uncovered"], it["overdue"])
             for it in v["installments"]],
            [("400", "0", "0"), ("100", "500", "500")],
        )

        # 并发为同一张发票创建计划：唯一约束 + BEGIN IMMEDIATE，只允许一份
        status, inv2 = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "10.00", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv2)
        target = inv2["id"]
        target_path = f"/api/v1/invoices/{target}/installment-plan"
        payload = {"installments": [{"due_date": "2026-08-01", "amount": "10.00"}]}

        codes: list[int] = []
        lock = threading.Lock()

        def create() -> None:
            code, _ = self._request("POST", target_path, payload)
            with lock:
                codes.append(code)

        threads = [threading.Thread(target=create) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(codes), [201] + [409] * 7)

    def test_26_invoice_number_check_duplicates(self) -> None:
        payload = {
            "items": [
                {"supplier_code": "SUP-A", "invoice_number": "INV-001"},
                {"supplier_code": "sup-a", "invoice_number": " inv-001 "},
                {"supplier_code": "SUP-A", "invoice_number": "INV-002"},
                {"supplier_code": "SUP-B", "invoice_number": "INV-001"},
            ]
        }
        status, body = self._request("POST", "/api/v1/invoice-number-check", payload)
        self.assertEqual(status, 200, body)
        self.assertEqual(
            body,
            {
                "items": [
                    {
                        "supplier_code": "SUP-A",
                        "normalized_invoice_number": "inv-001",
                        "positions": [1, 2],
                        "original_invoice_numbers": ["INV-001", " inv-001 "],
                    }
                ]
            },
        )

    def test_27_invoice_number_check_normalization(self) -> None:
        # NFKC（全角）+ 大小写折叠（Kelvin 符号 K → k；ß casefold → ss）
        payload = {
            "items": [
                {"supplier_code": "ＳＵＰ－１", "invoice_number": "ＡＢＣ１２３"},
                {"supplier_code": "SUP-1", "invoice_number": "abc123"},
                {"supplier_code": "x", "invoice_number": "STRAßE"},
                {"supplier_code": "x", "invoice_number": "strasse"},
            ]
        }
        status, body = self._request("POST", "/api/v1/invoice-number-check", payload)
        self.assertEqual(status, 200, body)
        self.assertEqual(
            [(g["normalized_invoice_number"], g["positions"]) for g in body["items"]],
            [("abc123", [1, 2]), ("strasse", [3, 4])],
        )

    def test_28_invoice_number_check_group_ordering(self) -> None:
        # 重复组按组内最早输入位置升序；同号码跨供应商不算重复
        payload = {
            "items": [
                {"supplier_code": "a", "invoice_number": "z"},
                {"supplier_code": "b", "invoice_number": "m"},
                {"supplier_code": "a", "invoice_number": "z"},
                {"supplier_code": "b", "invoice_number": "n"},
                {"supplier_code": "b", "invoice_number": "m"},
                {"supplier_code": "a", "invoice_number": "x"},
                {"supplier_code": "a", "invoice_number": "x"},
            ]
        }
        status, body = self._request("POST", "/api/v1/invoice-number-check", payload)
        self.assertEqual(status, 200, body)
        self.assertEqual(
            [(g["supplier_code"], g["positions"]) for g in body["items"]],
            [("a", [1, 3]), ("b", [2, 5]), ("a", [6, 7])],
        )
        # supplier_code 取首次出现位置的原始值
        self.assertEqual(
            [g["original_invoice_numbers"] for g in body["items"]],
            [["z", "z"], ["m", "m"], ["x", "x"]],
        )

    def test_29_invoice_number_check_no_duplicates(self) -> None:
        payload = {
            "items": [
                {"supplier_code": "a", "invoice_number": "1"},
                {"supplier_code": "b", "invoice_number": "1"},
                {"supplier_code": "a", "invoice_number": "2"},
            ]
        }
        status, body = self._request("POST", "/api/v1/invoice-number-check", payload)
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"items": []})

    def test_30_invoice_number_check_validation(self) -> None:
        path = "/api/v1/invoice-number-check"

        # 空批次
        status, body = self._request("POST", path, {"items": []})
        self.assertEqual(status, 422, body)

        # 超过 500 条
        big = {
            "items": [
                {"supplier_code": "s", "invoice_number": f"n{i}"} for i in range(501)
            ]
        }
        status, body = self._request("POST", path, big)
        self.assertEqual(status, 422, body)

        # 原始发票号码为空 / 纯空白（NFKC 后为空）
        for bad_number in ("", "   ", "\t\n", "\u3000"):  # 全角空格 NFKC 后为空
            status, body = self._request(
                "POST",
                path,
                {"items": [{"supplier_code": "s", "invoice_number": bad_number}]},
            )
            self.assertEqual(status, 422, (bad_number, body))

        # 供应商编号为空
        status, body = self._request(
            "POST",
            path,
            {"items": [{"supplier_code": "  ", "invoice_number": "n"}]},
        )
        self.assertEqual(status, 422, body)

        # 原始号码超过 100 个 Unicode 字符（每个 emoji 算 1 个字符）
        status, body = self._request(
            "POST",
            path,
            {"items": [{"supplier_code": "s", "invoice_number": "😀" * 101}]},
        )
        self.assertEqual(status, 422, body)

        # 恰好 100 个字符合法
        status, body = self._request(
            "POST",
            path,
            {"items": [{"supplier_code": "s", "invoice_number": "a" * 100}]},
        )
        self.assertEqual(status, 200, body)

        # 缺字段
        status, body = self._request(
            "POST", path, {"items": [{"supplier_code": "s"}]}
        )
        self.assertEqual(status, 422, body)

    def test_31_credit_memo_create_and_list(self) -> None:
        # USD 发票两行 13%：l1 net 10.00 -> tax 1.30；l2 net 3.34 -> tax 0.43
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [
                {"net": "10.00", "tax_rate": "0.13"},
                {"net": "3.34", "tax_rate": "0.13"},
            ]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        self.assertEqual(
            [(l["line_no"], l["net"], l["tax"]) for l in inv["lines"]],
            [(1, "10.00", "1.30"), (2, "3.34", "0.43")],
        )
        path = f"/api/v1/invoices/{invoice_id}/credit-memos"

        # 初始查询：无凭证
        status, ov = self._request("GET", path)
        self.assertEqual(status, 200, ov)
        self.assertEqual(ov["invoice_id"], invoice_id)
        self.assertEqual(ov["currency"], "USD")
        self.assertEqual(ov["total_net"], "13.34")
        self.assertEqual(ov["total_tax"], "1.73")
        self.assertEqual(ov["total_gross"], "15.07")
        self.assertEqual(ov["credit_count"], 0)
        self.assertEqual(ov["credited_net"], "0.00")
        self.assertEqual(ov["credited_tax"], "0.00")
        self.assertEqual(ov["credited_gross"], "0.00")
        self.assertEqual(ov["line_credits"], [])
        self.assertEqual(ov["memos"], [])

        # 一张凭证含两行（故意把行号倒序提交，凭证行保持提交顺序）
        status, memo = self._request(
            "POST", path, {"lines": [
                {"line_no": 2, "net": "1.00"},
                {"line_no": 1, "net": "3.34"},
            ]},
        )
        self.assertEqual(status, 201, memo)
        self.assertEqual(memo["invoice_id"], invoice_id)
        self.assertEqual(memo["currency"], "USD")
        self.assertIn("id", memo)
        self.assertIn("created_at", memo)
        # l2: round(0.43*1/3.34)=round(0.1287...)=0.13；l1: round(1.30*3.34/10)=0.43
        self.assertEqual(
            memo["lines"],
            [
                {"line_no": 2, "net": "1.00", "tax": "0.13", "gross": "1.13",
                 "cumulative_net": "1.00", "cumulative_tax": "0.13"},
                {"line_no": 1, "net": "3.34", "tax": "0.43", "gross": "3.77",
                 "cumulative_net": "3.34", "cumulative_tax": "0.43"},
            ],
        )
        self.assertEqual(memo["net_total"], "4.34")
        self.assertEqual(memo["tax_total"], "0.56")
        self.assertEqual(memo["gross_total"], "4.90")

        # 查询接口：凭证按 id 升序，凭证行按行号升序；含逐原行累计
        status, ov = self._request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(ov["credit_count"], 1)
        self.assertEqual(ov["credited_net"], "4.34")
        self.assertEqual(ov["credited_tax"], "0.56")
        self.assertEqual(ov["credited_gross"], "4.90")
        self.assertEqual(
            ov["line_credits"],
            [
                {"line_no": 1, "credited_net": "3.34", "credited_tax": "0.43",
                 "credited_gross": "3.77", "remaining_net": "6.66"},
                {"line_no": 2, "credited_net": "1.00", "credited_tax": "0.13",
                 "credited_gross": "1.13", "remaining_net": "2.34"},
            ],
        )
        self.assertEqual([l["line_no"] for l in ov["memos"][0]["lines"]], [1, 2])
        self.assertEqual(ov["memos"][0]["id"], memo["id"])

        # 原发票保持原样：逐行净额/税额、汇总与创建时完全一致
        status, fetched = self._request("GET", f"/api/v1/invoices/{invoice_id}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["totals"], inv["totals"])
        self.assertEqual(
            [(l["line_no"], l["net"], l["tax"], l["gross"]) for l in fetched["lines"]],
            [(l["line_no"], l["net"], l["tax"], l["gross"]) for l in inv["lines"]],
        )
        self.assertTrue(fetched["recompute_matched"])

        # 收款流水保持原样：未收余额仍等于含税总额，收款查询看不到任何凭证影响
        status, payov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(status, 200)
        self.assertEqual(payov["total_gross"], "15.07")
        self.assertEqual(payov["paid_total"], "0.00")
        self.assertEqual(payov["outstanding"], "15.07")
        self.assertEqual(payov["payment_count"], 0)

        # 登记一笔收款后再抵扣：凭证不改变未收余额
        status, pay = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "5.00"}
        )
        self.assertEqual(status, 201, pay)
        self.assertEqual(pay["outstanding"], "10.07")
        status, ov = self._request("GET", path)
        self.assertEqual(ov["credit_count"], 1)
        status, payov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(payov["outstanding"], "10.07")

    def test_32_credit_memo_cumulative_and_full_offset(self) -> None:
        # l1 net 10.00 @13% -> tax 1.30，分三次抵：3.34 / 3.33 / 3.33
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "10.00", "tax_rate": "0.13"}]},
        )
        self.assertEqual(status, 201, inv)
        path = f"/api/v1/invoices/{inv['id']}/credit-memos"
        from decimal import Decimal

        status, m1 = self._request("POST", path, {"lines": [{"line_no": 1, "net": "3.34"}]})
        self.assertEqual(status, 201, m1)
        # round(1.30 * 3.34 / 10) = round(0.4342) = 0.43
        self.assertEqual(m1["lines"][0]["tax"], "0.43")
        self.assertEqual(m1["lines"][0]["cumulative_net"], "3.34")
        self.assertEqual(m1["lines"][0]["cumulative_tax"], "0.43")

        status, m2 = self._request("POST", path, {"lines": [{"line_no": 1, "net": "3.33"}]})
        self.assertEqual(status, 201, m2)
        # 累计 6.67：round(1.30*0.667)=round(0.8671)=0.87；本次 = 0.87-0.43 = 0.44
        self.assertEqual(m2["lines"][0]["tax"], "0.44")
        self.assertEqual(m2["lines"][0]["cumulative_net"], "6.67")
        self.assertEqual(m2["lines"][0]["cumulative_tax"], "0.87")

        # 最后一笔恰好抵完净额 10.00：累计税额必须恰好等于原行税额 1.30
        status, m3 = self._request("POST", path, {"lines": [{"line_no": 1, "net": "3.33"}]})
        self.assertEqual(status, 201, m3)
        self.assertEqual(m3["lines"][0]["net"], "3.33")
        self.assertEqual(m3["lines"][0]["cumulative_net"], "10.00")
        self.assertEqual(m3["lines"][0]["cumulative_tax"], "1.30")
        self.assertEqual(m3["lines"][0]["tax"], "0.43")
        self.assertEqual(m3["lines"][0]["gross"], "3.76")
        # 三行税额合计恰好抵完原行已确认税额
        self.assertEqual(
            sum(Decimal(m["lines"][0]["tax"]) for m in (m1, m2, m3)),
            Decimal("1.30"),
        )

        # 抵完后逐原行剩余净额为 0
        status, ov = self._request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(ov["credit_count"], 3)
        self.assertEqual(ov["line_credits"][0]["remaining_net"], "0.00")
        self.assertEqual(ov["line_credits"][0]["credited_tax"], "1.30")

        # 再多抵 0.01 -> 422，且不新增凭证
        status, body = self._request(
            "POST", path, {"lines": [{"line_no": 1, "net": "0.01"}]}
        )
        self.assertEqual(status, 422)
        self.assertIn("超过原行净额", body["detail"])
        status, ov = self._request("GET", path)
        self.assertEqual(ov["credit_count"], 3)

    def test_33_credit_memo_validation(self) -> None:
        # 两行发票：l1 正净额 10.00；l2 负净额 -5.00（冲红行，不可抵扣）
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [
                {"net": "10.00", "tax_rate": "0.13"},
                {"net": "-5.00", "tax_rate": "0.13"},
            ]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        path = f"/api/v1/invoices/{invoice_id}/credit-memos"

        def post(lines):
            return self._request("POST", path, {"lines": lines})

        # 引用不存在的原行
        status, body = post([{"line_no": 9, "net": "1.00"}])
        self.assertEqual(status, 422, body)
        self.assertIn("不存在行号 9", body["detail"])

        # 原行净额不为正（负净额冲红行）
        status, body = post([{"line_no": 2, "net": "0.01"}])
        self.assertEqual(status, 422, body)
        self.assertIn("不为正", body["detail"])

        # 本次抵扣净额 0 / 负数 / 非最小单位
        for bad in ("0", "0.000", "-1.00", "0.001"):
            status, _ = post([{"line_no": 1, "net": bad}])
            self.assertEqual(status, 422, bad)

        # 行号非正、非整数 -> 422（pydantic）
        for bad_no in (0, -1, 1.5):
            status, _ = post([{"line_no": bad_no, "net": "1.00"}])
            self.assertEqual(status, 422, bad_no)

        # 同一凭证内重复行号 -> 422
        status, body = post([
            {"line_no": 1, "net": "1.00"},
            {"line_no": 1, "net": "2.00"},
        ])
        self.assertEqual(status, 422, body)
        self.assertIn("重复", json.dumps(body, ensure_ascii=False))

        # 空明细 -> 422
        status, _ = post([])
        self.assertEqual(status, 422)

        # 累计超额：先抵 9.00，再抵 1.01 -> 422 且不写入第二张
        status, m = post([{"line_no": 1, "net": "9.00"}])
        self.assertEqual(status, 201, m)
        status, body = post([{"line_no": 1, "net": "1.01"}])
        self.assertEqual(status, 422)
        self.assertIn("超过原行净额", body["detail"])
        status, ov = self._request("GET", path)
        self.assertEqual(ov["credit_count"], 1)
        self.assertEqual(ov["credited_net"], "9.00")

        # 全部失败后原发票与分页列表不受影响（发票仍在、逐行金额不变）
        status, fetched = self._request("GET", f"/api/v1/invoices/{invoice_id}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["totals"], inv["totals"])

        # 发票不存在：创建 404、查询 404
        status, body = self._request(
            "POST",
            "/api/v1/invoices/999999/credit-memos",
            {"lines": [{"line_no": 1, "net": "1.00"}]},
        )
        self.assertEqual(status, 404)
        self.assertIn("不存在", body["detail"])
        status, body = self._request("GET", "/api/v1/invoices/999999/credit-memos")
        self.assertEqual(status, 404)

    def test_34_credit_memo_jpy_rounding(self) -> None:
        # JPY 0 位小数：net 1000 @10% -> tax 100；多次抵扣验证 HALF_UP 与完全抵完
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "JPY", "lines": [{"net": "1000", "tax_rate": "0.10"}]},
        )
        self.assertEqual(status, 201, inv)
        self.assertEqual(inv["lines"][0]["tax"], "100")
        path = f"/api/v1/invoices/{inv['id']}/credit-memos"

        status, m1 = self._request("POST", path, {"lines": [{"line_no": 1, "net": "333"}]})
        self.assertEqual(status, 201, m1)
        # round(100*333/1000)=round(33.3)=33
        self.assertEqual(m1["lines"][0]["tax"], "33")
        self.assertEqual(m1["lines"][0]["gross"], "366")
        self.assertEqual(m1["lines"][0]["cumulative_tax"], "33")

        status, m2 = self._request("POST", path, {"lines": [{"line_no": 1, "net": "333"}]})
        self.assertEqual(status, 201, m2)
        # 累计 666：round(66.6)=67；本次 = 67-33 = 34
        self.assertEqual(m2["lines"][0]["tax"], "34")
        self.assertEqual(m2["lines"][0]["cumulative_tax"], "67")

        # 完全抵扣：累计税额恰好 100，本次 33（67+33=100）
        status, m3 = self._request("POST", path, {"lines": [{"line_no": 1, "net": "334"}]})
        self.assertEqual(status, 201, m3)
        self.assertEqual(m3["lines"][0]["cumulative_net"], "1000")
        self.assertEqual(m3["lines"][0]["cumulative_tax"], "100")
        self.assertEqual(m3["lines"][0]["tax"], "33")

        # 0.5 不是 JPY 最小单位整数倍 -> 422
        status, body = self._request(
            "POST", path, {"lines": [{"line_no": 1, "net": "0.5"}]}
        )
        self.assertEqual(status, 422)
        self.assertIn("最小单位", body["detail"])

        # 汇总金额不带小数位
        status, ov = self._request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(ov["credited_net"], "1000")
        self.assertEqual(ov["credited_tax"], "100")
        self.assertEqual(ov["credited_gross"], "1100")
        self.assertEqual(ov["line_credits"][0]["remaining_net"], "0")

    def test_35_credit_memo_concurrent(self) -> None:
        # 原行净额 10.00：15 个并发抵扣各 1.00，BEGIN IMMEDIATE 串行化：
        # 恰好 10 笔成功、5 笔 422，累计抵扣净额 10.00、累计税额恰好 1.30
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "10.00", "tax_rate": "0.13"}]},
        )
        self.assertEqual(status, 201, inv)
        path = f"/api/v1/invoices/{inv['id']}/credit-memos"

        codes: list[int] = []
        taxes: list[str] = []
        lock = threading.Lock()

        def credit() -> None:
            code, body = self._request(
                "POST", path, {"lines": [{"line_no": 1, "net": "1.00"}]}
            )
            with lock:
                codes.append(code)
                if code == 201:
                    taxes.append(body["lines"][0]["tax"])

        from decimal import Decimal

        threads = [threading.Thread(target=credit) for _ in range(15)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(codes), [201] * 10 + [422] * 5)
        status, ov = self._request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(ov["credit_count"], 10)
        self.assertEqual(ov["credited_net"], "10.00")
        self.assertEqual(ov["credited_tax"], "1.30")
        self.assertEqual(ov["credited_gross"], "11.30")
        self.assertEqual(ov["line_credits"][0]["remaining_net"], "0.00")
        # 逐次税额之和恰好抵完原行已确认税额
        self.assertEqual(sum(Decimal(t) for t in taxes), Decimal("1.30"))


if __name__ == "__main__":
    unittest.main()
