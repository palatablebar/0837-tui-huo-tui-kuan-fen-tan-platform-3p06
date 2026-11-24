"""分批退货签发的核心领域逻辑。

金额一律使用整数「分」，比例用整数除法并以最大余额法处理尾差，
避免二进制浮点在边界上漂移。所有函数都是纯函数，便于单测。

关键约束（见 docs/BUSINESS-RULES.md「分批退货」）：

* 每批按本批退货数量独立计算，已签发批次的金额不可修改；
* 退齐时累计实退必须等于整单一次退清的金额，尾差在最后一批吸收；
* 运费在退齐前不退，退齐后也只退一次；
* 任一批签发后累计退款不得超过订单实付。
"""

from app.domain.helpers import largest_remainder, round_div
from app.errors import DomainIssue


def line_full_refund(line):
    """整行一次退清时的商品应退（不含运费），与 calculate_rows 同口径。"""
    net_line = line["line_amount"] - line["alloc_discount"]
    return round_div(net_line * line["qty"], line["qty"])


def order_full_target(lines):
    """整单一次退清的目标金额：商品应退合计 + 退齐时的一次运费。"""
    goods = sum((line_full_refund(line) for line in lines), 0)
    freight = lines[0]["freight"] if all(line["returned_qty"] == line["qty"] for line in lines) else 0
    return goods, freight, goods + freight


def _alloc_discount(lines):
    """订单优惠按行金额做最大余额分摊，写回每行 alloc_discount。"""
    discount = min(lines[0]["discount_total"], sum(line["line_amount"] for line in lines))
    shares = largest_remainder(discount, [line["line_amount"] for line in lines])
    for line, share in zip(lines, shares):
        line["alloc_discount"] = share
    return lines


def _batch_line_refund(line, batch_qty):
    """单批单行按退货数量比例计算的商品应退（整数分，四舍五入）。"""
    net_line = line["line_amount"] - line["alloc_discount"]
    return round_div(net_line * batch_qty, line["qty"])


def _is_complete(lines):
    return all(line["returned_qty"] == line["qty"] for line in lines)


def plan_batch(order, batches):
    """根据已签发批次计算下一批的签发计划。

    ``order`` 为订单行（含分单位的整型金额与本次累计退货数量），
    ``batches`` 为已签发批次列表，每条含 ``lines`` 与 ``amount``。

    返回每个行的本批应退、运费、本批合计，以及退齐时为对齐整单目标
    而做的尾差调整。已签发金额保持不变，尾差只落在本批。
    """
    lines = _alloc_discount([dict(line) for line in order["lines"]])
    issued_by_line = {line["line_id"]: 0 for line in lines}
    issued_total = 0
    for batch in batches:
        issued_total += int(batch["amount"])
        for entry in batch["lines"]:
            issued_by_line[entry["line_id"]] = issued_by_line.get(entry["line_id"], 0) + int(entry["refund"])

    plan_lines = []
    for line in lines:
        already = issued_by_line[line["line_id"]]
        batch_qty = line["returned_qty"] - sum(
            entry.get("qty", 0) for batch in batches for entry in batch["lines"] if entry["line_id"] == line["line_id"])
        raw = _batch_line_refund(line, batch_qty)
        full = line_full_refund(line)
        # 单行累计不能超过该行整行退清金额。
        raw = max(0, min(raw, full - already))
        plan_lines.append({"line_id": line["line_id"], "qty": batch_qty, "refund": raw,
                           "already_refund": already, "full_refund": full})

    complete = _is_complete(lines)
    full_target = None
    if complete:
        # 退齐：把各行整行目标与已签发/本批拟退的差额收敛进本批，
        # 保证累计实退 == 整单一次退清（含一次运费）。
        goods_target, freight, full_target = order_full_target(lines)
        delta = goods_target - sum(item["already_refund"] + item["refund"] for item in plan_lines)
        if delta != 0:
            _absorb_tail(plan_lines, delta)
    else:
        freight = 0

    amount = sum(item["refund"] for item in plan_lines) + freight
    return {"order_id": order["order_id"], "lines": plan_lines, "freight_refund": freight,
            "amount": amount, "issued_before": issued_total,
            "cumulative_after": issued_total + amount, "complete": complete,
            "payment": lines[0]["payment"], "full_target": full_target}


def _absorb_tail(plan_lines, delta):
    """把尾差差额（分）吸收到本批各行，已签发金额不动。

    优先在尚有额度的行上调整，保证每行累计不超过其整行目标、且不为负。
    """
    if delta > 0:
        for item in plan_lines:
            room = item["full_refund"] - item["already_refund"] - item["refund"]
            take = min(delta, room)
            if take > 0:
                item["refund"] += take
                delta -= take
            if delta == 0:
                break
    else:
        for item in plan_lines:
            cut = min(-delta, item["refund"])
            if cut > 0:
                item["refund"] -= cut
                delta += cut
            if delta == 0:
                break
    if delta != 0:
        # 理论上不会发生：订单实付上限校验会先拦截无法收敛的批次。
        raise DomainIssue("TAIL_NOT_ABSORBABLE", 409)


def validate_order(lines):
    """订单级前置校验，返回问题码或 None。"""
    if not lines:
        return "ORDER_EMPTY"
    if any(len({line[field] for line in lines}) != 1 for field in ("discount_total", "payment", "freight")):
        return "ORDER_FIELDS_CONFLICT"
    prepared = _alloc_discount([dict(line) for line in lines])
    full_view = [dict(line, returned_qty=line["qty"]) for line in prepared]
    _, _, target = order_full_target(full_view)
    if target > lines[0]["payment"]:
        return "ORDER_OVER_PAYMENT"
    return None


def assert_batch_acceptable(plan):
    """签发前的累计上限与非负校验。"""
    if plan["amount"] < 0:
        raise DomainIssue("NEGATIVE_REFUND", 400)
    if any(item["qty"] < 0 for item in plan["lines"]):
        raise DomainIssue("BAD_RETURN_QTY", 400)
    if plan["cumulative_after"] > plan["payment"]:
        raise DomainIssue("CUMULATIVE_OVER_PAYMENT", 409)
    if plan["complete"] and plan["full_target"] is not None and plan["cumulative_after"] != plan["full_target"]:
        raise DomainIssue("FULL_TARGET_MISMATCH", 409)
