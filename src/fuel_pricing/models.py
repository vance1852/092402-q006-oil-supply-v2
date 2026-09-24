"""汽油指导价领域输入契约。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Mapping

from .errors import ValidationFailed

PRODUCTS = ("92", "95")
GRADES = frozenset(PRODUCTS)
TAX_KINDS = frozenset({"fixed", "rate"})
TAX_BASES = frozenset({"cost", "cost_plus_fixed", "tax"})


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not all(ch.isalnum() or ch in "_.:-" for ch in result) or not result[0].isalnum():
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except Exception as exc:  # InvalidOperation/ValueError/TypeError
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def iso_date(value: object, field: str) -> str:
    text = required_text(value, field, 10)
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


@dataclass(frozen=True, slots=True)
class TaxComponent:
    """税费组件。

    kind=fixed 为从量税（元/升）；kind=rate 为从价税，base 决定计税基础：
    cost（原油折算成本加加工价差）、cost_plus_fixed（成本加全部从量税，如增值税）、
    tax（已累计税费，如城建税与教育费附加）。
    """

    name: str
    kind: str
    value: Decimal
    base: str | None

    def to_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {"name": self.name, "kind": self.kind, "value": str(self.value)}
        if self.base is not None:
            document["base"] = self.base
        return document


@dataclass(frozen=True, slots=True)
class ProductRule:
    """单个牌号在一个政策版本下的完整参数。"""

    grade: str
    base_guide_price: Decimal
    tax_components: tuple[TaxComponent, ...]
    processing_spread: Decimal
    min_change: Decimal
    floor_price: Decimal | None
    ceiling_price: Decimal | None

    def to_document(self) -> dict[str, Any]:
        return {
            "grade": self.grade,
            "base_guide_price": str(self.base_guide_price),
            "tax_components": [component.to_document() for component in self.tax_components],
            "processing_spread": str(self.processing_spread),
            "min_change_threshold": str(self.min_change),
            "floor_price": None if self.floor_price is None else str(self.floor_price),
            "ceiling_price": None if self.ceiling_price is None else str(self.ceiling_price),
        }


@dataclass(frozen=True, slots=True)
class RuleDraft:
    """规则版本草稿：调整周期、报价窗口与各牌号参数的完整快照来源。"""

    rule_id: str
    price_index: str
    effective_from: str
    cycle_anchor_date: str
    cycle_workdays: int
    quote_window_workdays: int
    fx_rate_cny_per_usd: Decimal
    products: tuple[ProductRule, ...]
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RuleDraft":
        rule_id = identifier(raw.get("rule_id"), "rule_id")
        price_index = required_text(raw.get("price_index"), "price_index", 16).upper()
        effective_from = iso_date(raw.get("effective_from"), "effective_from")
        cycle_anchor_date = iso_date(raw.get("cycle_anchor_date"), "cycle_anchor_date")
        if cycle_anchor_date < effective_from:
            raise ValidationFailed("cycle_anchor_date 不能早于 effective_from")
        cycle = raw.get("cycle_workdays")
        if isinstance(cycle, bool) or not isinstance(cycle, int) or not 1 <= cycle <= 31:
            raise ValidationFailed("cycle_workdays 必须是 1 到 31 的整数")
        window = raw.get("quote_window_workdays")
        if isinstance(window, bool) or not isinstance(window, int) or not 1 <= window <= 62:
            raise ValidationFailed("quote_window_workdays 必须是 1 到 62 的整数")
        fx = decimal_value(raw.get("fx_rate_cny_per_usd"), "fx_rate_cny_per_usd", minimum=Decimal("0.000001"))
        products_raw = raw.get("products")
        if not isinstance(products_raw, Mapping) or not products_raw:
            raise ValidationFailed("products 必须是非空对象")
        unknown = set(products_raw) - GRADES
        if unknown:
            raise ValidationFailed(f"不支持的牌号: {sorted(unknown)}；仅支持 92 与 95")
        rules: list[ProductRule] = []
        for grade in PRODUCTS:
            if grade not in products_raw:
                raise ValidationFailed(f"products 必须包含 {grade} 号汽油参数")
            entry = products_raw[grade]
            if not isinstance(entry, Mapping):
                raise ValidationFailed(f"products.{grade} 必须是对象")
            taxes_raw = entry.get("tax_components", [])
            if not isinstance(taxes_raw, list) or not taxes_raw:
                raise ValidationFailed(f"products.{grade}.tax_components 必须是非空数组")
            names: set[str] = set()
            taxes: list[TaxComponent] = []
            for index, item in enumerate(taxes_raw):
                if not isinstance(item, Mapping):
                    raise ValidationFailed(f"products.{grade}.tax_components[{index}] 必须是对象")
                name = required_text(item.get("name"), f"products.{grade}.tax_components[{index}].name", 32)
                if name in names:
                    raise ValidationFailed(f"税费名称重复: {name}")
                names.add(name)
                kind = required_text(item.get("kind"), f"products.{grade}.tax_components[{index}].kind", 8)
                if kind not in TAX_KINDS:
                    raise ValidationFailed("税费 kind 只能是 fixed 或 rate")
                value = decimal_value(
                    item.get("value"),
                    f"products.{grade}.tax_components[{index}].value",
                    minimum=Decimal("0"),
                )
                base = item.get("base")
                if kind == "fixed":
                    if base is not None:
                        raise ValidationFailed(f"从量税 {name} 不能指定 base")
                    base_value = None
                else:
                    base_text = required_text(base, f"products.{grade}.tax_components[{index}].base", 24)
                    if base_text not in TAX_BASES:
                        raise ValidationFailed("从价税 base 只能是 cost、cost_plus_fixed 或 tax")
                    base_value = base_text
                taxes.append(TaxComponent(name=name, kind=kind, value=value, base=base_value))
            base_price = decimal_value(
                entry.get("base_guide_price"), f"products.{grade}.base_guide_price", minimum=Decimal("0")
            )
            spread = decimal_value(
                entry.get("processing_spread"), f"products.{grade}.processing_spread", minimum=Decimal("0")
            )
            minimum = decimal_value(
                entry.get("min_change_threshold"),
                f"products.{grade}.min_change_threshold",
                minimum=Decimal("0"),
            )
            floor = entry.get("floor_price")
            ceiling = entry.get("ceiling_price")
            floor_d = None if floor is None else decimal_value(floor, f"products.{grade}.floor_price", minimum=Decimal("0"))
            ceiling_d = None if ceiling is None else decimal_value(ceiling, f"products.{grade}.ceiling_price", minimum=Decimal("0"))
            if floor_d is not None and ceiling_d is not None and floor_d > ceiling_d:
                raise ValidationFailed(f"products.{grade} 的 floor_price 不能高于 ceiling_price")
            rules.append(
                ProductRule(
                    grade=grade,
                    base_guide_price=base_price,
                    tax_components=tuple(taxes),
                    processing_spread=spread,
                    min_change=minimum,
                    floor_price=floor_d,
                    ceiling_price=ceiling_d,
                )
            )
        note_raw = raw.get("note", "")
        note = required_text(note_raw, "note", 512) if isinstance(note_raw, str) and note_raw.strip() else ""
        return cls(
            rule_id=rule_id,
            price_index=price_index,
            effective_from=effective_from,
            cycle_anchor_date=cycle_anchor_date,
            cycle_workdays=cycle,
            quote_window_workdays=window,
            fx_rate_cny_per_usd=fx,
            products=tuple(rules),
            note=note,
        )

    def document(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "price_index": self.price_index,
            "effective_from": self.effective_from,
            "cycle_anchor_date": self.cycle_anchor_date,
            "cycle_workdays": self.cycle_workdays,
            "quote_window_workdays": self.quote_window_workdays,
            "fx_rate_cny_per_usd": str(self.fx_rate_cny_per_usd),
            "products": [product.to_document() for product in self.products],
            "note": self.note,
        }
