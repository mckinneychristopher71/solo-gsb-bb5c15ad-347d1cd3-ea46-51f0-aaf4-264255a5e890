"""发票税额计算 API（FastAPI）。

启动：uvicorn app.main:app
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import db as database
from .calc import compute_invoice, recompute_matches, to_decimal
from .currency import UnknownCurrencyError
from .duplicates import find_duplicates
from .schemas import (
    CreditMemoCreate,
    CreditMemoOverview,
    CreditMemoResult,
    Health,
    InstallmentPlanCreate,
    InstallmentPlanResult,
    InvoiceCreate,
    InvoiceNumberCheckRequest,
    InvoiceNumberCheckResult,
    InvoiceResult,
    PagedInvoices,
    PagedOverdueInvoices,
    PaymentAllocationCreate,
    PaymentAllocationResult,
    PaymentCreate,
    PaymentOverview,
    PaymentResult,
    PaymentSnapshot,
    ReversalCreate,
    ReversalResult,
    parse_iso_date,
    parse_utc_second,
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # 启动时确保表结构存在
    with database.connect() as conn:
        database.init_db(conn)
    yield


app = FastAPI(
    title="发票税额计算 API",
    version="1.8.0",
    description=(
        "基于 Decimal 的发票税额计算服务：按币种最小单位舍入、"
        "最大余数法分配残差、可复算，支持跨币种换算、收款登记与未收余额查询、"
        "收款冲销（分次冲销、按净入账额计算余额）、"
        "一笔来款在多张同币种待结发票间按到期日顺序分配、"
        "到期日（due_date）与逾期发票分页查询、"
        "单张发票指定 UTC 时点的历史收款快照（截止秒闭区间）、"
        "单张发票唯一一份分期回款计划（按期次顺序 FIFO 抵扣、逾期额按查询日计算）、"
        "对已保存发票正净额行的原发票贷项凭证（多行部分抵扣、按累计净额比例摊算税额、"
        "完全抵扣恰好抵完原行税额、不改写原发票与收款流水）、"
        "供应商发票号码批次查重（NFKC 规范化后同供应商内判重）、"
        "SQLite 持久化。"
    ),
    lifespan=lifespan,
)


def get_db() -> Iterator[sqlite3.Connection]:
    """每请求一个 SQLite 连接，请求结束关闭。"""

    conn = database.connect()
    try:
        yield conn
    finally:
        conn.close()


@app.exception_handler(ValueError)
async def _value_error_handler(_request: Any, exc: ValueError) -> JSONResponse:
    """计算/校验错误统一返回 422（UnknownCurrencyError 也是 ValueError 子类）。"""

    return JSONResponse(status_code=422, content={"detail": str(exc)})


def _row_to_result(row: sqlite3.Row, line_rows: list[sqlite3.Row]) -> dict[str, Any]:
    """用数据库行重建与 compute_invoice 同构的结果，并用原始请求做复算校验。"""

    stored_request = json.loads(row["request_json"])
    fx_target = row["target_currency"]

    lines: list[dict[str, Any]] = []
    for lr in line_rows:
        line: dict[str, Any] = {
            "line_no": lr["line_no"],
            "description": lr["description"],
            "tax_rate": lr["tax_rate"],
            "currency": row["currency"],
            "net": lr["net"],
            "exact_tax": lr["exact_tax"],
            "tax": lr["tax"],
            "gross": lr["gross"],
        }
        if fx_target is not None and lr["conv_net"] is not None:
            line["converted"] = {
                "currency": fx_target,
                "net": lr["conv_net"],
                "tax": lr["conv_tax"],
                "gross": lr["conv_gross"],
            }
        lines.append(line)

    # 残差分配明细不单独建列，复算一次取回（纯函数，结果确定）
    fresh = compute_invoice(stored_request)
    result: dict[str, Any] = {
        "rule_id": row["rule_id"],
        "currency": row["currency"],
        "currency_digits": row["currency_digits"],
        "lines": lines,
        "totals": {
            "currency": row["currency"],
            "total_net": row["total_net"],
            "total_tax": row["total_tax"],
            "total_gross": row["total_gross"],
        },
        "rounding": fresh["rounding"],
    }
    if fx_target is not None:
        result["fx"] = {
            "target_currency": fx_target,
            "target_digits": fresh["fx"]["target_digits"],
            "exchange_rate": row["exchange_rate"],
            "rate_direction": fresh["fx"]["rate_direction"],
            "total_net": row["conv_total_net"],
            "total_tax": row["conv_total_tax"],
            "total_gross": row["conv_total_gross"],
            "residual_units": fresh["fx"]["residual_units"],
            "residual_unit": fresh["fx"]["residual_unit"],
            "allocations": fresh["fx"]["allocations"],
        }

    core = {
        k: v
        for k, v in result.items()
        if k not in ("id", "created_at", "recompute_matched", "due_date")
    }
    result["id"] = row["id"]
    result["due_date"] = row["due_date"]
    result["created_at"] = row["created_at"]
    result["recompute_matched"] = recompute_matches(core, stored_request)
    return result


# ---- 端点 ----

@app.get("/health", response_model=Health, tags=["health"])
def health(conn: sqlite3.Connection = Depends(get_db)) -> Health:
    try:
        conn.execute("SELECT 1").fetchone()
    except Exception:  # pragma: no cover
        raise HTTPException(status_code=503, detail="database unavailable")
    return Health(status="ok", database="up")


@app.post(
    "/api/v1/invoice-number-check",
    response_model=InvoiceNumberCheckResult,
    tags=["invoice-number-check"],
)
def check_invoice_numbers(
    payload: InvoiceNumberCheckRequest,
) -> dict[str, Any]:
    """供应商发票号码批次查重（1~500 条，无状态、不落库）。

    对每个供应商编号与发票号码做 Unicode NFKC 规范化、去除首尾空白并按
    大小写折叠，在**同一供应商内**识别规范化后重复的号码。原始号码为空、
    超过 100 个 Unicode 字符或供应商编号为空返回 422；条目数越界、缺字段
    同样返回 422。响应按输入首次出现顺序返回每个重复组的供应商（首次出现
    位置的原始值）、规范化号码、所有输入位置（从 1 开始）及原始号码，
    未重复号码不列出。
    """

    pairs = [(item.supplier_code, item.invoice_number) for item in payload.items]
    groups = find_duplicates(pairs)
    return {
        "items": [
            {
                "supplier_code": group.supplier_code,
                "normalized_invoice_number": group.normalized_invoice_number,
                "positions": list(group.positions),
                "original_invoice_numbers": list(group.original_invoice_numbers),
            }
            for group in groups
        ]
    }


@app.post(
    "/api/v1/invoices",
    response_model=InvoiceResult,
    status_code=201,
    tags=["invoices"],
)
def create_invoice(
    payload: InvoiceCreate, conn: sqlite3.Connection = Depends(get_db)
) -> dict[str, Any]:
    # mode="json"：due_date 等 date 字段也以 ISO 字符串进入纯数据字典，
    # compute_invoice 只读取金额/币种字段，额外键会被忽略
    data = payload.model_dump(mode="json")
    try:
        result = compute_invoice(data)
    except UnknownCurrencyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    invoice_id = database.save_invoice(conn, data, result)
    row = database.get_invoice(conn, invoice_id)
    line_rows = database.get_lines(conn, invoice_id)
    return _row_to_result(row, line_rows)


@app.get(
    "/api/v1/invoices/overdue",
    response_model=PagedOverdueInvoices,
    tags=["invoices"],
)
def list_overdue_invoices(
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数，1~100"),
    as_of: str | None = Query(
        None,
        description="截止日期 ISO 8601（YYYY-MM-DD）；缺省取 UTC 当前日期。"
        "仅返回 due_date 严格早于该日且未收余额大于零的发票",
    ),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    if as_of is None:
        cutoff = datetime.now(timezone.utc).date()
    else:
        # 非法日期走全局 ValueError -> 422 处理器
        cutoff = parse_iso_date(as_of, "as_of")

    items, total = database.list_overdue_invoices(conn, cutoff, page, page_size)
    pages = (total + page_size - 1) // page_size if total else 0
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": pages,
        "as_of": cutoff.isoformat(),
    }


@app.get(
    "/api/v1/invoices/{invoice_id}",
    response_model=InvoiceResult,
    tags=["invoices"],
)
def read_invoice(
    invoice_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> dict[str, Any]:
    row = database.get_invoice(conn, invoice_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"发票 {invoice_id} 不存在")
    return _row_to_result(row, database.get_lines(conn, invoice_id))


@app.get(
    "/api/v1/invoices",
    response_model=PagedInvoices,
    tags=["invoices"],
)
def list_invoices(
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数，1~100"),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    rows, total = database.list_invoices(conn, page, page_size)
    items: list[dict[str, Any]] = []
    for row in rows:
        count = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM invoice_lines WHERE invoice_id = ?",
                (row["id"],),
            ).fetchone()["c"]
        )
        items.append(
            {
                "id": row["id"],
                "currency": row["currency"],
                "target_currency": row["target_currency"],
                "total_net": row["total_net"],
                "total_tax": row["total_tax"],
                "total_gross": row["total_gross"],
                "line_count": count,
                "created_at": row["created_at"],
            }
        )
    pages = (total + page_size - 1) // page_size if total else 0
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": pages,
    }


@app.post(
    "/api/v1/invoices/{invoice_id}/payments",
    response_model=PaymentResult,
    status_code=201,
    tags=["payments"],
)
def register_payment(
    invoice_id: int,
    payload: PaymentCreate,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    amount = to_decimal(payload.amount, "amount")
    if amount <= 0:
        # 走全局 ValueError -> 422 处理器
        raise ValueError(f"收款金额必须大于 0，收到 {payload.amount!r}")
    try:
        return database.register_payment(conn, invoice_id, amount)
    except database.InvoiceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/api/v1/payment-allocations",
    response_model=PaymentAllocationResult,
    status_code=201,
    tags=["payments"],
)
def allocate_payment(
    payload: PaymentAllocationCreate,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """一笔来款在多张待结发票间按到期日顺序分配，整笔成功或整笔拒绝。"""

    amount = to_decimal(payload.amount, "amount")
    if amount <= 0:
        # 走全局 ValueError -> 422 处理器
        raise ValueError(f"来款金额必须大于 0，收到 {payload.amount!r}")
    try:
        return database.allocate_payment(
            conn, payload.currency, amount, list(payload.invoice_ids)
        )
    except database.InvoiceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(
    "/api/v1/invoices/{invoice_id}/payments",
    response_model=PaymentOverview,
    tags=["payments"],
)
def read_payments(
    invoice_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> dict[str, Any]:
    row = database.get_invoice(conn, invoice_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"发票 {invoice_id} 不存在")
    return database.payment_overview(conn, row)


@app.get(
    "/api/v1/invoices/{invoice_id}/payment-snapshot",
    response_model=PaymentSnapshot,
    tags=["payments"],
)
def read_payment_snapshot(
    invoice_id: int,
    at: str = Query(
        ...,
        description="历史快照截止时点，必填，UTC 秒精度（YYYY-MM-DDTHH:MM:SSZ）；"
        "截止秒为闭区间，同秒的收款与冲销全部计入",
    ),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """单张发票在指定 UTC 时点的历史收款快照。

    只统计 ``created_at <= at``（含当秒）的收款与对应冲销，按发票币种最小
    单位用 Decimal 计算收款总额、冲销总额、净已收与未收余额；截止后的流水
    不参与，历史结果可重复复现。发票不存在返回 404；时间格式错误或时点早于
    发票创建时间返回 422（均走全局 ValueError -> 422 处理器）。
    """

    row = database.get_invoice(conn, invoice_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"发票 {invoice_id} 不存在")
    cutoff = parse_utc_second(at, "at")
    return database.payment_snapshot(conn, row, cutoff)


@app.post(
    "/api/v1/payments/{payment_id}/reversals",
    response_model=ReversalResult,
    status_code=201,
    tags=["payments"],
)
def create_reversal(
    payment_id: int,
    payload: ReversalCreate,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """冲销一笔收款：可同一笔分次冲销，累计不得超过原收款金额。

    冲销记录单独持久化（不删除原收款）；未收余额、逾期查询与后续来款分配
    均按净入账额（收款 − 冲销）计算。
    """

    amount = to_decimal(payload.amount, "amount")
    if amount <= 0:
        # 走全局 ValueError -> 422 处理器
        raise ValueError(f"冲销金额必须大于 0，收到 {payload.amount!r}")
    try:
        return database.reverse_payment(conn, payment_id, amount)
    except database.PaymentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---- 分期回款计划 ----


@app.post(
    "/api/v1/invoices/{invoice_id}/installment-plan",
    response_model=InstallmentPlanResult,
    status_code=201,
    tags=["installment-plans"],
)
def create_installment_plan(
    invoice_id: int,
    payload: InstallmentPlanCreate,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """为单张发票创建唯一一份分期回款计划。

    提交按期次顺序（到期日升序，允许同日多期）排列的期次与金额；每张发票
    只允许一份计划，期次金额须为发票币种最小单位的正数且合计等于发票含税
    总额。发票存在性、重复计划、金额与合计校验及计划头/期次写入在**同一个
    ``BEGIN IMMEDIATE`` 事务**内完成，任一校验失败整体回滚、不留任何计划行。
    发票不存在返回 404，已存在计划返回 409，金额/顺序/合计错误返回 422。
    """

    installments = [
        (item.due_date, to_decimal(item.amount, f"第 {idx} 期 amount"))
        for idx, item in enumerate(payload.installments, start=1)
    ]
    try:
        return database.create_installment_plan(conn, invoice_id, installments)
    except database.InvoiceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except database.PlanAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get(
    "/api/v1/invoices/{invoice_id}/installment-plan",
    response_model=InstallmentPlanResult,
    tags=["installment-plans"],
)
def read_installment_plan(
    invoice_id: int,
    as_of: str | None = Query(
        None,
        description="查询日期 ISO 8601（YYYY-MM-DD）；缺省取 UTC 当前日期。"
        "仅到期日严格早于该日的未覆盖额计为逾期额",
    ),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """查询分期回款计划：把当前净已收额（收款 − 冲销）按期次顺序逐期抵扣。

    每期返回已覆盖额 ``covered``、未覆盖额 ``uncovered`` 与逾期额
    ``overdue``（仅到期日严格早于查询日期的未覆盖额计入）。抵扣为纯读计算，
    **不改写原计划**：冲销导致净已收回降后再次查询，较后期次会重新打开。
    发票不存在或发票尚无计划均返回 404。
    """

    row = database.get_invoice(conn, invoice_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"发票 {invoice_id} 不存在")
    plan = database.get_installment_plan(conn, invoice_id)
    if plan is None:
        raise HTTPException(
            status_code=404,
            detail=f"发票 {invoice_id} 尚未创建分期回款计划",
        )

    if as_of is None:
        cutoff = datetime.now(timezone.utc).date()
    else:
        # 非法日期走全局 ValueError -> 422 处理器
        cutoff = parse_iso_date(as_of, "as_of")

    item_rows = database.get_installment_plan_items(conn, int(plan["id"]))
    return database.installment_plan_status(
        conn, row, int(plan["id"]), plan["created_at"], item_rows, cutoff
    )


# ---- 原发票贷项凭证 ----


@app.post(
    "/api/v1/invoices/{invoice_id}/credit-memos",
    response_model=CreditMemoResult,
    status_code=201,
    tags=["credit-memos"],
)
def create_credit_memo(
    invoice_id: int,
    payload: CreditMemoCreate,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """对已保存发票的正净额行提交一张可含多行的部分抵扣贷项凭证。

    每行给出 ``line_no`` 与本次抵扣净额 ``net``；同一凭证内行号不得重复，
    净额必须是发票币种最小单位的正整数倍，引用的原行必须存在且原净额为正，
    同一原行历次抵扣净额合计不得超过原净额。每行本次抵扣税额按

    ``round(原行已确认税额 × 抵扣后累计净额 / 原净额) − 历次已记税额``

    计算（HALF_UP 到币种最小单位）；**完全抵扣时累计税额恰好等于原行已确认
    税额**，即原行税额被恰好抵完。发票存在性、行号/金额/累计超额校验与凭证头、
    凭证行写入在**同一个 ``BEGIN IMMEDIATE`` 事务**内完成，任一校验失败整体
    回滚、不留任何凭证行。发票不存在返回 404，其余校验错误返回 422。

    贷项凭证只追加到独立的表：**原发票（逐行净额/税额/含税额、汇总）与收款流水
    （收款、冲销、未收余额、逾期、分期计划视图）全部保持原样**。
    """

    items = [
        (item.line_no, to_decimal(item.net, f"lines[{idx}].net"))
        for idx, item in enumerate(payload.lines, start=1)
    ]
    try:
        return database.create_credit_memo(conn, invoice_id, items)
    except database.InvoiceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(
    "/api/v1/invoices/{invoice_id}/credit-memos",
    response_model=CreditMemoOverview,
    tags=["credit-memos"],
)
def list_credit_memos(
    invoice_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """按原发票查询全部贷项凭证（按凭证创建顺序），并给出逐原行累计抵扣。

    每张凭证返回各行本次净额、税额、含税额以及抵扣后该原行的累计抵扣净额/税额；
    顶层 ``line_credits`` 按原行号汇总当前累计抵扣净额/税额/含税额与剩余净额。
    纯读接口：原发票与收款流水保持原样，无凭证时 ``credit_count`` 为 0、
    ``memos`` 与 ``line_credits`` 为空列表。发票不存在返回 404。
    """

    row = database.get_invoice(conn, invoice_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"发票 {invoice_id} 不存在")
    return database.credit_memo_overview(conn, row)
