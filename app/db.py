"""SQLite 持久化层。

存储六张表：
* ``invoices``          —— 发票头（含原始请求 JSON，用于复算）与汇总金额；
* ``invoice_lines``     —— 逐行结果（税额残差分配后的最终值，含换算列）；
* ``payments``          —— 收款登记（每笔收款的金额、币种与登记时间）；
* ``payment_reversals`` —— 收款冲销（每笔冲销的金额、币种与登记时间，
  关联原收款流水；原收款不删除，余额一律按净入账额 = 收款 − 冲销 计算）；
* ``installment_plans``      —— 分期回款计划头（每张发票至多一份，invoice_id 唯一）；
* ``installment_plan_items`` —— 计划期次（期次号、到期日、金额，按期次号升序），
  期次金额合计等于发票含税总额；查询时不改写计划，覆盖/逾期状态实时按净已收额计算。
* ``credit_notes``       —— 原发票贷项凭证头（一张凭证对一张已保存发票，可含多行）；
* ``credit_note_lines``  —— 凭证明细（原行行号、本次抵扣净额/税额/含税额，
  以及抵扣后该原行的累计净额/税额）；写入只新增凭证记录，**从不改写原发票、
  原发票行与收款流水**。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    currency        TEXT NOT NULL,
    currency_digits INTEGER NOT NULL,
    target_currency TEXT,
    exchange_rate   TEXT,
    due_date        TEXT,
    total_net       TEXT NOT NULL,
    total_tax       TEXT NOT NULL,
    total_gross     TEXT NOT NULL,
    conv_total_net   TEXT,
    conv_total_tax   TEXT,
    conv_total_gross TEXT,
    residual_units  INTEGER NOT NULL,
    rule_id         TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_lines (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id   INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    line_no      INTEGER NOT NULL,
    description  TEXT,
    tax_rate     TEXT NOT NULL,
    net          TEXT NOT NULL,
    exact_tax    TEXT NOT NULL,
    tax          TEXT NOT NULL,
    gross        TEXT NOT NULL,
    conv_net     TEXT,
    conv_tax     TEXT,
    conv_gross   TEXT,
    UNIQUE(invoice_id, line_no)
);

CREATE INDEX IF NOT EXISTS idx_invoices_created_at ON invoices(created_at);

-- 逾期查询只关心已设置到期日的发票
CREATE INDEX IF NOT EXISTS idx_invoices_due_date ON invoices(due_date);

CREATE TABLE IF NOT EXISTS payments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    amount     TEXT NOT NULL,
    currency   TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_payments_invoice_id ON payments(invoice_id);

CREATE TABLE IF NOT EXISTS payment_reversals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id INTEGER NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
    amount     TEXT NOT NULL,
    currency   TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_payment_reversals_payment_id ON payment_reversals(payment_id);

-- 每张发票至多一份分期回款计划：invoice_id 上的唯一约束是并发兜底
CREATE TABLE IF NOT EXISTS installment_plans (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL UNIQUE REFERENCES invoices(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS installment_plan_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id     INTEGER NOT NULL REFERENCES installment_plans(id) ON DELETE CASCADE,
    installment INTEGER NOT NULL,
    due_date    TEXT NOT NULL,
    amount      TEXT NOT NULL,
    UNIQUE(plan_id, installment)
);

CREATE INDEX IF NOT EXISTS idx_installment_plan_items_plan_id
    ON installment_plan_items(plan_id);

CREATE TABLE IF NOT EXISTS credit_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_credit_notes_invoice_id ON credit_notes(invoice_id);

CREATE TABLE IF NOT EXISTS credit_note_lines (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    credit_note_id INTEGER NOT NULL REFERENCES credit_notes(id) ON DELETE CASCADE,
    line_no        INTEGER NOT NULL,
    net            TEXT NOT NULL,
    tax            TEXT NOT NULL,
    gross          TEXT NOT NULL,
    cumulative_net TEXT NOT NULL,
    cumulative_tax TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_credit_note_lines_credit_note_id
    ON credit_note_lines(credit_note_id);
"""


class InvoiceNotFoundError(Exception):
    """收款登记/查询时发票不存在。"""

    def __init__(self, invoice_id: int) -> None:
        self.invoice_id = invoice_id
        super().__init__(f"发票 {invoice_id} 不存在")


class PaymentNotFoundError(Exception):
    """冲销时原收款流水不存在。"""

    def __init__(self, payment_id: int) -> None:
        self.payment_id = payment_id
        super().__init__(f"收款流水 {payment_id} 不存在")


class PlanAlreadyExistsError(Exception):
    """为已存在分期回款计划的发票再次创建计划。"""

    def __init__(self, invoice_id: int, plan_id: int) -> None:
        self.invoice_id = invoice_id
        self.plan_id = plan_id
        super().__init__(
            f"发票 {invoice_id} 已存在分期回款计划（plan_id={plan_id}），"
            f"每张发票只允许一份计划"
        )


def utc_now_iso() -> str:
    """UTC 时间，ISO 8601 带 Z 后缀，秒精度。"""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def utc_second_text(value: datetime) -> str:
    """感知时间转 UTC 秒精度 ISO 字符串（``YYYY-MM-DDTHH:MM:SSZ``）。

    与 :func:`utc_now_iso` 同构，用于把外部传入的截止时点归一到与库内
    ``created_at`` 相同的形状，供闭区间（``<=``）字符串比较。
    """

    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def db_path() -> str:
    return os.getenv("DB_PATH", "/data/invoices.db")


def connect(database: str | None = None) -> sqlite3.Connection:
    path = database or db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # check_same_thread=False：FastAPI 的依赖注入与端点可能运行在线程池的
    # 不同线程；连接始终是每请求一条、单请求内串行使用，跨线程创建/使用是安全的
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    # 兼容 v1.1 旧库：先为已存在的 invoices 表补上 due_date 列，
    # 再执行 CREATE ... IF NOT EXISTS（新库则无表可迁，直接由 SCHEMA 建出）
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='invoices'"
    ).fetchone()
    if table_exists is not None:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(invoices)").fetchall()}
        if "due_date" not in columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN due_date TEXT")
            conn.commit()
    conn.executescript(SCHEMA)
    conn.commit()


def save_invoice(conn: sqlite3.Connection, payload: dict[str, Any], result: dict[str, Any]) -> int:
    """在单个事务内写入发票头与行项目，返回发票 id。"""

    with conn:
        cur = conn.execute(
            """
            INSERT INTO invoices (
                currency, currency_digits, target_currency, exchange_rate,
                due_date,
                total_net, total_tax, total_gross,
                conv_total_net, conv_total_tax, conv_total_gross,
                residual_units, rule_id, request_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result["currency"],
                result["currency_digits"],
                result.get("fx", {}).get("target_currency"),
                result.get("fx", {}).get("exchange_rate"),
                payload.get("due_date"),
                result["totals"]["total_net"],
                result["totals"]["total_tax"],
                result["totals"]["total_gross"],
                (result.get("fx") or {}).get("total_net"),
                (result.get("fx") or {}).get("total_tax"),
                (result.get("fx") or {}).get("total_gross"),
                result["rounding"]["residual_units"],
                result["rule_id"],
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                utc_now_iso(),
            ),
        )
        invoice_id = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO invoice_lines (
                invoice_id, line_no, description, tax_rate,
                net, exact_tax, tax, gross,
                conv_net, conv_tax, conv_gross
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    invoice_id,
                    line["line_no"],
                    line.get("description"),
                    line["tax_rate"],
                    line["net"],
                    line["exact_tax"],
                    line["tax"],
                    line["gross"],
                    (line.get("converted") or {}).get("net"),
                    (line.get("converted") or {}).get("tax"),
                    (line.get("converted") or {}).get("gross"),
                )
                for line in result["lines"]
            ],
        )
    return invoice_id


def get_invoice(conn: sqlite3.Connection, invoice_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
    ).fetchone()


def get_lines(conn: sqlite3.Connection, invoice_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY line_no",
            (invoice_id,),
        ).fetchall()
    )


def list_invoices(
    conn: sqlite3.Connection, page: int, page_size: int
) -> tuple[list[sqlite3.Row], int]:
    total = int(conn.execute("SELECT COUNT(*) AS c FROM invoices").fetchone()["c"])
    offset = (page - 1) * page_size
    rows = list(
        conn.execute(
            "SELECT * FROM invoices ORDER BY id DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        ).fetchall()
    )
    return rows, total


# ---- 逾期查询 ----


def list_overdue_invoices(
    conn: sqlite3.Connection, as_of: date, page: int, page_size: int
) -> tuple[list[dict[str, Any]], int]:
    """逾期发票分页查询。

    逾期口径：``due_date`` 非空且 **严格早于** ``as_of``，且按 Decimal 逐笔求和
    得到的未收余额（total_gross − 净入账合计，净入账 = Σ payments − Σ reversals）
    **大于 0**；缺少到期日、已收满（余额 0）的发票不纳入。结果按 id 倒序。

    未收余额不走 SQLite 浮点聚合：一次性取回候选发票的收款与冲销流水，在
    Python 侧用 Decimal 逐发票（每张发票币种单一）求和。
    """

    as_of_text = as_of.isoformat()
    rows = list(
        conn.execute(
            """
            SELECT * FROM invoices
            WHERE due_date IS NOT NULL AND due_date < ?
            ORDER BY id DESC
            """,
            (as_of_text,),
        ).fetchall()
    )
    if not rows:
        return [], 0

    paid_map = _paid_totals(conn, [int(r["id"]) for r in rows])

    overdue: list[dict[str, Any]] = []
    for r in rows:
        unit = _minor_unit(int(r["currency_digits"]))
        gross = Decimal(r["total_gross"])
        paid = paid_map.get(int(r["id"]), Decimal("0")).quantize(unit)
        outstanding = gross - paid
        if outstanding > 0:
            overdue.append(
                {
                    "id": int(r["id"]),
                    "currency": r["currency"],
                    "due_date": r["due_date"],
                    "total_gross": format(gross, "f"),
                    "paid_total": format(paid, "f"),
                    "outstanding": format(outstanding, "f"),
                    "created_at": r["created_at"],
                }
            )

    total = len(overdue)
    offset = (page - 1) * page_size
    return overdue[offset : offset + page_size], total


# ---- 收款登记 ----


def _minor_unit(digits: int) -> Decimal:
    """某币种的 1 个最小单位，例如 digits=2 -> Decimal('0.01')。"""

    return Decimal(1).scaleb(-digits)


def _reversed_totals(
    conn: sqlite3.Connection,
    payment_ids: list[int],
    at_text: str | None = None,
) -> dict[int, Decimal]:
    """一批收款流水的已冲销合计。用 Decimal 逐笔求和，不走 SQLite 的浮点 SUM 聚合。

    ``at_text`` 给出 UTC 秒精度截止时点（``YYYY-MM-DDTHH:MM:SSZ``）时，只合计
    ``created_at <= at_text`` 的冲销（截止秒为闭区间，同秒计入），供历史快照
    使用；缺省则合计全部冲销。
    """

    if not payment_ids:
        return {}
    placeholders = ",".join("?" for _ in payment_ids)
    if at_text is None:
        rows = conn.execute(
            "SELECT payment_id, amount FROM payment_reversals"
            f" WHERE payment_id IN ({placeholders})",
            payment_ids,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT payment_id, amount FROM payment_reversals"
            f" WHERE payment_id IN ({placeholders}) AND created_at <= ?",
            (*payment_ids, at_text),
        ).fetchall()
    totals: dict[int, Decimal] = {}
    for r in rows:
        pid = int(r["payment_id"])
        totals[pid] = totals.get(pid, Decimal("0")) + Decimal(r["amount"])
    return totals


def _paid_total(conn: sqlite3.Connection, invoice_id: int) -> Decimal:
    """净入账合计：收款 − 冲销。用 Decimal 逐笔求和，不走 SQLite 的浮点 SUM 聚合。"""

    rows = conn.execute(
        "SELECT id, amount FROM payments WHERE invoice_id = ?", (invoice_id,)
    ).fetchall()
    reversed_map = _reversed_totals(conn, [int(r["id"]) for r in rows])
    return sum(
        (
            Decimal(r["amount"]) - reversed_map.get(int(r["id"]), Decimal("0"))
            for r in rows
        ),
        Decimal("0"),
    )


def register_payment(
    conn: sqlite3.Connection, invoice_id: int, amount: Decimal
) -> dict[str, Any]:
    """登记一笔收款：余额校验与写入在 **同一个事务** 中完成。

    使用 ``BEGIN IMMEDIATE`` 在事务开始即取得写锁，把并发的收款请求串行化，
    「读净入账合计 -> 校验未收余额 -> 写入」之间不会被另一笔收款/冲销插队，
    避免超收。

    * 发票不存在 -> :class:`InvoiceNotFoundError`
    * 金额不是币种最小单位整数倍、或累计净入账将超过含税总额 -> ``ValueError``
      （事务回滚，不留下任何记录；由 API 层映射为 422）
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        inv = conn.execute(
            "SELECT currency, currency_digits, total_gross FROM invoices WHERE id = ?",
            (invoice_id,),
        ).fetchone()
        if inv is None:
            raise InvoiceNotFoundError(invoice_id)

        digits = int(inv["currency_digits"])
        unit = _minor_unit(digits)
        if (amount / unit) != (amount / unit).to_integral_value():
            raise ValueError(
                f"收款金额 {amount} 不是 {inv['currency']} 最小单位 {unit} 的整数倍"
            )
        amount = amount.quantize(unit)

        gross = Decimal(inv["total_gross"])
        paid = _paid_total(conn, invoice_id)
        outstanding = gross - paid
        if amount > outstanding:
            raise ValueError(
                f"收款 {amount} 超出未收余额 {outstanding}"
                f"（含税总额 {gross}，已收 {paid}），未登记任何记录"
            )

        created_at = utc_now_iso()
        cur = conn.execute(
            """
            INSERT INTO payments (invoice_id, amount, currency, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (invoice_id, format(amount, "f"), inv["currency"], created_at),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    new_paid = paid + amount
    return {
        "id": int(cur.lastrowid),
        "invoice_id": invoice_id,
        "amount": format(amount, "f"),
        "currency": inv["currency"],
        "created_at": created_at,
        "reversed_total": format(Decimal("0").quantize(unit), "f"),
        "net_amount": format(amount, "f"),
        "paid_total": format(new_paid, "f"),
        "outstanding": format(gross - new_paid, "f"),
    }


# ---- 收款冲销 ----


def reverse_payment(
    conn: sqlite3.Connection, payment_id: int, amount: Decimal
) -> dict[str, Any]:
    """冲销一笔收款（可同一笔分次冲销）：累计计算与写入在 **同一个事务** 中完成。

    冲销记录写入独立的 ``payment_reversals`` 表并关联原流水，**不删除原收款**；
    全库的未收余额、逾期查询与来款分配都按净入账额（收款 − 冲销）计算，
    冲销后发票的未收余额相应回升。

    使用 ``BEGIN IMMEDIATE`` 在事务开始即取得写锁，与 :func:`register_payment`、
    :func:`allocate_payment` 及其他冲销请求串行化：「读原流水与累计冲销 ->
    校验不超额 -> 写入」在同一事务内完成，并发冲销不会超额。

    * 原收款流水不存在 -> :class:`PaymentNotFoundError`
    * 金额 ≤ 0、不是币种最小单位整数倍、或累计冲销将超过原收款金额 ->
      ``ValueError``（事务回滚，不留下任何记录；由 API 层映射为 422）
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        pay = conn.execute(
            """
            SELECT p.id, p.invoice_id, p.amount, p.currency, i.currency_digits
            FROM payments p JOIN invoices i ON i.id = p.invoice_id
            WHERE p.id = ?
            """,
            (payment_id,),
        ).fetchone()
        if pay is None:
            raise PaymentNotFoundError(payment_id)

        unit = _minor_unit(int(pay["currency_digits"]))
        if amount <= 0:
            raise ValueError(f"冲销金额必须大于 0，收到 {amount}")
        if (amount / unit) != (amount / unit).to_integral_value():
            raise ValueError(
                f"冲销金额 {amount} 不是 {pay['currency']} 最小单位 {unit} 的整数倍"
            )
        amount = amount.quantize(unit)

        original = Decimal(pay["amount"])
        reversed_so_far = _reversed_totals(conn, [payment_id]).get(
            payment_id, Decimal("0")
        )
        if reversed_so_far + amount > original:
            raise ValueError(
                f"冲销 {amount} 后累计冲销将达 {reversed_so_far + amount}，"
                f"超过原收款 {original}（已冲销 {reversed_so_far}），未写入任何记录"
            )

        created_at = utc_now_iso()
        cur = conn.execute(
            """
            INSERT INTO payment_reversals (payment_id, amount, currency, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (payment_id, format(amount, "f"), pay["currency"], created_at),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    new_reversed = reversed_so_far + amount
    return {
        "id": int(cur.lastrowid),
        "payment_id": payment_id,
        "invoice_id": int(pay["invoice_id"]),
        "amount": format(amount, "f"),
        "currency": pay["currency"],
        "created_at": created_at,
        "reversed_total": format(new_reversed, "f"),
        "net_amount": format(original - new_reversed, "f"),
    }


def _paid_totals(conn: sqlite3.Connection, invoice_ids: list[int]) -> dict[int, Decimal]:
    """一批发票的净入账合计（收款 − 冲销）。用 Decimal 逐笔求和，不走浮点 SUM 聚合。"""

    if not invoice_ids:
        return {}
    placeholders = ",".join("?" for _ in invoice_ids)
    rows = conn.execute(
        f"SELECT id, invoice_id, amount FROM payments WHERE invoice_id IN ({placeholders})",
        invoice_ids,
    ).fetchall()
    reversed_map = _reversed_totals(conn, [int(r["id"]) for r in rows])
    paid: dict[int, Decimal] = {}
    for r in rows:
        iid = int(r["invoice_id"])
        net = Decimal(r["amount"]) - reversed_map.get(int(r["id"]), Decimal("0"))
        paid[iid] = paid.get(iid, Decimal("0")) + net
    return paid


def allocate_payment(
    conn: sqlite3.Connection,
    currency: str,
    amount: Decimal,
    invoice_ids: list[int],
) -> dict[str, Any]:
    """把一笔来款分配到多张同币种发票上，整笔成功或整笔拒绝。

    分配顺序：``due_date`` 升序（未设置到期日的排最后）、同一到期日按发票 ID
    升序；只处理 ``invoice_ids`` 中的发票，不做跨币种换算。按顺序逐张抵扣：
    每张分配 ``min(剩余来款, 该发票未收余额)``，来款用尽即止。未收余额按
    **净入账额**（收款 − 冲销）计算，冲销释放的余额可以再次被分配。

    使用 ``BEGIN IMMEDIATE`` 在事务开始即取得写锁，与 :func:`register_payment`、
    :func:`reverse_payment` 及彼此的来款分配请求串行化：「读发票/净入账合计 ->
    校验 -> 写入多条 payments」在同一事务内完成，并发收款不会超收。

    任一前置条件不满足都回滚，**不留下任何收款记录**：

    * 列表中任一发票不存在 -> :class:`InvoiceNotFoundError`
    * 来款金额 ≤ 0、不是币种最小单位整数倍 -> ``ValueError``
    * 发票币种与来款不一致 -> ``ValueError``
    * 列表内发票未收余额合计 < 来款金额 -> ``ValueError``
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 调用方（schema 层）已保证列表非空且无重复，这里再防御性校验一次，
        # 避免绕过 API 直接调用本函数时出现部分写入
        if not invoice_ids:
            raise ValueError("invoice_ids 不能为空")
        if len(invoice_ids) != len(set(invoice_ids)):
            raise ValueError("invoice_ids 不得包含重复的发票 ID")

        placeholders = ",".join("?" for _ in invoice_ids)
        rows = list(
            conn.execute(
                f"SELECT id, currency, currency_digits, due_date, total_gross"
                f" FROM invoices WHERE id IN ({placeholders})",
                invoice_ids,
            ).fetchall()
        )
        found = {int(r["id"]): r for r in rows}
        for invoice_id in invoice_ids:
            if invoice_id not in found:
                raise InvoiceNotFoundError(invoice_id)

        digits_by_id = {int(r["id"]): int(r["currency_digits"]) for r in rows}
        currencies = {r["currency"] for r in rows}
        mismatched = sorted(c for c in currencies if c != currency)
        if mismatched:
            raise ValueError(
                f"来款币种 {currency} 与发票币种不一致：{', '.join(mismatched)}；"
                f"来款分配不做跨币种换算"
            )
        digits = digits_by_id[invoice_ids[0]]
        unit = _minor_unit(digits)

        if amount <= 0:
            raise ValueError(f"来款金额必须大于 0，收到 {amount}")
        if (amount / unit) != (amount / unit).to_integral_value():
            raise ValueError(
                f"来款金额 {amount} 不是 {currency} 最小单位 {unit} 的整数倍"
            )
        amount = amount.quantize(unit)

        paid_map = _paid_totals(conn, invoice_ids)
        outstanding: dict[int, Decimal] = {}
        for invoice_id in invoice_ids:
            r = found[invoice_id]
            outstanding[invoice_id] = Decimal(r["total_gross"]) - paid_map.get(
                invoice_id, Decimal("0")
            )

        total_outstanding = sum(outstanding.values(), Decimal("0"))
        if amount > total_outstanding:
            raise ValueError(
                f"来款 {amount} {currency} 超出列表内发票未收余额合计 "
                f"{format(total_outstanding, 'f')}，整笔拒绝，未登记任何记录"
            )

        # 到期日升序、NULL 排最后（is None -> True=1）；同日（含都为空）按 ID 升序
        ordered_ids = sorted(
            invoice_ids,
            key=lambda i: (found[i]["due_date"] is None, found[i]["due_date"], i),
        )

        remaining = amount
        created_at = utc_now_iso()
        items: list[dict[str, Any]] = []
        for invoice_id in ordered_ids:
            due = outstanding[invoice_id]
            allocated = remaining if remaining < due else due
            payment_id: int | None = None
            if allocated > 0:
                cur = conn.execute(
                    """
                    INSERT INTO payments (invoice_id, amount, currency, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (invoice_id, format(allocated, "f"), currency, created_at),
                )
                payment_id = int(cur.lastrowid)
                remaining -= allocated
            items.append(
                {
                    "invoice_id": invoice_id,
                    "due_date": found[invoice_id]["due_date"],
                    "allocated": format(allocated, "f"),
                    "outstanding": format(due - allocated, "f"),
                    "payment_id": payment_id,
                }
            )
        if remaining != 0:
            # 不变量防御：上面已校验总未收充足，理论上不可能剩余
            raise AssertionError(f"来款分配后仍有余额 {format(remaining, 'f')} 未抵扣")
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "currency": currency,
        "amount": format(amount, "f"),
        "allocated_total": format(amount, "f"),
        "created_at": created_at,
        "allocations": items,
    }


def payment_overview(conn: sqlite3.Connection, invoice: sqlite3.Row) -> dict[str, Any]:
    """某张发票的收款记录与未收余额（供 GET 使用）。

    每条流水附带已冲销金额 ``reversed_total`` 与净入账额 ``net_amount``；
    顶层 ``paid_total`` 为全发票净入账合计，``outstanding`` 按净入账额计算。
    """

    rows = list(
        conn.execute(
            "SELECT * FROM payments WHERE invoice_id = ? ORDER BY id",
            (invoice["id"],),
        ).fetchall()
    )
    unit = _minor_unit(int(invoice["currency_digits"]))
    reversed_map = _reversed_totals(conn, [int(r["id"]) for r in rows])
    gross = Decimal(invoice["total_gross"])
    paid = Decimal("0")
    reversed_total = Decimal("0")
    payments: list[dict[str, Any]] = []
    for r in rows:
        rev = reversed_map.get(int(r["id"]), Decimal("0")).quantize(unit)
        net = Decimal(r["amount"]) - rev
        paid += net
        reversed_total += rev
        payments.append(
            {
                "id": r["id"],
                "invoice_id": r["invoice_id"],
                "amount": r["amount"],
                "currency": r["currency"],
                "created_at": r["created_at"],
                "reversed_total": format(rev, "f"),
                "net_amount": format(net, "f"),
            }
        )
    paid = paid.quantize(unit)
    return {
        "invoice_id": invoice["id"],
        "currency": invoice["currency"],
        "total_gross": invoice["total_gross"],
        "paid_total": format(paid, "f"),
        "reversed_total": format(reversed_total.quantize(unit), "f"),
        "outstanding": format(gross - paid, "f"),
        "payment_count": len(rows),
        "payments": payments,
    }


# ---- 历史收款快照 ----


def payment_snapshot(
    conn: sqlite3.Connection, invoice: sqlite3.Row, at: datetime
) -> dict[str, Any]:
    """单张发票在指定 UTC 时点的历史收款快照（供 GET 使用）。

    截止时点 ``at`` 为**闭区间、秒精度**：``created_at <= at`` 的流水全部计入，
    即截止秒当秒写入的收款/冲销都包含在内；库内 ``created_at`` 与 ``at`` 均为
    定长的 ``YYYY-MM-DDTHH:MM:SSZ`` UTC 文本，直接用字符串字典序比较即可。

    分别筛选快照时点之前及当秒的：

    * **收款**：该发票 ``payments.created_at <= at`` 的流水；
    * **冲销**：关联上述收款且 ``payment_reversals.created_at <= at`` 的流水
      （按所属收款连接到发票，并独立按冲销自身登记时间过滤）。

    随后按发票币种最小单位用 ``Decimal`` 计算收款总额、冲销总额、净已收
    （收款 − 冲销）与未收余额（含税总额 − 净已收）。查询只读取快照时点之前的
    历史行，**截止后写入的任何流水都不参与，历史结果不会随后续流水改变**。

    ``at`` 早于发票创建时间（``invoices.created_at``，秒精度）时抛
    ``ValueError``（由 API 层映射为 422）。
    """

    at_text = utc_second_text(at)
    created_text = invoice["created_at"]
    if at_text < created_text:
        raise ValueError(
            f"截止时点 {at_text} 早于发票创建时间 {created_text}，"
            f"无法生成历史收款快照"
        )

    payment_rows = list(
        conn.execute(
            """
            SELECT * FROM payments
            WHERE invoice_id = ? AND created_at <= ?
            ORDER BY id
            """,
            (invoice["id"], at_text),
        ).fetchall()
    )
    payment_ids = [int(r["id"]) for r in payment_rows]
    reversed_map = _reversed_totals(conn, payment_ids, at_text=at_text)

    reversal_rows: list[sqlite3.Row] = []
    if payment_ids:
        placeholders = ",".join("?" for _ in payment_ids)
        reversal_rows = list(
            conn.execute(
                f"""
                SELECT r.* FROM payment_reversals r
                WHERE r.payment_id IN ({placeholders}) AND r.created_at <= ?
                ORDER BY r.id
                """,
                (*payment_ids, at_text),
            ).fetchall()
        )

    unit = _minor_unit(int(invoice["currency_digits"]))
    gross = Decimal(invoice["total_gross"])
    received_total = Decimal("0")
    reversed_total = Decimal("0")
    payments: list[dict[str, Any]] = []
    for r in payment_rows:
        amount = Decimal(r["amount"])
        rev = reversed_map.get(int(r["id"]), Decimal("0")).quantize(unit)
        net = amount - rev
        received_total += amount
        reversed_total += rev
        payments.append(
            {
                "id": r["id"],
                "invoice_id": r["invoice_id"],
                "amount": format(amount.quantize(unit), "f"),
                "currency": r["currency"],
                "created_at": r["created_at"],
                "reversed_total": format(rev, "f"),
                "net_amount": format(net, "f"),
            }
        )

    reversals: list[dict[str, Any]] = []
    for r in reversal_rows:
        amount = Decimal(r["amount"]).quantize(unit)
        reversals.append(
            {
                "id": r["id"],
                "payment_id": r["payment_id"],
                "invoice_id": invoice["id"],
                "amount": format(amount, "f"),
                "currency": r["currency"],
                "created_at": r["created_at"],
            }
        )

    received_total = received_total.quantize(unit)
    reversed_total = reversed_total.quantize(unit)
    net_paid = (received_total - reversed_total).quantize(unit)
    return {
        "invoice_id": invoice["id"],
        "currency": invoice["currency"],
        "at": at_text,
        "invoice_created_at": created_text,
        "total_gross": format(gross.quantize(unit), "f"),
        "received_total": format(received_total, "f"),
        "reversed_total": format(reversed_total, "f"),
        "net_paid": format(net_paid, "f"),
        "outstanding": format((gross - net_paid).quantize(unit), "f"),
        "payment_count": len(payment_rows),
        "reversal_count": len(reversal_rows),
        "payments": payments,
        "reversals": reversals,
    }


# ---- 分期回款计划 ----


def get_installment_plan(
    conn: sqlite3.Connection, invoice_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM installment_plans WHERE invoice_id = ?", (invoice_id,)
    ).fetchone()


def get_installment_plan_items(
    conn: sqlite3.Connection, plan_id: int
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM installment_plan_items WHERE plan_id = ? ORDER BY installment",
            (plan_id,),
        ).fetchall()
    )


def create_installment_plan(
    conn: sqlite3.Connection,
    invoice_id: int,
    installments: list[tuple[str, Decimal]],
) -> dict[str, Any]:
    """为单张发票创建唯一一份分期回款计划：全部校验与写入在 **同一事务** 完成。

    ``installments`` 为 ``(due_date, amount)`` 列表，调用方（schema 层）已保证
    非空且到期日非递减（升序、允许相同）。事务内（``BEGIN IMMEDIATE``）再做
    全部业务校验，任一不满足都回滚、**不留下任何计划行**：

    * 发票不存在 -> :class:`InvoiceNotFoundError`
    * 该发票已存在计划 -> :class:`PlanAlreadyExistsError`
    * 任一期金额 ≤ 0、不是发票币种最小单位的正整数倍 -> ``ValueError``
    * 期次金额合计不等于发票含税总额 -> ``ValueError``

    成功后返回按当前 UTC 日期计算的完整计划视图（与查询接口同构）。
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        inv = conn.execute(
            "SELECT id, currency, currency_digits, total_gross FROM invoices WHERE id = ?",
            (invoice_id,),
        ).fetchone()
        if inv is None:
            raise InvoiceNotFoundError(invoice_id)

        existing = conn.execute(
            "SELECT id FROM installment_plans WHERE invoice_id = ?", (invoice_id,)
        ).fetchone()
        if existing is not None:
            raise PlanAlreadyExistsError(invoice_id, int(existing["id"]))

        unit = _minor_unit(int(inv["currency_digits"]))
        gross = Decimal(inv["total_gross"])

        amounts: list[Decimal] = []
        for idx, (_due, raw_amount) in enumerate(installments, start=1):
            if raw_amount <= 0:
                raise ValueError(f"第 {idx} 期金额必须大于 0，收到 {raw_amount}")
            if (raw_amount / unit) != (raw_amount / unit).to_integral_value():
                raise ValueError(
                    f"第 {idx} 期金额 {raw_amount} 不是 {inv['currency']} 最小单位 "
                    f"{unit} 的整数倍"
                )
            amounts.append(raw_amount.quantize(unit))

        total = sum(amounts, Decimal("0"))
        if total != gross:
            raise ValueError(
                f"期次合计 {format(total, 'f')} 与发票含税总额 "
                f"{format(gross, 'f')} 不一致，未创建任何计划"
            )

        created_at = utc_now_iso()
        cur = conn.execute(
            "INSERT INTO installment_plans (invoice_id, created_at) VALUES (?, ?)",
            (invoice_id, created_at),
        )
        plan_id = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO installment_plan_items (plan_id, installment, due_date, amount)
            VALUES (?, ?, ?, ?)
            """,
            [
                (plan_id, idx, due, format(amount, "f"))
                for idx, ((due, _raw), amount) in enumerate(
                    zip(installments, amounts), start=1
                )
            ],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return installment_plan_status(
        conn, inv, plan_id, created_at,
        get_installment_plan_items(conn, plan_id),
        datetime.now(timezone.utc).date(),
    )


def installment_plan_status(
    conn: sqlite3.Connection,
    invoice: sqlite3.Row | dict[str, Any],
    plan_id: int,
    created_at: str,
    item_rows: list[sqlite3.Row],
    as_of: date,
) -> dict[str, Any]:
    """按期次顺序用净已收额（收款 − 冲销）逐期抵扣，生成计划视图。

    抵扣为**先进先出（FIFO）**：净已收总额先覆盖第 1 期，剩余再覆盖第 2 期，
    依次类推；每期得到 ``covered``（已覆盖额）与 ``uncovered``（未覆盖额 =
    期次金额 − 已覆盖额）。**只有到期日严格早于 ``as_of`` 的未覆盖额计入
    ``overdue``（逾期额）**，到期日当天不计逾期。

    计算只读取 ``payments`` / ``payment_reversals``（沿用现有净已收口径），
    **从不改写原计划**；收款被冲销后净已收回降，较后期次会重新打开（uncovered
    回升），计划行本身保持创建时的期次与金额不变。
    """

    invoice_id = int(invoice["id"])
    currency = invoice["currency"]
    unit = _minor_unit(int(invoice["currency_digits"]))
    gross = Decimal(invoice["total_gross"]).quantize(unit)
    net_paid = _paid_total(conn, invoice_id).quantize(unit)

    as_of_text = as_of.isoformat()
    remaining_credit = net_paid
    items: list[dict[str, Any]] = []
    covered_total = Decimal("0")
    uncovered_total = Decimal("0")
    overdue_total = Decimal("0")
    for r in item_rows:
        amount = Decimal(r["amount"]).quantize(unit)
        covered = remaining_credit if remaining_credit < amount else amount
        if covered > 0:
            remaining_credit -= covered
        uncovered = amount - covered
        overdue = uncovered if r["due_date"] < as_of_text else Decimal("0").quantize(unit)
        covered_total += covered
        uncovered_total += uncovered
        overdue_total += overdue
        items.append(
            {
                "installment": int(r["installment"]),
                "due_date": r["due_date"],
                "amount": format(amount, "f"),
                "covered": format(covered, "f"),
                "uncovered": format(uncovered, "f"),
                "overdue": format(overdue, "f"),
            }
        )

    return {
        "plan_id": plan_id,
        "invoice_id": invoice_id,
        "currency": currency,
        "created_at": created_at,
        "as_of": as_of_text,
        "total_gross": format(gross, "f"),
        "net_paid": format(net_paid, "f"),
        "covered_total": format(covered_total.quantize(unit), "f"),
        "uncovered_total": format(uncovered_total.quantize(unit), "f"),
        "overdue_total": format(overdue_total.quantize(unit), "f"),
        "installments": items,
    }


# ---- 原发票贷项凭证 ----


def _credit_note_view(conn: sqlite3.Connection, note_row: sqlite3.Row) -> dict[str, Any]:
    """用凭证头与明细行组装响应视图；原行净额/税额取自 invoice_lines（从不改写）。"""

    invoice_id = int(note_row["invoice_id"])
    unit = _minor_unit(int(note_row["currency_digits"]))
    rows = list(
        conn.execute(
            """
            SELECT cl.line_no, cl.net, cl.tax, cl.gross,
                   cl.cumulative_net, cl.cumulative_tax,
                   il.net AS original_net, il.tax AS original_tax
            FROM credit_note_lines cl
            JOIN invoice_lines il
              ON il.invoice_id = ? AND il.line_no = cl.line_no
            WHERE cl.credit_note_id = ?
            ORDER BY cl.line_no
            """,
            (invoice_id, int(note_row["id"])),
        ).fetchall()
    )
    lines: list[dict[str, Any]] = []
    total_net = Decimal("0")
    total_tax = Decimal("0")
    total_gross = Decimal("0")
    for r in rows:
        net = Decimal(r["net"])
        tax = Decimal(r["tax"])
        gross = Decimal(r["gross"])
        total_net += net
        total_tax += tax
        total_gross += gross
        lines.append(
            {
                "line_no": int(r["line_no"]),
                "net": format(net.quantize(unit), "f"),
                "tax": format(tax.quantize(unit), "f"),
                "gross": format(gross.quantize(unit), "f"),
                "original_net": r["original_net"],
                "original_tax": r["original_tax"],
                "cumulative_net": r["cumulative_net"],
                "cumulative_tax": r["cumulative_tax"],
            }
        )
    return {
        "id": int(note_row["id"]),
        "invoice_id": invoice_id,
        "currency": note_row["currency"],
        "created_at": note_row["created_at"],
        "total_net": format(total_net.quantize(unit), "f"),
        "total_tax": format(total_tax.quantize(unit), "f"),
        "total_gross": format(total_gross.quantize(unit), "f"),
        "lines": lines,
    }


def create_credit_note(
    conn: sqlite3.Connection,
    invoice_id: int,
    lines: list[tuple[int, Decimal]],
) -> dict[str, Any]:
    """为一张已保存发票创建一张可含多行的部分抵扣贷项凭证。

    ``lines`` 为 ``(原行行号 line_no, 本次抵扣净额)`` 列表，调用方（schema 层）
    已保证非空且凭证内 line_no 不重复。全部校验与凭证头/明细写入在**同一个
    ``BEGIN IMMEDIATE`` 写事务**内完成：「读原行与历次抵扣 -> 逐行校验并算税 ->
    写入」之间不会被另一张凭证插队，同一原行的历次累计抵扣净额绝不会超过原净额。

    仅允许抵扣**正净额原行**：

    * 发票不存在 -> :class:`InvoiceNotFoundError`
    * line_no 不存在（或该行净额 ≤ 0）-> ``ValueError``
    * 本次抵扣净额 ≤ 0、不是币种最小单位整数倍 -> ``ValueError``
    * 加上本次后该原行累计抵扣净额超过原净额 -> ``ValueError``

    每行本次税额按

        抵扣后累计税额 = round_half_up(原行已确认税额 × 抵扣后累计净额 / 原净额)
        本次税额       = 抵扣后累计税额 − 历次已记税额

    计算（以币种最小单位四舍五入）；原净额恰好抵完时累计税额恒等于原行已确认
    税额，即完全抵扣时恰好抵完原行税额。任一行校验失败整体回滚，不留任何凭证行；
    原发票、原发票行与收款流水始终保持原样。
    """

    # 防御性校验，避免绕过 API 直接调用本函数时部分写入
    if not lines:
        raise ValueError("抵扣行不能为空")
    line_nos = [line_no for line_no, _amount in lines]
    if len(line_nos) != len(set(line_nos)):
        raise ValueError("同一张贷项凭证内 line_no 不得重复")

    conn.execute("BEGIN IMMEDIATE")
    try:
        inv = conn.execute(
            "SELECT id, currency, currency_digits FROM invoices WHERE id = ?",
            (invoice_id,),
        ).fetchone()
        if inv is None:
            raise InvoiceNotFoundError(invoice_id)

        currency = inv["currency"]
        unit = _minor_unit(int(inv["currency_digits"]))
        original_rows = list(
            conn.execute(
                "SELECT line_no, net, tax FROM invoice_lines WHERE invoice_id = ?",
                (invoice_id,),
            ).fetchall()
        )
        original_by_no = {int(r["line_no"]): r for r in original_rows}

        # 每个被抵扣原行的历次已记净额/税额，用 Decimal 逐行求和（不走浮点 SUM）
        target_nos = list(dict.fromkeys(line_nos))
        placeholders = ",".join("?" for _ in target_nos)
        prior_rows = conn.execute(
            f"""
            SELECT il.line_no, cl.net, cl.tax
            FROM credit_note_lines cl
            JOIN credit_notes cn ON cn.id = cl.credit_note_id
            JOIN invoice_lines il
              ON il.invoice_id = cn.invoice_id AND il.line_no = cl.line_no
            WHERE cn.invoice_id = ? AND il.line_no IN ({placeholders})
            """,
            (invoice_id, *target_nos),
        ).fetchall()
        prior_net: dict[int, Decimal] = {}
        prior_tax: dict[int, Decimal] = {}
        for r in prior_rows:
            no = int(r["line_no"])
            prior_net[no] = prior_net.get(no, Decimal("0")) + Decimal(r["net"])
            prior_tax[no] = prior_tax.get(no, Decimal("0")) + Decimal(r["tax"])

        prepared: list[dict[str, Any]] = []
        with localcontext() as ctx:
            ctx.prec = 50
            for idx, (line_no, raw_amount) in enumerate(lines, start=1):
                original = original_by_no.get(line_no)
                if original is None:
                    raise ValueError(
                        f"第 {idx} 行 line_no={line_no} 在发票 {invoice_id} 中不存在"
                    )
                original_net = Decimal(original["net"])
                original_tax = Decimal(original["tax"])
                if original_net <= 0:
                    # 只有正净额原行允许抵扣（冲红/零额行不在贷项凭证范围内）
                    raise ValueError(
                        f"第 {idx} 行 line_no={line_no} 的原净额 "
                        f"{format(original_net, 'f')} 非正数，不能抵扣"
                    )
                if raw_amount <= 0:
                    raise ValueError(
                        f"第 {idx} 行（line_no={line_no}）抵扣净额必须大于 0，"
                        f"收到 {format(raw_amount, 'f')}"
                    )
                if (raw_amount / unit) != (raw_amount / unit).to_integral_value():
                    raise ValueError(
                        f"第 {idx} 行（line_no={line_no}）抵扣净额 {format(raw_amount, 'f')} "
                        f"不是 {currency} 最小单位 {format(unit, 'f')} 的整数倍"
                    )
                amount = raw_amount.quantize(unit)

                before_net = prior_net.get(line_no, Decimal("0")).quantize(unit)
                before_tax = prior_tax.get(line_no, Decimal("0")).quantize(unit)
                cumulative_net = before_net + amount
                if cumulative_net > original_net:
                    raise ValueError(
                        f"第 {idx} 行（line_no={line_no}）抵扣后累计净额 "
                        f"{format(cumulative_net, 'f')} 超过原净额 "
                        f"{format(original_net, 'f')}（历次已抵扣 {format(before_net, 'f')}），"
                        f"未创建任何凭证"
                    )

                cumulative_tax = (
                    original_tax * cumulative_net / original_net
                ).quantize(unit, rounding=ROUND_HALF_UP)
                if cumulative_net == original_net:
                    # 完全抵扣：必须恰好抵完原行已确认税额（比例公式此时本就等于原税额）
                    cumulative_tax = original_tax
                tax = cumulative_tax - before_tax
                if tax < 0:
                    # 数学上不可能（HALF_UP 对非负输入单调不减）；作为不变量防御
                    raise AssertionError(
                        f"第 {idx} 行（line_no={line_no}）本次税额为负：{format(tax, 'f')}"
                    )
                gross = amount + tax
                prepared.append(
                    {
                        "line_no": line_no,
                        "net": amount,
                        "tax": tax,
                        "gross": gross,
                        "cumulative_net": cumulative_net,
                        "cumulative_tax": cumulative_tax,
                    }
                )

        created_at = utc_now_iso()
        cur = conn.execute(
            "INSERT INTO credit_notes (invoice_id, created_at) VALUES (?, ?)",
            (invoice_id, created_at),
        )
        credit_note_id = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO credit_note_lines (
                credit_note_id, line_no, net, tax, gross,
                cumulative_net, cumulative_tax
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    credit_note_id,
                    item["line_no"],
                    format(item["net"], "f"),
                    format(item["tax"], "f"),
                    format(item["gross"], "f"),
                    format(item["cumulative_net"], "f"),
                    format(item["cumulative_tax"], "f"),
                )
                for item in prepared
            ],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    note_row = conn.execute(
        """
        SELECT cn.id, cn.invoice_id, cn.created_at,
               i.currency, i.currency_digits
        FROM credit_notes cn JOIN invoices i ON i.id = cn.invoice_id
        WHERE cn.id = ?
        """,
        (credit_note_id,),
    ).fetchone()
    return _credit_note_view(conn, note_row)


def list_credit_notes(conn: sqlite3.Connection, invoice_id: int) -> dict[str, Any] | None:
    """按原发票查询全部贷项凭证（按凭证 id 升序）。发票不存在返回 ``None``。

    纯读接口：原发票与收款流水保持原样，凭证金额全部取自已持久化的凭证行。
    """

    inv = conn.execute(
        "SELECT id, currency FROM invoices WHERE id = ?",
        (invoice_id,),
    ).fetchone()
    if inv is None:
        return None

    note_rows = list(
        conn.execute(
            """
            SELECT cn.id, cn.invoice_id, cn.created_at,
                   i.currency, i.currency_digits
            FROM credit_notes cn JOIN invoices i ON i.id = cn.invoice_id
            WHERE cn.invoice_id = ?
            ORDER BY cn.id
            """,
            (invoice_id,),
        ).fetchall()
    )
    notes = [_credit_note_view(conn, r) for r in note_rows]
    return {
        "invoice_id": invoice_id,
        "currency": inv["currency"],
        "count": len(notes),
        "credit_notes": notes,
    }
