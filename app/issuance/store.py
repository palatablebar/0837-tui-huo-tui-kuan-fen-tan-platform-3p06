"""分批签发的 SQLite 持久化。

设计要点：

* ``issuance_batches.request_key`` 唯一：同一请求键重试直接返回原批次，不重复记账；
* ``issuance_ledger`` 只追加（append-only），与批次一一对应，重试不会产生第二条分录；
* 签发在单事务内 ``BEGIN IMMEDIATE`` 并对订单行加锁，
  两个待签批次并发提交时串行化，累计退款绝不会超过订单实付。
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone


class IssuanceStore:
    def __init__(self, path):
        self.path = path
        self.init()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=8)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def init(self):
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS issuance_orders(
              order_id TEXT PRIMARY KEY,
              payment INTEGER NOT NULL,
              full_target INTEGER NOT NULL,
              lines TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS issuance_batches(
              id TEXT PRIMARY KEY,
              request_key TEXT UNIQUE NOT NULL,
              order_id TEXT NOT NULL,
              seq INTEGER NOT NULL,
              amount INTEGER NOT NULL,
              cumulative INTEGER NOT NULL,
              freight_refund INTEGER NOT NULL,
              is_final INTEGER NOT NULL,
              status TEXT NOT NULL,
              detail TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(order_id, seq)
            );
            CREATE TABLE IF NOT EXISTS issuance_ledger(
              id TEXT PRIMARY KEY,
              batch_id TEXT UNIQUE NOT NULL,
              order_id TEXT NOT NULL,
              debit INTEGER NOT NULL,
              cumulative_after INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(batch_id) REFERENCES issuance_batches(id)
            );
            ''')

    def upsert_order(self, order_id, payment, full_target, lines):
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute('''INSERT INTO issuance_orders(order_id,payment,full_target,lines,created_at)
                          VALUES(?,?,?,?,?)
                          ON CONFLICT(order_id) DO UPDATE SET
                            payment=excluded.payment, full_target=excluded.full_target, lines=excluded.lines''',
                       (order_id, int(payment), int(full_target), json.dumps(lines, ensure_ascii=False), now))

    def get_order(self, order_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM issuance_orders WHERE order_id=?", (order_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["lines"] = json.loads(data["lines"])
        return data

    def find_by_request(self, request_key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM issuance_batches WHERE request_key=?", (request_key,)).fetchone()
        return dict(row) if row else None

    def list_batches(self, order_id):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM issuance_batches WHERE order_id=? ORDER BY seq", (order_id,)).fetchall()
        return [dict(row) for row in rows]

    def issue_batch(self, request_key, order_id, plan, detail):
        """在单事务内签发并记账。返回 (batch_dict, problem)。

        problem 取值：``exists``（幂等命中）、``over_limit``（累计超实付）、
        ``qty_exceeded``（累计退货超订购量）、``final_mismatch``（退齐未对齐整单）。
        """
        existing = self.find_by_request(request_key)
        if existing:
            return existing, "exists"
        now = datetime.now(timezone.utc).isoformat()
        batch_id = uuid.uuid4().hex[:12]
        ledger_id = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            order = db.execute("SELECT * FROM issuance_orders WHERE order_id=?", (order_id,)).fetchone()
            if order is None:
                db.rollback()
                return None, "order_missing"
            rows = db.execute("SELECT * FROM issuance_batches WHERE order_id=? ORDER BY seq", (order_id,)).fetchall()
            issued_cumulative = sum(int(row["amount"]) for row in rows)
            payment = int(order["payment"])

            # 累计退货数量校验（并发下也以已落库批次为准）。
            qty_by_line = {}
            for row in rows:
                for entry in json.loads(row["detail"])["lines"]:
                    qty_by_line[entry["line_id"]] = qty_by_line.get(entry["line_id"], 0) + int(entry["qty"])
            for entry in detail["lines"]:
                qty_by_line[entry["line_id"]] = qty_by_line.get(entry["line_id"], 0) + int(entry["qty"])
            order_lines = {line["line_id"]: line for line in json.loads(order["lines"])}
            if any(qty > order_lines[line_id]["qty"] for line_id, qty in qty_by_line.items()):
                db.rollback()
                return None, "qty_exceeded"

            cumulative = issued_cumulative + int(plan["amount"])
            if cumulative > payment:
                db.rollback()
                return None, "over_limit"
            if plan["complete"] and cumulative != int(order["full_target"]):
                db.rollback()
                return None, "final_mismatch"
            if int(plan["amount"]) < 0:
                db.rollback()
                return None, "negative"

            seq = (max((int(row["seq"]) for row in rows), default=0)) + 1
            db.execute('''INSERT INTO issuance_batches
                          (id,request_key,order_id,seq,amount,cumulative,freight_refund,is_final,status,detail,created_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                       (batch_id, request_key, order_id, seq, int(plan["amount"]), cumulative,
                        int(plan["freight_refund"]), 1 if plan["complete"] else 0, "ISSUED",
                        json.dumps(detail, ensure_ascii=False), now))
            db.execute('''INSERT INTO issuance_ledger(id,batch_id,order_id,debit,cumulative_after,created_at)
                          VALUES(?,?,?,?,?,?)''',
                       (ledger_id, batch_id, order_id, int(plan["amount"]), cumulative, now))
        return self.find_by_request(request_key), None

    def ledger_totals(self, order_id):
        with self.connect() as db:
            rows = db.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS total, "
                "COALESCE(SUM(freight_refund),0) AS freight FROM issuance_batches WHERE order_id=?",
                (order_id,)).fetchone()
            ledger_rows = db.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(debit),0) AS debit_total "
                "FROM issuance_ledger WHERE order_id=?", (order_id,)).fetchone()
        return {"batch_count": rows["n"], "ledger_count": ledger_rows["n"],
                "ledger_debit_total": int(ledger_rows["debit_total"]),
                "cumulative": int(rows["total"]), "freight_total": int(rows["freight"])}
