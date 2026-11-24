"""分批退货签发的 HTTP 接口。"""

from flask import Blueprint, jsonify, request

from app.config import SAMPLES
from app.errors import DomainIssue
from app.intake.reader import read_records
from app.catalog import SPEC
from app.issuance import service

api = Blueprint("issuance", __name__, url_prefix="/api/issuance")


def store():
    from flask import current_app
    return current_app.extensions["issuance"]


def _rows_from_payload(data):
    if data.get("rows"):
        return data["rows"]
    profile = data.get("profile", "clean")
    if profile not in {"clean", "dirty"}:
        raise DomainIssue("SAMPLE_UNKNOWN", 404)
    raw = (SAMPLES / f"{profile}.csv").read_bytes()
    parsed = read_records(raw, SPEC["columns"], 10_000)
    return [dict(zip(SPEC["columns"], record["cells"])) for record in parsed["records"]
            if not record["blank"] and not record["repeated"] and len(record["cells"]) == len(SPEC["columns"])]


@api.post("/orders")
def register():
    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id")
    if not order_id:
        raise DomainIssue("BAD_REQUEST")
    rows = _rows_from_payload(data)
    order, issues = service.register_order(store(), order_id, rows)
    return jsonify({"order_id": order_id, "payment": order["payment"], "full_target": order["full_target"],
                    "line_count": len(order["lines"]), "prepare_issues": issues})


@api.get("/orders/<order_id>")
def status(order_id):
    return jsonify(service.order_status(store(), order_id))


@api.post("/orders/<order_id>/preview")
def preview(order_id):
    data = request.get_json(silent=True) or {}
    returned_qty = data.get("returned_qty")
    if not isinstance(returned_qty, dict):
        raise DomainIssue("BAD_REQUEST")
    return jsonify(service.preview_batch(store(), order_id, returned_qty))


@api.post("/orders/<order_id>/batches")
def issue(order_id):
    data = request.get_json(silent=True) or {}
    returned_qty = data.get("returned_qty")
    request_key = data.get("request_key")
    if not isinstance(returned_qty, dict) or not request_key:
        raise DomainIssue("BAD_REQUEST")
    result = service.issue(store(), order_id, returned_qty, str(request_key), data.get("comment", ""))
    return jsonify(result)


@api.get("/orders/<order_id>/batches")
def batches(order_id):
    return jsonify(service.order_status(store(), order_id))
