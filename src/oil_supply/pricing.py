"""汽油指导价政策规则与调价窗口的确定性计算。

本模块只做纯计算，不访问数据库。政策规则按版本整体快照传入，
四舍五入（ROUND_HALF_UP，到分）、暂缓调整和跨周期累计的规则全部显式
写在 :class:`PricingRule` 中，因此同一组输入永远得到同一结果。

价格链条（每升含税费指导价）::

    原油篮子价（美元/桶）× 产品折算系数 + 加工价差 + 定额消费税
    所得金额再乘以（1 + 增值税率）

折算系数已经吸收汇率与桶升折算，系数本身按政策日期变化即代表汇率或
收率口径变化。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence

from .errors import ValidationFailed
from .planning import decimal_text, digest

GASOLINE_PRODUCTS = ("gasoline-92", "gasoline-95")
CRUDE_INDEXES = ("BRENT", "WTI", "DUBAI", "ESPO", "URAL")

ZERO = Decimal("0")
CENT = Decimal("0.01")
HUNDRED = Decimal("100")


def quantize_price(value: Decimal) -> Decimal:
    """汽油指导价一律四舍五入到 0.01 元/升。"""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _decimal(raw: object, field: str, *, minimum: Decimal | None = None) -> Decimal:
    if isinstance(raw, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        value = Decimal(str(raw))
    except Exception as exc:  # InvalidOperation/ValueError 统一成校验错误
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not value.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and value < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class ProductRule:
    """单个牌号的加工与税费口径。"""

    product: str
    conversion_factor: Decimal
    processing_margin_per_liter: Decimal
    consumption_tax_per_liter: Decimal

    @classmethod
    def from_dict(cls, product: str, raw: Mapping[str, object]) -> "ProductRule":
        if product not in GASOLINE_PRODUCTS:
            raise ValidationFailed("只支持 gasoline-92 与 gasoline-95")
        if not isinstance(raw, Mapping):
            raise ValidationFailed(f"{product} 规则必须是对象")
        return cls(
            product=product,
            conversion_factor=_decimal(raw.get("conversion_factor"), f"{product}.conversion_factor", minimum=Decimal("0.000001")),
            processing_margin_per_liter=_decimal(
                raw.get("processing_margin_per_liter"),
                f"{product}.processing_margin_per_liter",
                minimum=ZERO,
            ),
            consumption_tax_per_liter=_decimal(
                raw.get("consumption_tax_per_liter"),
                f"{product}.consumption_tax_per_liter",
                minimum=ZERO,
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "conversion_factor": decimal_text(self.conversion_factor),
            "processing_margin_per_liter": decimal_text(self.processing_margin_per_liter),
            "consumption_tax_per_liter": decimal_text(self.consumption_tax_per_liter),
        }


@dataclass(frozen=True, slots=True)
class PricingRule:
    """某一生效区间内完整的汽油调价政策快照。"""

    index_weights: Mapping[str, Decimal]
    window_sessions: int
    change_threshold_per_liter: Decimal
    vat_rate: Decimal
    suspend_basket_ceiling_usd: Decimal | None
    suspend_basket_floor_usd: Decimal | None
    carry_below_threshold: bool
    carry_on_suspend: bool
    products: Mapping[str, ProductRule]
    payload: Mapping[str, object]

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "PricingRule":
        if not isinstance(raw, Mapping):
            raise ValidationFailed("政策规则必须是对象")
        weights_raw = raw.get("index_weights")
        if not isinstance(weights_raw, Mapping) or not weights_raw:
            raise ValidationFailed("index_weights 必须是非空对象")
        weights: dict[str, Decimal] = {}
        for index, weight in weights_raw.items():
            name = str(index).upper()
            if name not in CRUDE_INDEXES:
                raise ValidationFailed(f"不支持的原油基准 {name}")
            value = _decimal(weight, f"index_weights.{name}", minimum=Decimal("0.0001"))
            weights[name] = value
        sessions_raw = raw.get("window_sessions")
        if isinstance(sessions_raw, bool) or not isinstance(sessions_raw, int) or not 2 <= sessions_raw <= 60:
            raise ValidationFailed("window_sessions 必须是 2 到 60 的整数")
        threshold = _decimal(
            raw.get("change_threshold_per_liter"),
            "change_threshold_per_liter",
            minimum=ZERO,
        )
        vat_rate = _decimal(raw.get("vat_rate", "0.13"), "vat_rate", minimum=ZERO)
        if vat_rate > 1:
            raise ValidationFailed("vat_rate 不能大于 1")
        ceiling_raw = raw.get("suspend_basket_ceiling_usd")
        ceiling = None if ceiling_raw is None else _decimal(
            ceiling_raw, "suspend_basket_ceiling_usd", minimum=Decimal("0.01")
        )
        floor_raw = raw.get("suspend_basket_floor_usd")
        floor = None if floor_raw is None else _decimal(
            floor_raw, "suspend_basket_floor_usd", minimum=ZERO
        )
        if ceiling is not None and floor is not None and floor >= ceiling:
            raise ValidationFailed("suspend_basket_floor_usd 必须低于 suspend_basket_ceiling_usd")
        products_raw = raw.get("products")
        if not isinstance(products_raw, Mapping):
            raise ValidationFailed("products 必须是对象")
        missing = [name for name in GASOLINE_PRODUCTS if name not in products_raw]
        if missing:
            raise ValidationFailed(f"缺少牌号规则: {', '.join(missing)}")
        extra = [name for name in products_raw if name not in GASOLINE_PRODUCTS]
        if extra:
            raise ValidationFailed(f"不支持的牌号: {', '.join(extra)}")
        products = {name: ProductRule.from_dict(name, products_raw[name]) for name in GASOLINE_PRODUCTS}

        def _flag(name: str, default: bool) -> bool:
            value = raw.get(name, default)
            if not isinstance(value, bool):
                raise ValidationFailed(f"{name} 必须是布尔值")
            return value

        payload = {
            "index_weights": {name: decimal_text(weights[name]) for name in sorted(weights)},
            "window_sessions": sessions_raw,
            "change_threshold_per_liter": decimal_text(threshold),
            "vat_rate": decimal_text(vat_rate),
            "suspend_basket_ceiling_usd": None if ceiling is None else decimal_text(ceiling),
            "suspend_basket_floor_usd": None if floor is None else decimal_text(floor),
            "carry_below_threshold": _flag("carry_below_threshold", True),
            "carry_on_suspend": _flag("carry_on_suspend", False),
            "products": {name: products[name].as_dict() for name in GASOLINE_PRODUCTS},
        }
        return cls(
            index_weights=weights,
            window_sessions=sessions_raw,
            change_threshold_per_liter=threshold,
            vat_rate=vat_rate,
            suspend_basket_ceiling_usd=ceiling,
            suspend_basket_floor_usd=floor,
            carry_below_threshold=payload["carry_below_threshold"],
            carry_on_suspend=payload["carry_on_suspend"],
            products=products,
            payload=payload,
        )

    @property
    def sha256(self) -> str:
        return digest(self.payload)

    def normalized_weights(self) -> dict[str, Decimal]:
        total = sum(self.index_weights.values(), ZERO)
        return {name: weight / total for name, weight in self.index_weights.items()}


@dataclass(frozen=True, slots=True)
class QuotePoint:
    """计算窗口实际锁定的一条报价修订。"""

    price_index: str
    trade_date: str
    quote_id: int
    close_usd: Decimal

    def as_dict(self) -> dict[str, object]:
        return {
            "price_index": self.price_index,
            "trade_date": self.trade_date,
            "quote_id": self.quote_id,
            "close_usd": decimal_text(self.close_usd),
        }


@dataclass(frozen=True, slots=True)
class ProductWindowResult:
    product: str
    expected_per_liter: Decimal
    reference_per_liter: Decimal
    current_per_liter: Decimal
    incoming_change: Decimal
    raw_change: Decimal
    candidate_change: Decimal
    applied_change: Decimal
    threshold_per_liter: Decimal
    decision: str  # moved / held / suspended
    resulting_per_liter: Decimal
    outgoing_change: Decimal

    def as_dict(self) -> dict[str, object]:
        return {
            "expected_per_liter": decimal_text(self.expected_per_liter),
            "reference_per_liter": decimal_text(self.reference_per_liter),
            "current_per_liter": decimal_text(self.current_per_liter),
            "incoming_change": decimal_text(self.incoming_change),
            "raw_change": decimal_text(self.raw_change),
            "candidate_change": decimal_text(self.candidate_change),
            "applied_change": decimal_text(self.applied_change),
            "threshold_per_liter": decimal_text(self.threshold_per_liter),
            "decision": self.decision,
            "resulting_per_liter": decimal_text(self.resulting_per_liter),
            "outgoing_change": decimal_text(self.outgoing_change),
        }


def _average(points: Sequence[QuotePoint]) -> Decimal:
    return sum((point.close_usd for point in points), ZERO) / Decimal(len(points))


def evaluate_window(
    rule: PricingRule,
    quotes: Mapping[str, Sequence[QuotePoint]],
    current_prices: Mapping[str, Decimal],
    incoming_changes: Mapping[str, Decimal],
    anchor_date: str,
    reference_prices: Mapping[str, Decimal | None] | None = None,
) -> dict[str, object]:
    """对一个调价锚定日执行纯函数计算。

    价格链条按锚定日生效的规则快照从原油篮子推导为含税费的目标价
    （``expected_per_liter``）。每个牌号的周期增量是目标价相对上一窗口
    目标价（``reference_per_liter``；首轮取窗口开始时的现行价）的差额，
    与上周期结转的未调金额相加得到 ``candidate_change``：

    - 绝对额达到门槛：调 ``candidate_change``（四舍五入到分），结转清零；
    - 低于门槛：暂缓，未调金额按 ``carry_below_threshold`` 决定是否结转；
    - 触发调控上下限：全部牌号暂缓，按 ``carry_on_suspend`` 决定结转。
    """
    date.fromisoformat(anchor_date)  # 校验锚定日格式
    reference_prices = reference_prices or {}
    weighted: list[tuple[str, Decimal]] = []
    all_points: list[QuotePoint] = []
    for name, weight in rule.normalized_weights().items():
        points = quotes.get(name, ())
        if len(points) != rule.window_sessions:
            raise ValidationFailed(
                f"{name} 在 {anchor_date} 前只有 {len(points)} 个交易日报价，需要 {rule.window_sessions} 个"
            )
        dates = sorted(point.trade_date for point in points)
        if dates[-1] > anchor_date:
            raise ValidationFailed("报价交易日不能晚于锚定日")
        weighted.append((name, _average(points) * weight))
        all_points.extend(points)
    basket = sum((value for _, value in weighted), ZERO)
    suspended_ceiling = rule.suspend_basket_ceiling_usd is not None and basket >= rule.suspend_basket_ceiling_usd
    suspended_floor = rule.suspend_basket_floor_usd is not None and basket <= rule.suspend_basket_floor_usd
    suspended = suspended_ceiling or suspended_floor
    suspension_reason = (
        "ceiling" if suspended_ceiling else "floor" if suspended_floor else None
    )
    window_start = min(point.trade_date for point in all_points)

    products: dict[str, ProductWindowResult] = {}
    for name in GASOLINE_PRODUCTS:
        if name not in current_prices:
            raise ValidationFailed(f"缺少 {name} 的现行指导价")
        product_rule = rule.products[name]
        current = current_prices[name]
        expected = quantize_price(
            (basket * product_rule.conversion_factor
             + product_rule.processing_margin_per_liter
             + product_rule.consumption_tax_per_liter) * (Decimal(1) + rule.vat_rate)
        )
        reference = reference_prices.get(name)
        reference = current if reference is None else reference
        incoming = incoming_changes.get(name, ZERO)
        raw_change = quantize_price(expected - reference)
        candidate = quantize_price(raw_change + incoming)
        applied = candidate
        threshold = quantize_price(rule.change_threshold_per_liter)
        if suspended:
            decision = "suspended"
        elif abs(applied) >= threshold and applied != ZERO:
            decision = "moved"
        else:
            decision = "held"
        if decision == "moved":
            resulting = quantize_price(current + applied)
            outgoing = ZERO
        else:
            resulting = current
            carry_enabled = rule.carry_on_suspend if suspended else rule.carry_below_threshold
            outgoing = candidate if carry_enabled else ZERO
        products[name] = ProductWindowResult(
            product=name,
            expected_per_liter=expected,
            reference_per_liter=quantize_price(reference),
            current_per_liter=quantize_price(current),
            incoming_change=quantize_price(incoming),
            raw_change=raw_change,
            candidate_change=candidate,
            applied_change=applied,
            threshold_per_liter=threshold,
            decision=decision,
            resulting_per_liter=quantize_price(resulting),
            outgoing_change=quantize_price(outgoing),
        )

    if suspended:
        outcome = "suspended"
    elif any(item.decision == "moved" for item in products.values()):
        outcome = "moved"
    else:
        outcome = "held"
    return {
        "anchor_date": anchor_date,
        "window_start_date": window_start,
        "basket_usd_per_barrel": decimal_text(basket.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)),
        "suspended": suspended,
        "suspension_reason": suspension_reason,
        "outcome": outcome,
        "products": {name: products[name].as_dict() for name in GASOLINE_PRODUCTS},
        "locked_quotes": [point.as_dict() for point in sorted(all_points, key=lambda item: (item.price_index, item.trade_date))],
        "rule_sha256": rule.sha256,
    }


def rule_hash(raw: Mapping[str, object]) -> str:
    """对原始规则字典做规范化哈希，便于在入库前比较。"""
    return PricingRule.from_dict(raw).sha256
