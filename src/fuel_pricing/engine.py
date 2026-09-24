"""汽油指导价的确定性计算引擎。

所有金额均使用 Decimal，中间过程不做量化，仅在输出指导价时按 ROUND_HALF_UP
保留两位小数；不足最低变动门槛或触及地板/天花板价时本周期暂缓，未生效金额
全额结转入下一周期。引擎是纯函数，输入相同则输出必然相同。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

ZERO = Decimal("0")
MONEY = Decimal("0.01")
# 1 桶的升数，物理常量，不随政策版本变化。
LITRES_PER_BARREL = Decimal("158.9873")


class EngineError(ValueError):
    """输入数据不足以完成确定性计算。"""


def money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def text(value: Decimal) -> str:
    return format(value, "f")


def is_workday(day: date) -> bool:
    return day.weekday() < 5


def shift_workdays(day: date, steps: int) -> date:
    """按工作日推进 steps 个工作日（steps 可为负数）。"""

    if steps == 0:
        return day
    direction = 1 if steps > 0 else -1
    remaining = abs(steps)
    current = day
    while remaining:
        current += timedelta(days=direction)
        if is_workday(current):
            remaining -= 1
    return current


def window_workdays(evaluation_date: date, window: int) -> list[date]:
    """评估日之前（不含当日）最近 window 个工作日，按时间升序。"""

    if window <= 0:
        raise EngineError("报价窗口工作日数必须大于零")
    dates: list[date] = []
    cursor = evaluation_date
    for _ in range(window):
        cursor = shift_workdays(cursor, -1)
        dates.append(cursor)
    return list(reversed(dates))


def cycle_dates(anchor: date, cycle_workdays: int, evaluation_date: date) -> tuple[bool, date | None, date]:
    """以 anchor 为首个周期日，按工作日间隔推导评估日所属周期。

    返回 (是否为周期日, 上一周期日, 评估日对齐到的周期日)。
    评估日早于锚点时对齐周期日为锚点且不视为周期日。
    """

    if cycle_workdays <= 0:
        raise EngineError("调整周期工作日数必须大于零")
    if evaluation_date < anchor:
        return False, None, anchor
    index = 0
    current = anchor
    while current < evaluation_date:
        current = shift_workdays(current, cycle_workdays)
        index += 1
    aligned = current
    previous = anchor if index == 0 else shift_workdays(current, -cycle_workdays)
    return current == evaluation_date, previous, aligned


def _quote_rows(quotes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for quote in quotes:
        rows.append(
            {
                "trade_date": str(quote["trade_date"]),
                "quote_id": int(quote["quote_id"]),
                "source_revision": str(quote["source_revision"]),
                "close_usd": text(Decimal(str(quote["close_usd"]))),
            }
        )
    return sorted(rows, key=lambda item: item["trade_date"])


def evaluate_grade(
    *,
    rule_document: Mapping[str, Any],
    grade: str,
    evaluation_date: str,
    window_dates: Sequence[str],
    quotes: Sequence[Mapping[str, Any]],
    previous_price: Decimal,
    carry_in: Decimal,
) -> dict[str, Any]:
    """计算单个牌号在某评估日的确定结果。

    quotes 必须恰好覆盖 window_dates 中每个工作日（每交易日一条锁定修订），
    否则抛出 EngineError。
    """

    products = {item["grade"]: item for item in rule_document["products"]}
    if grade not in products:
        raise EngineError(f"规则快照缺少 {grade} 号汽油参数")
    product = products[grade]
    expected = list(window_dates)
    rows = _quote_rows(quotes)
    actual_dates = [row["trade_date"] for row in rows]
    if actual_dates != expected:
        raise EngineError(f"报价窗口不完整：期望 {expected}，实际 {actual_dates}")

    fx = Decimal(str(rule_document["fx_rate_cny_per_usd"]))
    closes = [Decimal(row["close_usd"]) for row in rows]
    avg_close = sum(closes, ZERO) / Decimal(len(closes))
    crude_cost = avg_close * fx / LITRES_PER_BARREL
    spread = Decimal(str(product["processing_spread"]))
    cost_base = crude_cost + spread

    fixed_sum = ZERO
    rate_sum = ZERO
    tax_rows: list[dict[str, Any]] = []
    for component in product["tax_components"]:
        kind = component["kind"]
        value = Decimal(str(component["value"]))
        if kind == "fixed":
            base_name = None
            amount = value
            fixed_sum += amount
        else:
            base_name = component["base"]
            if base_name == "cost":
                base_amount = cost_base
            elif base_name == "cost_plus_fixed":
                base_amount = cost_base + fixed_sum
            elif base_name == "tax":
                base_amount = fixed_sum + rate_sum
            else:  # 模型层已拦截，防御性处理
                raise EngineError(f"未知计税基础 {base_name}")
            amount = value * base_amount
            rate_sum += amount
        tax_rows.append(
            {
                "name": component["name"],
                "kind": kind,
                "base": base_name,
                "amount_cny": text(money(amount)),
            }
        )

    theoretical_raw = cost_base + fixed_sum + rate_sum
    theoretical = money(theoretical_raw)
    previous_price = money(previous_price)
    carry_in = money(carry_in)
    raw_change = money(theoretical - previous_price)
    cumulative = money(carry_in + raw_change)

    floor_price = None if product.get("floor_price") is None else money(Decimal(str(product["floor_price"])))
    ceiling_price = None if product.get("ceiling_price") is None else money(Decimal(str(product["ceiling_price"])))
    threshold = money(Decimal(str(product["min_change_threshold"])))
    unbounded = money(previous_price + cumulative)

    defer_reason: str | None = None
    if floor_price is not None and unbounded < floor_price:
        defer_reason = "floor_frozen"
    elif ceiling_price is not None and unbounded > ceiling_price:
        defer_reason = "ceiling_frozen"
    elif abs(cumulative) < threshold:
        defer_reason = "below_threshold"

    if defer_reason is not None:
        deferred = True
        guide_price = previous_price
        applied_change = ZERO
        carry_out = cumulative
    else:
        deferred = False
        guide_price = unbounded
        applied_change = cumulative
        carry_out = ZERO

    return {
        "grade": grade,
        "evaluation_date": evaluation_date,
        "window_start": expected[0],
        "window_end": expected[-1],
        "window_trade_dates": expected,
        "locked_quotes": rows,
        "avg_close_usd": text(avg_close),
        "fx_rate_cny_per_usd": text(fx),
        "litres_per_barrel": text(LITRES_PER_BARREL),
        "crude_cost_cny": text(money(crude_cost)),
        "processing_spread_cny": text(money(spread)),
        "cost_base_cny": text(money(cost_base)),
        "taxes": tax_rows,
        "tax_total_cny": text(money(fixed_sum + rate_sum)),
        "theoretical_price_cny": text(theoretical),
        "previous_guide_price_cny": text(previous_price),
        "raw_change_cny": text(raw_change),
        "carry_in_cny": text(carry_in),
        "cumulative_change_cny": text(cumulative),
        "deferred": deferred,
        "defer_reason": defer_reason,
        "applied_change_cny": text(money(applied_change)),
        "guide_price_cny": text(money(guide_price)),
        "carry_out_cny": text(money(carry_out)),
    }
