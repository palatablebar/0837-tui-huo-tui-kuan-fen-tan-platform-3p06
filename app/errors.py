from flask import jsonify


MESSAGES = {
    "EMPTY_FILE": "文件为空",
    "BLANK_FILE": "文件只有空白",
    "HEADER_ONLY": "文件只有表头",
    "HEADER_MISMATCH": "表头与本项目字段不一致",
    "UNCLOSED_QUOTE": "引号未闭合，文件已拒收",
    "TOO_MANY_ROWS": "数据行超过上限",
    "FILE_TOO_LARGE": "文件超过大小上限",
    "KEY_MISSING": "主键为空",
    "CONFLICT_DUPLICATE": "同一键存在冲突内容",
    "SAMPLE_UNKNOWN": "样例不存在",
    "STAGE_UNKNOWN": "阶段不存在",
    "RUN_NOT_FOUND": "批次不存在",
    "STALE_REVIEW": "批次已被更新，请刷新后复核",
    "GATE_BLOCKED": "存在阻断项，不能签发",
    "NOT_APPROVED": "批次尚未签发",
    "BAD_REQUEST": "请求内容不完整",
    "ORDER_NOT_FOUND": "订单未登记",
    "ORDER_EMPTY": "订单没有可退商品行",
    "ORDER_FIELDS_CONFLICT": "订单头字段在行之间不一致",
    "ORDER_OVER_PAYMENT": "整单应退超过订单实付",
    "BAD_RETURN_QTY": "退货数量超出订购数量或为负",
    "QTY_EXCEEDED": "累计退货数量超过订购数量",
    "CUMULATIVE_OVER_PAYMENT": "累计退款将超过订单实付，本批拒签",
    "FULL_TARGET_MISMATCH": "退齐时累计实退与整单一次退清不一致",
    "NEGATIVE_REFUND": "退款金额不能为负",
    "TAIL_NOT_ABSORBABLE": "尾差无法在本批吸收",
    "ALREADY_COMPLETE": "订单已退齐，不能再签发",
}


class DomainIssue(Exception):
    def __init__(self, code: str, status: int = 400, detail: str | None = None):
        self.code = code
        self.status = status
        self.detail = detail
        super().__init__(code)


def error_response(exc: DomainIssue):
    return jsonify(error=exc.detail or MESSAGES.get(exc.code, "请求无法处理"), code=exc.code), exc.status
