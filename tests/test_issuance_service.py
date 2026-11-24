"""分批签发的服务层与持久化测试：幂等、并发上限、台账守恒。"""

import threading

import pytest

from app.errors import DomainIssue
from app.issuance import service
from app.issuance.store import IssuanceStore


@pytest.fixture
def store(tmp_path):
    return IssuanceStore(str(tmp_path / "issuance.sqlite3"))


def rows(oid="O1", **changes):
    base = [
        {"order_id": oid, "line_id": oid + "L1", "line_amount": "100.00", "discount_total": "15.00",
         "return_qty": "1", "qty": "2", "unit_refund": "100.00", "freight": "10.00", "payment": "200.00"},
        {"order_id": oid, "line_id": oid + "L2", "line_amount": "50.00", "discount_total": "15.00",
         "return_qty": "0", "qty": "1", "unit_refund": "50.00", "freight": "10.00", "payment": "200.00"},
    ]
    for row in base:
        row.update(changes)
    return base


def full_flow(store, oid="O1"):
    order, _ = service.register_order(store, oid, rows(oid))
    return order


def test_register_persists_full_target(store):
    order = full_flow(store)
    assert order["full_target"] == 14500
    assert order["payment"] == 20000


def test_unknown_order_is_404(store):
    with pytest.raises(DomainIssue) as exc:
        service.order_status(store, "NOPE")
    assert exc.value.code == "ORDER_NOT_FOUND"
    assert exc.value.status == 404


def test_issue_then_status_reports_cumulative(store):
    full_flow(store)
    batch = service.issue(store, "O1", {"O1L1": 1, "O1L2": 0}, "req-1")
    assert batch["seq"] == 1 and batch["status"] == "ISSUED"
    status = service.order_status(store, "O1")
    assert status["cumulative_refund"] == 4500
    assert status["remaining"] == 15500
    assert status["freight_refunded"] == 0
    assert status["complete"] is False


def test_retry_same_request_key_is_idempotent_and_books_once(store):
    full_flow(store)
    first = service.issue(store, "O1", {"O1L1": 1, "O1L2": 0}, "req-1")
    retry = service.issue(store, "O1", {"O1L1": 1, "O1L2": 0}, "req-1")
    assert retry["idempotent_hit"] is True
    assert retry["batch_id"] == first["batch_id"]
    status = service.order_status(store, "O1")
    assert status["batch_count"] == 1
    assert status["ledger_count"] == 1  # 重试不重复记账
    assert status["ledger_balanced"] is True


def test_final_batch_matches_full_target_and_pays_freight_once(store):
    order = full_flow(store)
    service.issue(store, "O1", {"O1L1": 1, "O1L2": 0}, "req-1")
    final = service.issue(store, "O1", {"O1L1": 2, "O1L2": 1}, "req-2")
    status = service.order_status(store, "O1")
    assert final["is_final"] is True
    assert status["cumulative_refund"] == order["full_target"]
    assert status["freight_refunded"] == 1000
    assert status["remaining"] == 20000 - 14500
    assert status["complete"] is True


def test_cumulative_cannot_exceed_payment(store):
    full_flow(store, "OC")
    # 把实付压低到无法容纳整单目标：登记时即阻断
    with pytest.raises(DomainIssue) as exc:
        service.register_order(store, "OP", rows("OP", payment="10.00"))
    assert exc.value.code == "ORDER_OVER_PAYMENT"


def test_return_qty_cannot_exceed_ordered(store):
    full_flow(store)
    with pytest.raises(DomainIssue) as exc:
        service.issue(store, "O1", {"O1L1": 3, "O1L2": 1}, "bad")
    assert exc.value.code == "BAD_RETURN_QTY"


def test_cannot_reduce_already_returned_qty(store):
    full_flow(store)
    service.issue(store, "O1", {"O1L1": 2, "O1L2": 0}, "req-1")
    with pytest.raises(DomainIssue) as exc:
        service.issue(store, "O1", {"O1L1": 1, "O1L2": 0}, "req-2")
    assert exc.value.code == "BAD_RETURN_QTY"


def test_concurrent_batches_never_breach_payment(store):
    full_flow(store, "OY")
    errors = []

    def worker(key):
        try:
            service.issue(store, "OY", {"OYL1": 2, "OYL2": 1}, key)  # 每批都申请整单 14500
        except DomainIssue as exc:
            errors.append(exc.code)

    threads = [threading.Thread(target=worker, args=(key,)) for key in ("k-a", "k-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    status = service.order_status(store, "OY")
    assert status["cumulative_refund"] <= status["payment"]
    assert status["batch_count"] == 1
    assert set(errors) <= {"QTY_EXCEEDED", "CUMULATIVE_OVER_PAYMENT", "FULL_TARGET_MISMATCH"}
    assert errors  # 至少一个并发批次被拒


def test_preview_does_not_write(store):
    full_flow(store)
    plan = service.preview_batch(store, "O1", {"O1L1": 1, "O1L2": 0})
    assert plan["batch_amount"] == 4500
    assert plan["cumulative_after"] == 4500
    assert service.order_status(store, "O1")["batch_count"] == 0


def test_ledger_debit_equals_batch_amounts(store):
    full_flow(store)
    service.issue(store, "O1", {"O1L1": 1, "O1L2": 0}, "req-1")
    service.issue(store, "O1", {"O1L1": 2, "O1L2": 1}, "req-2")
    status = service.order_status(store, "O1")
    assert status["ledger_balanced"] is True
    assert sum(batch["amount"] for batch in status["batches"]) == status["cumulative_refund"]


def test_register_rejects_unknown_order_id(store):
    with pytest.raises(DomainIssue) as exc:
        service.register_order(store, "GHOST", rows("O1"))
    assert exc.value.code == "ORDER_NOT_FOUND"
