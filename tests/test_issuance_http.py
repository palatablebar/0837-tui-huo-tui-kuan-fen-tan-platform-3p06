"""分批签发的 HTTP 接口测试。"""

import json


def order_payload(oid="O1", payment="200.00", freight="10.00"):
    return {
        "order_id": oid,
        "rows": [
            {"order_id": oid, "line_id": oid + "L1", "line_amount": "100.00", "discount_total": "15.00",
             "return_qty": "1", "qty": "2", "unit_refund": "100.00", "freight": freight, "payment": payment},
            {"order_id": oid, "line_id": oid + "L2", "line_amount": "50.00", "discount_total": "15.00",
             "return_qty": "0", "qty": "1", "unit_refund": "50.00", "freight": freight, "payment": payment},
        ],
    }


def register(client, oid="O1", **changes):
    payload = order_payload(oid, **changes)
    response = client.post("/api/issuance/orders", json=payload)
    assert response.status_code == 200, response.data
    return response.get_json()


def test_register_and_read_status(client):
    data = register(client)
    assert data["full_target"] == 14500
    status = client.get("/api/issuance/orders/O1").get_json()
    assert status["cumulative_refund"] == 0
    assert status["batch_count"] == 0


def test_register_from_sample_profile(client):
    response = client.post("/api/issuance/orders", json={"order_id": "O1", "profile": "clean"})
    assert response.status_code == 200
    assert response.get_json()["full_target"] == 14500


def test_preview_then_issue_flow(client):
    register(client)
    preview = client.post("/api/issuance/orders/O1/preview",
                          json={"returned_qty": {"O1L1": 1, "O1L2": 0}})
    assert preview.status_code == 200
    body = preview.get_json()
    assert body["batch_amount"] == 4500 and body["freight_refund"] == 0
    assert body["remaining_after"] == 15500

    issue = client.post("/api/issuance/orders/O1/batches",
                        json={"returned_qty": {"O1L1": 1, "O1L2": 0}, "request_key": "r1", "comment": "首批"})
    assert issue.status_code == 200
    assert issue.get_json()["cumulative_after"] == 4500


def test_full_refund_converges_and_freight_once(client):
    order = register(client)
    client.post("/api/issuance/orders/O1/batches",
                json={"returned_qty": {"O1L1": 1, "O1L2": 0}, "request_key": "r1"})
    final = client.post("/api/issuance/orders/O1/batches",
                        json={"returned_qty": {"O1L1": 2, "O1L2": 1}, "request_key": "r2"})
    assert final.get_json()["is_final"] is True
    status = client.get("/api/issuance/orders/O1").get_json()
    assert status["cumulative_refund"] == order["full_target"]
    assert status["freight_refunded"] == 1000
    assert status["ledger_balanced"] is True


def test_retry_is_idempotent_no_double_booking(client):
    register(client)
    payload = {"returned_qty": {"O1L1": 1, "O1L2": 0}, "request_key": "dup"}
    first = client.post("/api/issuance/orders/O1/batches", json=payload).get_json()
    second = client.post("/api/issuance/orders/O1/batches", json=payload).get_json()
    assert first["batch_id"] == second["batch_id"]
    assert second["idempotent_hit"] is True
    status = client.get("/api/issuance/orders/O1").get_json()
    assert status["batch_count"] == 1 and status["ledger_count"] == 1


def test_over_payment_is_rejected(client):
    response = client.post("/api/issuance/orders", json=order_payload("OX", payment="10.00"))
    assert response.status_code == 409
    assert response.get_json()["code"] == "ORDER_OVER_PAYMENT"


def test_qty_guard_returns_400(client):
    register(client)
    response = client.post("/api/issuance/orders/O1/batches",
                           json={"returned_qty": {"O1L1": 9, "O1L2": 1}, "request_key": "bad"})
    assert response.status_code == 400
    assert response.get_json()["code"] == "BAD_RETURN_QTY"


def test_missing_request_key_is_bad_request(client):
    register(client)
    response = client.post("/api/issuance/orders/O1/batches",
                           json={"returned_qty": {"O1L1": 1}})
    assert response.status_code == 400


def test_unknown_order_returns_404(client):
    response = client.get("/api/issuance/orders/NOPE")
    assert response.status_code == 404
    assert response.get_json()["code"] == "ORDER_NOT_FOUND"


def test_each_batch_response_explains_cumulative_refund(client):
    register(client)
    first = client.post("/api/issuance/orders/O1/batches",
                        json={"returned_qty": {"O1L1": 1, "O1L2": 0}, "request_key": "r1"}).get_json()
    second = client.post("/api/issuance/orders/O1/batches",
                         json={"returned_qty": {"O1L1": 2, "O1L2": 1}, "request_key": "r2"}).get_json()
    # 每批签发后都交代：本批金额、本批前累计、本批后累计、订单实付
    assert first["amount"] == 4500 and first["cumulative_after"] == 4500
    assert second["detail"]["issued_before"] == 4500
    assert second["detail"]["cumulative_after"] == 14500
    status = client.get("/api/issuance/orders/O1").get_json()
    assert status["remaining"] == 20000 - 14500
