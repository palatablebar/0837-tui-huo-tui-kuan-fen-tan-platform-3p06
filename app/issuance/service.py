"""分批签发应用服务：编排领域计算与持久化。"""

import json

from app.domain.prepare import prepare_rows
from app.errors import DomainIssue
from app.issuance.domain import (
    _alloc_discount,
    assert_batch_acceptable,
    order_full_target,
    plan_batch,
    validate_order,
)
from app.issuance.store import IssuanceStore

# 持久层问题码 -> 对外错误码
STORE_PROBLEMS = {
    "order_missing": ("ORDER_NOT_FOUND", 404),
    "over_limit": ("CUMULATIVE_OVER_PAYMENT", 409),
    "qty_exceeded": ("QTY_EXCEEDED", 409),
    "final_mismatch": ("FULL_TARGET_MISMATCH", 409),
    "negative": ("NEGATIVE_REFUND", 400),
}


def register_order(store: IssuanceStore, order_id, raw_rows):
    """把来源行整理成订单并登记目标金额。raw_rows 为 CSV 原始字典列表。"""
    prepared, issues = prepare_rows(raw_rows)
    lines = [line for line in prepared if line["order_id"] == order_id]
    if not lines:
        raise DomainIssue("ORDER_NOT_FOUND", 404)
    problem = validate_order(lines)
    if problem:
        raise DomainIssue(problem, 400 if problem == "ORDER_EMPTY" else 409)
    lines = _alloc_discount([dict(line) for line in lines])
    # 整单一次退清目标：把各行数量视作全部退齐。
    full_view = [dict(line, returned_qty=line["qty"]) for line in lines]
    _, _, full_target = order_full_target(full_view)
    store.upsert_order(order_id, lines[0]["payment"], full_target, lines)
    return store.get_order(order_id), issues


def preview_batch(store: IssuanceStore, order_id, returned_qty):
    """不写库，预览下一批签发计划与签发后累计。"""
    order = store.get_order(order_id)
    if order is None:
        raise DomainIssue("ORDER_NOT_FOUND", 404)
    lines = _apply_returned(order["lines"], returned_qty, store.list_batches(order_id))
    batches = _issued_batches(store, order_id)
    plan = plan_batch({"order_id": order_id, "lines": lines}, batches)
    assert_batch_acceptable(plan)
    return _serialize_plan(order, plan, batches)


def _issued_batches(store, order_id):
    batches = []
    for row in store.list_batches(order_id):
        detail = json.loads(row["detail"])
        batches.append({"seq": row["seq"], "amount": row["amount"], "lines": detail["lines"],
                        "freight_refund": row["freight_refund"], "is_final": bool(row["is_final"])})
    return batches


def _apply_returned(stored_lines, returned_qty, issued_rows):
    """合并本批申报的累计退货数量与已签发批次数量。"""
    already = {line["line_id"]: 0 for line in stored_lines}
    for row in issued_rows:
        for entry in json.loads(row["detail"])["lines"]:
            already[entry["line_id"]] = already.get(entry["line_id"], 0) + int(entry["qty"])
    lines = []
    for line in stored_lines:
        item = dict(line)
        requested = int(returned_qty.get(line["line_id"], already[line["line_id"]]))
        item["returned_qty"] = requested
        if requested < already[line["line_id"]] or requested > line["qty"]:
            raise DomainIssue("BAD_RETURN_QTY", 400)
        lines.append(item)
    return lines


def issue(store: IssuanceStore, order_id, returned_qty, request_key, comment=""):
    """签发一批并记账。重试同一 request_key 幂等返回原批次。"""
    order = store.get_order(order_id)
    if order is None:
        raise DomainIssue("ORDER_NOT_FOUND", 404)
    issued_rows = store.list_batches(order_id)
    lines = _apply_returned(order["lines"], returned_qty, issued_rows)
    batches = _issued_batches(store, order_id)
    plan = plan_batch({"order_id": order_id, "lines": lines}, batches)
    assert_batch_acceptable(plan)

    detail = {"order_id": order_id, "lines": plan["lines"], "freight_refund": plan["freight_refund"],
              "amount": plan["amount"], "complete": plan["complete"], "comment": comment.strip(),
              "issued_before": plan["issued_before"], "cumulative_after": plan["cumulative_after"]}
    batch, problem = store.issue_batch(request_key, order_id, plan, detail)
    if problem == "exists":
        return _serialize_batch(batch, store, order, idempotent=True)
    if problem:
        code, status = STORE_PROBLEMS.get(problem, ("BAD_REQUEST", 400))
        raise DomainIssue(code, status)
    return _serialize_batch(batch, store, order, idempotent=False)


def order_status(store: IssuanceStore, order_id):
    order = store.get_order(order_id)
    if order is None:
        raise DomainIssue("ORDER_NOT_FOUND", 404)
    batches = []
    for row in store.list_batches(order_id):
        batches.append(_serialize_batch(row, store, order, summarize=True))
    totals = store.ledger_totals(order_id)
    return {
        "order_id": order_id,
        "payment": order["payment"],
        "full_target": order["full_target"],
        "remaining": order["payment"] - totals["cumulative"],
        "freight_refunded": totals["freight_total"],
        "batch_count": totals["batch_count"],
        "ledger_count": totals["ledger_count"],
        "ledger_balanced": totals["ledger_debit_total"] == totals["cumulative"],
        "cumulative_refund": totals["cumulative"],
        "complete": any(item["is_final"] for item in batches),
        "batches": batches,
    }


def _serialize_plan(order, plan, batches):
    return {
        "order_id": plan["order_id"],
        "payment": plan["payment"],
        "issued_before": plan["issued_before"],
        "batch_amount": plan["amount"],
        "freight_refund": plan["freight_refund"],
        "cumulative_after": plan["cumulative_after"],
        "remaining_after": plan["payment"] - plan["cumulative_after"],
        "complete": plan["complete"],
        "full_target": plan["full_target"],
        "lines": plan["lines"],
        "issued_batch_count": len(batches),
    }


def _serialize_batch(row, store, order, summarize=False, idempotent=False):
    detail = json.loads(row["detail"])
    payload = {
        "batch_id": row["id"],
        "request_key": row["request_key"],
        "order_id": row["order_id"],
        "seq": row["seq"],
        "status": row["status"],
        "amount": row["amount"],
        "freight_refund": row["freight_refund"],
        "cumulative_after": row["cumulative"],
        "is_final": bool(row["is_final"]),
        "created_at": row["created_at"],
        "idempotent_hit": idempotent,
        "detail": detail if not summarize else {"lines": detail["lines"], "comment": detail.get("comment", "")},
    }
    return payload
