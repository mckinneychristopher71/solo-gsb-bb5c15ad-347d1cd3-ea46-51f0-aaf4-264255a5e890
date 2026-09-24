"""发票税额计算核心。

规则（固定且可复算）：

1. 每行税额 = 不含税净额 × 税率，全程使用 ``Decimal`` 高精度运算（精度 50 位），
   不出现任何 float。
2. 每行含税 / 税额按 **币种最小单位** 做 ``ROUND_HALF_UP``（四舍五入）。
3. 发票总税额 = Σ(各行精确税额) 后，再按币种最小单位做 HALF_UP；
   它与「各行已舍入税额之和」之间的差即为 *残差*（最小单位整数个，
   必有 ``|残差| <= 行数``）。
4. 残差按 **最大余数法（Hare quota / largest remainder）** 分配，
   排序完全确定，任何时候重新计算结果一致：

   * 残差 > 0（需补分到行）：按「精确税额超出其已舍入值的零头」从大到小排序，
     每行 +1 个最小单位，分完为止；
   * 残差 < 0（需从行扣减）：按同一零头从小到大排序，每行 -1 个最小单位。
   * 零头相同按行在请求中的顺序（line_no）排前的优先，保证结果确定。

   最终各行税额之和恰好等于总税额，残差被完全分配。

5. 跨币种：当 ``target_currency`` 与发票币种不同时，必须提供
   ``exchange_rate``（标价方式：1 单位发票币种 = exchange_rate 单位目标币种）。
   原币金额原样保留；目标币每行净额/税额同样按目标币种最小单位 HALF_UP，
   并在目标币上再做一次相同的残差分配。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, localcontext, getcontext
from typing import Any, Iterable

from .currency import UnknownCurrencyError, currency_digits

# 全程高精度，避免中间过程被截断
getcontext().prec = 50

RULE_ID = "half_up_plus_largest_remainder_v1"

_ZERO = Decimal("0")
_QUANT_CACHE: dict[int, Decimal] = {}


def _unit(digits: int) -> Decimal:
    """某币种的 1 个最小单位，例如 digits=2 -> Decimal('0.01')。"""

    if digits not in _QUANT_CACHE:
        _QUANT_CACHE[digits] = Decimal(1).scaleb(-digits)
    return _QUANT_CACHE[digits]


def _quantize(value: Decimal, digits: int) -> Decimal:
    """按币种最小单位做 ROUND_HALF_UP 舍入。"""

    return value.quantize(_unit(digits), rounding=ROUND_HALF_UP)


def to_decimal(value: Any, field: str) -> Decimal:
    """把入参（字符串/int/Decimal）安全转成 Decimal；禁止 float。"""

    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool 是 int 子类，显式拒绝
        raise ValueError(f"{field} 必须是十进制数字字符串，不能是布尔值")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field} 不能为空")
        try:
            return Decimal(text)
        except Exception as exc:  # InvalidOperation 等
            raise ValueError(f"{field} 不是合法的十进制数字: {value!r}") from exc
    raise ValueError(f"{field} 必须以字符串形式提交十进制数字，收到: {type(value).__name__}")


@dataclass(frozen=True)
class Allocation:
    """某一币种维度上的残差分配结果。"""

    rounded_taxes: list[Decimal]      # 分配后各行税额（最小单位整数倍）
    line_totals: list[Decimal]        # 分配后各行含税总额
    total_tax: Decimal                # 发票总税额（按最小单位舍入）
    total_net: Decimal                # 不含税总额（输入即最小单位整数倍）
    total_gross: Decimal              # 含税总额 = total_net + total_tax
    residual: int                     # 残差（最小单位个数，正负整数）
    residual_unit: str                # 最小单位，如 '0.01'
    allocations: list[dict[str, Any]] # 可审计的逐行分配明细


def _allocate(
    nets: list[Decimal],
    exact_taxes: list[Decimal],
    digits: int,
) -> Allocation:
    """逐行舍入后，把总税额残差按最大余数法分配回各行。"""

    unit = _unit(digits)
    n = len(nets)

    rounded = [_quantize(t, digits) for t in exact_taxes]
    # 零头：精确税额超过「向下取整到最小单位」的部分，落在 [0, unit)
    floors = [ (t / unit).to_integral_value(rounding="ROUND_FLOOR") * unit for t in exact_taxes ]
    remainders = [exact_taxes[i] - floors[i] for i in range(n)]

    sum_rounded = sum(rounded, _ZERO)
    exact_total = sum(exact_taxes, _ZERO)
    total_tax = _quantize(exact_total, digits)
    residual_q = total_tax - sum_rounded  # 必为最小单位整数倍
    residual = int(residual_q / unit)

    if not (-n <= residual <= n):
        # 数学上不可能；作为不变量防御
        raise AssertionError(f"残差 {residual} 超出行数 {n}")

    final_taxes = list(rounded)
    allocations: list[dict[str, Any]] = []
    if residual > 0:
        order = sorted(range(n), key=lambda i: (-remainders[i], i))
        for rank, idx in enumerate(order[:residual]):
            final_taxes[idx] += unit
            allocations.append(
                {
                    "line_no": idx + 1,
                    "delta_units": 1,
                    "remainder": _canon(exact_taxes[idx] - floors[idx]),
                    "rank": rank + 1,
                }
            )
    elif residual < 0:
        order = sorted(range(n), key=lambda i: (remainders[i], i))
        for rank, idx in enumerate(order[: -residual]):
            final_taxes[idx] -= unit
            allocations.append(
                {
                    "line_no": idx + 1,
                    "delta_units": -1,
                    "remainder": _canon(exact_taxes[idx] - floors[idx]),
                    "rank": rank + 1,
                }
            )

    line_totals = [nets[i] + final_taxes[i] for i in range(n)]
    total_net = sum(nets, _ZERO)
    total_gross = total_net + total_tax

    # 不变量：行税额之和必须严格等于总税额
    if sum(final_taxes, _ZERO) != total_tax:
        raise AssertionError("残差分配后行税额之和与总税额不一致")

    return Allocation(
        rounded_taxes=final_taxes,
        line_totals=line_totals,
        total_tax=total_tax,
        total_net=total_net,
        total_gross=total_gross,
        residual=residual,
        residual_unit=_s(unit),
        allocations=allocations,
    )


def _s(value: Decimal) -> str:
    """Decimal -> 字符串，保留有效位且不带指数（如 0.005 -> '0.005'）。"""

    return format(value, "f")


def _canon(value: Decimal) -> str:
    """规范化展示：去掉无意义尾随零（0.0050 -> '0.005'，0.10 -> '0.1'）。

    仅用于 *精确值*（exact_tax、余数审计字段）；已按最小单位舍入的金额仍用
    :func:`_s` 保留固定位数（如税额 '1.00'）。
    """

    normalized = value - _ZERO  # -0 归一成 +0
    return format(normalized.normalize(), "f")


def _validate_lines(raw_lines: Iterable[Any]) -> list[tuple[str | None, Decimal, Decimal]]:
    """校验并规范化行项目，返回 (description, net, rate) 列表。"""

    lines = list(raw_lines)
    if not lines:
        raise ValueError("至少需要一行发票行 (lines 不能为空)")

    result: list[tuple[str | None, Decimal, Decimal]] = []
    for idx, line in enumerate(lines, start=1):
        description = line.get("description") if isinstance(line, dict) else None
        net = to_decimal(
            line.get("net") if isinstance(line, dict) else getattr(line, "net", None),
            f"lines[{idx}].net",
        )
        if net == _ZERO:
            raise ValueError(f"lines[{idx}].net 不能为 0")
        rate = to_decimal(
            line.get("tax_rate") if isinstance(line, dict) else getattr(line, "tax_rate", None),
            f"lines[{idx}].tax_rate",
        )
        if not (_ZERO <= rate <= 1):
            raise ValueError(f"lines[{idx}].tax_rate 必须在 0~1 之间（例如 0.13 表示 13%）")
        result.append((description, net, rate))
    return result


def compute_invoice(payload: dict[str, Any]) -> dict[str, Any]:
    """执行完整计算，返回可直接 JSON 序列化（金额全部为字符串）的结果字典。

    该函数是纯函数：同样输入必然得到同样输出，持久化与复算都以它为准。
    """

    currency = str(payload["currency"]).strip().upper()
    if not currency:
        raise ValueError("currency 不能为空")
    try:
        digits = currency_digits(currency)
    except UnknownCurrencyError:
        raise

    lines_in = _validate_lines(payload.get("lines", []))
    n = len(lines_in)

    # 净额必须是币种最小单位的整数倍（发票上不存在更小单位）；
    # 通过后统一 quantize 一次，使展示/存储固定为币种位数（1.5 -> 1.50）
    unit = _unit(digits)
    nets: list[Decimal] = []
    normalized_lines: list[tuple[str | None, Decimal, Decimal]] = []
    for idx, (description, net, rate) in enumerate(lines_in, start=1):
        if (net / unit) != (net / unit).to_integral_value():
            raise ValueError(
                f"lines[{idx}].net={_s(net)} 不是 {currency} 最小单位 {_s(unit)} 的整数倍"
            )
        net = net.quantize(unit)
        nets.append(net)
        normalized_lines.append((description, net, rate))
    lines_in = normalized_lines

    with localcontext() as ctx:
        ctx.prec = 50

        exact_taxes = [nets[i] * lines_in[i][2] for i in range(n)]
        alloc = _allocate(nets, exact_taxes, digits)

        # ---- 跨币种换算 ----
        target_currency = payload.get("target_currency")
        fx_section: dict[str, Any] | None = None
        converted_nets: list[Decimal] | None = None
        converted_alloc: Allocation | None = None
        exchange_rate: Decimal | None = None

        if target_currency is not None:
            target_currency = str(target_currency).strip().upper()
            if not target_currency:
                raise ValueError("target_currency 不能为空字符串")
            if target_currency == currency:
                raise ValueError("target_currency 与 currency 相同，无需换算")
            if payload.get("exchange_rate") is None:
                raise ValueError("跨币种换算必须提供 exchange_rate")
            exchange_rate = to_decimal(payload["exchange_rate"], "exchange_rate")
            if exchange_rate <= _ZERO:
                raise ValueError("exchange_rate 必须为正数")
            target_digits = currency_digits(target_currency)

            converted_nets = [
                _quantize(nets[i] * exchange_rate, target_digits) for i in range(n)
            ]
            # 目标币逐行精确税额按「原币净额 × 税率 × 汇率」口径计算，
            # 然后在目标币种上独立做一次逐行舍入 + 残差分配
            converted_exact_taxes = [
                nets[i] * lines_in[i][2] * exchange_rate for i in range(n)
            ]
            converted_alloc = _allocate(
                converted_nets, converted_exact_taxes, target_digits
            )

            fx_section = {
                "target_currency": target_currency,
                "target_digits": target_digits,
                "exchange_rate": _s(exchange_rate),
                "rate_direction": f"1 {currency} = {_s(exchange_rate)} {target_currency}",
                "total_net": _s(converted_alloc.total_net),
                "total_tax": _s(converted_alloc.total_tax),
                "total_gross": _s(converted_alloc.total_gross),
                "residual_units": converted_alloc.residual,
                "residual_unit": converted_alloc.residual_unit,
                "allocations": converted_alloc.allocations,
            }

    result_lines: list[dict[str, Any]] = []
    for i, (description, net, rate) in enumerate(lines_in):
        line_out: dict[str, Any] = {
            "line_no": i + 1,
            "description": description,
            "tax_rate": _s(rate),
            "currency": currency,
            "net": _s(net),
            "exact_tax": _canon(exact_taxes[i]),
            "tax": _s(alloc.rounded_taxes[i]),
            "gross": _s(alloc.line_totals[i]),
        }
        if converted_alloc is not None:
            line_out["converted"] = {
                "currency": target_currency,
                "net": _s(converted_nets[i]),
                "tax": _s(converted_alloc.rounded_taxes[i]),
                "gross": _s(
                    converted_nets[i] + converted_alloc.rounded_taxes[i]
                ),
            }
        result_lines.append(line_out)

    result: dict[str, Any] = {
        "rule_id": RULE_ID,
        "currency": currency,
        "currency_digits": digits,
        "lines": result_lines,
        "totals": {
            "currency": currency,
            "total_net": _s(alloc.total_net),
            "total_tax": _s(alloc.total_tax),
            "total_gross": _s(alloc.total_gross),
        },
        "rounding": {
            "mode": "ROUND_HALF_UP",
            "minor_unit": alloc.residual_unit,
            "residual_units": alloc.residual,
            "residual_amount": _s(alloc.residual * _unit(digits)),
            "method": "largest_remainder",
            "allocations": alloc.allocations,
        },
    }
    if fx_section is not None:
        result["fx"] = fx_section
    return result


def recompute_matches(stored: dict[str, Any], payload: dict[str, Any]) -> bool:
    """用原始请求重新计算，与已存结果逐字段比对，用于 GET 时的可复算校验。"""

    fresh = compute_invoice(payload)
    return fresh == stored
