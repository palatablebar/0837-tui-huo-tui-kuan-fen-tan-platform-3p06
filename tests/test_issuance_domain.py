"""分批退货签发的领域计算测试（不依赖 Flask 与数据库）。"""

import pytest

from app.domain.prepare import prepare_rows
from app.errors import DomainIssue
from app.issuance.domain import (
    _alloc_discount,
    assert_batch_acceptable,
    order_full_target,
    plan_batch,
    validate_order,
)


def prepare(raw):
    items, issues = prepare_rows(raw)
    assert issues == []
    return _alloc_discount(items)


def order_rows(oid="O1", payment="200.00", freight="10.00"):
    return [
        {"order_id": oid, "line_id": oid + "L1", "line_amount": "100.00", "discount_total": "15.00",
         "return_qty": "1", "qty": "2", "unit_refund": "100.00", "freight": freight, "payment": payment},
        {"order_id": oid, "line_id": oid + "L2", "line_amount": "50.00", "discount_total": "15.00",
         "return_qty": "0", "qty": "1", "unit_refund": "50.00", "freight": freight, "payment": payment},
    ]


def full_target(lines):
    view = [dict(line, returned_qty=line["qty"]) for line in lines]
    return order_full_target(view)[2]


def test_full_target_matches_single_full_refund():
    lines = prepare(order_rows())
    # 与 calculate_rows 整单口径一致：商品 13500 + 运费 1000 = 14500
    assert full_target(lines) == 14500


def test_partial_batches_sum_to_full_target_when_complete():
    lines = prepare(order_rows())
    target = full_target(lines)

    first = plan_batch({"order_id": "O1", "lines": [dict(lines[0], returned_qty=1), dict(lines[1], returned_qty=0)]}, [])
    assert first["freight_refund"] == 0  # 退齐前不退运费
    assert first["complete"] is False

    issued = [{"amount": first["amount"], "lines": first["lines"], "freight_refund": 0}]
    final = plan_batch({"order_id": "O1", "lines": [dict(lines[0], returned_qty=2), dict(lines[1], returned_qty=1)]}, issued)
    assert final["complete"] is True
    assert final["freight_refund"] == 1000  # 退齐后只退一次运费
    assert final["cumulative_after"] == target
    # 已签发批次金额未被修改，尾差全部落在最后一批
    assert first["amount"] == 4500


def test_issued_amounts_are_immutable_later_batches_only_adjust_own_lines():
    lines = prepare(order_rows())
    first = plan_batch({"order_id": "O1", "lines": [dict(lines[0], returned_qty=1), dict(lines[1], returned_qty=0)]}, [])
    frozen = first["amount"]
    issued = [{"amount": frozen, "lines": first["lines"], "freight_refund": 0}]
    plan_batch({"order_id": "O1", "lines": [dict(lines[0], returned_qty=2), dict(lines[1], returned_qty=1)]}, issued)
    assert first["amount"] == frozen


def test_rounding_tail_is_absorbed_in_final_batch():
    # 33.33 元优惠 10 元，净行 2333 分，按 3 件分批会产生每批 778 的舍入。
    raw = [{"order_id": "O3", "line_id": "O3L1", "line_amount": "33.33", "discount_total": "10.00",
            "return_qty": "1", "qty": "3", "unit_refund": "33.33", "freight": "5.00", "payment": "60.00"}]
    line = prepare(raw)[0]
    target = full_target([line])
    batches = []
    for cumulative in (1, 2, 3):
        plan = plan_batch({"order_id": "O3", "lines": [dict(line, returned_qty=cumulative)]}, batches)
        batches.append({"amount": plan["amount"], "lines": plan["lines"], "freight_refund": plan["freight_refund"]})
    assert batches[-1]["amount"] == 1277  # 尾差 1 分在末批吸收
    assert sum(b["amount"] for b in batches) == target
    assert batches[0]["freight_refund"] == 0 and batches[1]["freight_refund"] == 0
    assert batches[2]["freight_refund"] == 500


def test_freight_refunded_exactly_once_even_in_many_batches():
    raw = [{"order_id": "O4", "line_id": "O4L1", "line_amount": "30.00", "discount_total": "0.00",
            "return_qty": "1", "qty": "4", "unit_refund": "30.00", "freight": "6.00", "payment": "40.00"}]
    line = prepare(raw)[0]
    batches = []
    freight_events = 0
    for cumulative in (1, 2, 3, 4):
        plan = plan_batch({"order_id": "O4", "lines": [dict(line, returned_qty=cumulative)]}, batches)
        freight_events += plan["freight_refund"]
        batches.append({"amount": plan["amount"], "lines": plan["lines"], "freight_refund": plan["freight_refund"]})
    assert freight_events == 600
    assert sum(b["amount"] for b in batches) == full_target([line])


def test_cumulative_never_exceeds_payment():
    lines = prepare(order_rows(payment="120.00"))  # 实付 12000 < 整单目标 14500
    plan = plan_batch({"order_id": "O1", "lines": [dict(lines[0], returned_qty=2), dict(lines[1], returned_qty=1)]}, [])
    with pytest.raises(DomainIssue) as exc:
        assert_batch_acceptable(plan)
    assert exc.value.code == "CUMULATIVE_OVER_PAYMENT"


def test_validate_order_detects_conflicting_headers():
    rows = order_rows()
    rows[1]["payment"] = "999.00"
    lines = prepare(rows)
    assert validate_order(lines) == "ORDER_FIELDS_CONFLICT"


def test_validate_order_detects_target_over_payment():
    lines = prepare(order_rows(payment="10.00"))
    assert validate_order(lines) == "ORDER_OVER_PAYMENT"


def test_each_batch_is_calculated_independently():
    # 两批都退 1 件，各自独立按比例计算，互不引用对方的舍入结果。
    raw = [{"order_id": "O5", "line_id": "O5L1", "line_amount": "10.00", "discount_total": "0.00",
            "return_qty": "1", "qty": "3", "unit_refund": "10.00", "freight": "0.00", "payment": "10.00"}]
    line = prepare(raw)[0]
    first = plan_batch({"order_id": "O5", "lines": [dict(line, returned_qty=1)]}, [])
    second = plan_batch({"order_id": "O5", "lines": [dict(line, returned_qty=2)]},
                        [{"amount": first["amount"], "lines": first["lines"], "freight_refund": 0}])
    assert first["lines"][0]["refund"] == second["lines"][0]["refund"] == 333


def test_multiline_tail_converges_without_negative_refunds():
    raw = [
        {"order_id": "M", "line_id": "ML1", "line_amount": "10.00", "discount_total": "1.00",
         "return_qty": "1", "qty": "3", "unit_refund": "10", "freight": "0.00", "payment": "30.00"},
        {"order_id": "M", "line_id": "ML2", "line_amount": "20.00", "discount_total": "1.00",
         "return_qty": "1", "qty": "3", "unit_refund": "20", "freight": "0.00", "payment": "30.00"},
    ]
    lines = prepare(raw)
    target = full_target(lines)
    batches = []
    frozen_amounts = []
    for cumulative in (1, 2, 3):
        plan = plan_batch({"order_id": "M", "lines": [dict(line, returned_qty=cumulative) for line in lines]}, batches)
        assert all(item["refund"] >= 0 for item in plan["lines"])
        frozen_amounts.append(plan["amount"])
        batches.append({"amount": plan["amount"], "lines": plan["lines"], "freight_refund": plan["freight_refund"]})
    # 已签发金额冻结
    for index, amount in enumerate(frozen_amounts):
        assert batches[index]["amount"] == amount
    assert sum(batch["amount"] for batch in batches) == target
    assert batches[-1]["amount"] == 968  # 两行尾差都在末批吸收
