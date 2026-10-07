import json
import sqlite3
import secrets
import os
import subprocess
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "buildverse.db"

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))

ADMIN_KEY = os.environ.get("BUILDVERSE_ADMIN_KEY")
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "")

PRODUCTS = {
    1: ("Cinematic AI Video Prompt Pack", 15000),
    2: ("Nigerian Business Ad Prompt Pack", 10000),
    3: ("30 Viral Content Concepts", 7500),
    4: ("AI Commercial Starter Kit", 25000),
    5: ("Premium Brand Starter Kit", 35000),
    6: ("BuildVerse Storytelling Pack", 20000),
    7: ("Suno Music Prompt Collection", 10000),
    8: ("Social Content Calendar", 12000),
}


def get_db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = get_db()

    con.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no TEXT UNIQUE NOT NULL,
            customer_name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT NOT NULL,
            product_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            amount INTEGER NOT NULL,
            payment_status TEXT NOT NULL DEFAULT 'pending',
            delivery_status TEXT NOT NULL DEFAULT 'not_delivered',
            payment_reference TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            paid_at TEXT DEFAULT '',
            delivered_at TEXT DEFAULT '',
            items_json TEXT DEFAULT '[]'
        )
    """)

    columns = {row[1] for row in con.execute('PRAGMA table_info(orders)').fetchall()}
    if 'items_json' not in columns:
        con.execute("ALTER TABLE orders ADD COLUMN items_json TEXT DEFAULT '[]'")

    con.commit()
    con.close()


def json_response(handler, data, status=200):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")

    handler.send_response(status)
    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8"
    )
    handler.send_header(
        "Content-Length",
        str(len(body))
    )
    handler.end_headers()

    handler.wfile.write(body)


def read_json(handler):
    length = int(handler.headers.get("Content-Length", "0"))

    raw = handler.rfile.read(length)

    if not raw:
        return {}

    return json.loads(raw.decode("utf-8"))


def new_order_number():
    return "BV-" + secrets.token_hex(4).upper()


def paystack_request(url, method="GET", data=None):
    if not PAYSTACK_SECRET_KEY:
        raise Exception("PAYSTACK_SECRET_KEY is not configured")

    headers = [
        "-H", f"Authorization: Bearer {PAYSTACK_SECRET_KEY}",
        "-H", "Content-Type: application/json",
        "-H", "Cache-Control: no-cache",
        "-A", "curl/8.0"
    ]

    command = [
        "curl",
        "-sS",
        "-X", method,
        *headers,
        url
    ]

    if data is not None:
        command.extend([
            "-d",
            json.dumps(data)
        ])

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30
    )

    if result.returncode != 0:
        raise Exception(
            f"Paystack connection error: {result.stderr.strip()}"
        )

    try:
        return json.loads(result.stdout)
    except Exception:
        raise Exception(
            f"Invalid Paystack response: {result.stdout[:500]}"
        )


class BuildVerseServer(SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        super().__init__(
            *args,
            directory=str(BASE),
            **kwargs
        )

    def admin_authorized(self):
        key = self.headers.get("X-Admin-Key", "")

        return secrets.compare_digest(
            key,
            ADMIN_KEY
        )

    def do_GET(self):

        path = urlparse(self.path).path

        # Health check
        if path == "/api/health":

            return json_response(
                self,
                {
                    "ok": True,
                    "service": "BuildVerse Store API"
                }
            )

        # Customer order tracking
        if path == "/api/orders/track":

            query = urlparse(self.path).query
            order_no = ""

            for item in query.split("&"):
                if item.startswith("order_no="):
                    order_no = item.split("=", 1)[1]

            if not order_no:
                return json_response(
                    self,
                    {"error": "Order number is required."},
                    400
                )

            con = get_db()

            order = con.execute(
                """
                SELECT
                    order_no,
                    customer_name,
                    product_name,
                    amount,
                    payment_status,
                    delivery_status,
                    created_at,
                    paid_at,
                    items_json
                FROM orders
                WHERE order_no = ?
                """,
                (order_no,)
            ).fetchone()

            con.close()

            if not order:
                return json_response(
                    self,
                    {"error": "Order not found."},
                    404
                )

            return json_response(
                self,
                dict(order)
            )

        # Products
        if path.startswith("/api/payments/verify"):
            try:
                query = urlparse(self.path).query
                reference = ""

                for item in query.split("&"):
                    if item.startswith("reference="):
                        reference = item.split("=", 1)[1]

                if not reference:
                    return json_response(
                        self,
                        {"error": "Payment reference is required"},
                        400
                    )

                result = paystack_request(
                    "https://api.paystack.co/transaction/verify/"
                    + reference
                )

                if not result.get("status"):
                    return json_response(
                        self,
                        {"error": "Unable to verify payment"},
                        400
                    )

                payment = result.get("data", {})

                con = get_db()

                order = con.execute(
                    """
                    SELECT * FROM orders
                    WHERE payment_reference = ?
                    """,
                    (reference,)
                ).fetchone()

                if not order:
                    con.close()
                    return json_response(
                        self,
                        {"error": "Order not found"},
                        404
                    )

                expected_amount = int(order["amount"]) * 100
                paid_amount = int(payment.get("amount", 0))
                status = payment.get("status")
                currency = payment.get("currency")

                if status != "success":
                    con.close()
                    return json_response(
                        self,
                        {
                            "ok": False,
                            "paid": False,
                            "payment_status": status
                        }
                    )

                if paid_amount != expected_amount:
                    con.close()
                    return json_response(
                        self,
                        {"error": "Payment amount does not match order"},
                        400
                    )

                if currency != "NGN":
                    con.close()
                    return json_response(
                        self,
                        {"error": "Payment currency is invalid"},
                        400
                    )

                con.execute(
                    """
                    UPDATE orders
                    SET payment_status = 'paid',
                        paid_at = ?
                    WHERE id = ?
                    """,
                    (
                        datetime.now(timezone.utc).isoformat(),
                        order["id"]
                    )
                )

                con.commit()
                con.close()

                return json_response(
                    self,
                    {
                        "ok": True,
                        "paid": True,
                        "order_no": order["order_no"],
                        "payment_status": "paid"
                    }
                )

            except Exception as error:
                return json_response(
                    self,
                    {"error": str(error)},
                    500
                )

        if path == "/api/products":

            products = []

            for product_id, product in PRODUCTS.items():

                products.append({
                    "id": product_id,
                    "name": product[0],
                    "amount": product[1]
                })

            return json_response(
                self,
                products
            )

        # Admin orders
        if path == "/api/admin/orders":

            if not self.admin_authorized():

                return json_response(
                    self,
                    {"error": "Unauthorized"},
                    401
                )

            con = get_db()

            rows = con.execute("""
                SELECT *
                FROM orders
                ORDER BY id DESC
            """).fetchall()

            con.close()

            return json_response(
                self,
                [dict(row) for row in rows]
            )

        # Admin summary
        if path == "/api/admin/summary":

            if not self.admin_authorized():

                return json_response(
                    self,
                    {"error": "Unauthorized"},
                    401
                )

            con = get_db()

            total_orders = con.execute(
                "SELECT COUNT(*) FROM orders"
            ).fetchone()[0]

            paid_orders = con.execute(
                """
                SELECT COUNT(*)
                FROM orders
                WHERE payment_status = 'paid'
                """
            ).fetchone()[0]

            pending_orders = con.execute(
                """
                SELECT COUNT(*)
                FROM orders
                WHERE payment_status = 'pending'
                """
            ).fetchone()[0]

            revenue = con.execute(
                """
                SELECT COALESCE(SUM(amount), 0)
                FROM orders
                WHERE payment_status = 'paid'
                """
            ).fetchone()[0]

            customers = con.execute(
                """
                SELECT COUNT(DISTINCT email)
                FROM orders
                """
            ).fetchone()[0]

            con.close()

            return json_response(
                self,
                {
                    "total_orders": total_orders,
                    "paid_orders": paid_orders,
                    "pending": pending_orders,
                    "revenue": revenue,
                    "customers": customers
                }
            )

        # Normal website files
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/orders":
            try:
                data = read_json(self)

                name = str(data.get("name", "")).strip()
                email = str(data.get("email", "")).strip()
                phone = str(data.get("phone", "")).strip()

                raw_items = data.get("items", [])

                # Backward compatibility with the old single-product format
                if not raw_items and data.get("product_id") is not None:
                    raw_items = [{
                        "product_id": data.get("product_id"),
                        "quantity": 1
                    }]

                if not name or not email or not phone:
                    return json_response(
                        self,
                        {
                            "error":
                            "Name, email and phone are required."
                        },
                        400
                    )

                if not isinstance(raw_items, list) or not raw_items:
                    return json_response(
                        self,
                        {"error": "Your cart is empty."},
                        400
                    )

                if len(raw_items) > 20:
                    return json_response(
                        self,
                        {"error": "Too many different products."},
                        400
                    )

                items = []
                total = 0
                names = []

                for item in raw_items:
                    try:
                        product_id = int(item.get("product_id"))
                        quantity = int(item.get("quantity", 1))
                    except Exception:
                        return json_response(
                            self,
                            {"error": "Invalid cart item."},
                            400
                        )

                    if product_id not in PRODUCTS:
                        return json_response(
                            self,
                            {"error": "Invalid product in cart."},
                            400
                        )

                    if quantity < 1 or quantity > 99:
                        return json_response(
                            self,
                            {"error": "Invalid product quantity."},
                            400
                        )

                    product_name, unit_price = PRODUCTS[product_id]

                    line_total = unit_price * quantity
                    total += line_total
                    names.append(
                        f"{product_name} x{quantity}"
                    )

                    items.append({
                        "product_id": product_id,
                        "product_name": product_name,
                        "quantity": quantity,
                        "unit_price": unit_price,
                        "line_total": line_total
                    })

                order_number = new_order_number()
                created_at = datetime.now(timezone.utc).isoformat()

                con = get_db()

                con.execute(
                    """
                    INSERT INTO orders (
                        order_no,
                        customer_name,
                        email,
                        phone,
                        product_id,
                        product_name,
                        amount,
                        payment_status,
                        delivery_status,
                        payment_reference,
                        created_at,
                        items_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_number,
                        name,
                        email,
                        phone,
                        items[0]["product_id"],
                        " + ".join(names),
                        total,
                        "pending",
                        "not_delivered",
                        "",
                        created_at,
                        json.dumps(items, ensure_ascii=False)
                    )
                )

                con.commit()
                con.close()

                print(
                    f"NEW ORDER: {order_number} | "
                    f"{name} | {len(items)} item(s) | "
                    f"₦{total:,}"
                )

                return json_response(
                    self,
                    {
                        "ok": True,
                        "order_no": order_number,
                        "amount": total,
                        "payment_status": "pending",
                        "items": items
                    },
                    201
                )

            except Exception as error:
                return json_response(
                    self,
                    {"error": str(error)},
                    400
                )

        if path == "/api/payments/initialize":
            try:
                data = read_json(self)
                order_no = str(data.get("order_no", "")).strip()

                if not order_no:
                    return json_response(
                        self,
                        {"error": "Order number is required."},
                        400
                    )

                con = get_db()

                order = con.execute(
                    """
                    SELECT * FROM orders
                    WHERE order_no = ?
                    """,
                    (order_no,)
                ).fetchone()

                if not order:
                    con.close()
                    return json_response(
                        self,
                        {"error": "Order not found."},
                        404
                    )

                if order["payment_status"] == "paid":
                    con.close()
                    return json_response(
                        self,
                        {"error": "Order is already paid."},
                        400
                    )

                reference = (
                    order["order_no"]
                    + "-"
                    + secrets.token_hex(4).upper()
                )

                amount_kobo = int(order["amount"]) * 100

                result = paystack_request(
                    "https://api.paystack.co/transaction/initialize",
                    method="POST",
                    data={
                        "email": order["email"],
                        "amount": amount_kobo,
                        "currency": "NGN",
                        "reference": reference
                    }
                )

                if not result.get("status"):
                    con.close()
                    return json_response(
                        self,
                        {
                            "error": result.get(
                                "message",
                                "Unable to initialize payment."
                            )
                        },
                        400
                    )

                con.execute(
                    """
                    UPDATE orders
                    SET payment_reference = ?
                    WHERE id = ?
                    """,
                    (reference, order["id"])
                )

                con.commit()
                con.close()

                return json_response(
                    self,
                    {
                        "ok": True,
                        "order_no": order["order_no"],
                        "reference": reference,
                        "access_code": result["data"]["access_code"],
                        "authorization_url":
                            result["data"]["authorization_url"]
                    }
                )

            except Exception as error:
                return json_response(
                    self,
                    {"error": str(error)},
                    500
                )

    def do_PATCH(self):

        path = urlparse(self.path).path

        parts = path.strip("/").split("/")

        if (
            len(parts) != 4
            or parts[0] != "api"
            or parts[1] != "admin"
            or parts[2] != "orders"
        ):

            return json_response(
                self,
                {"error": "Not found"},
                404
            )

        if not self.admin_authorized():

            return json_response(
                self,
                {"error": "Unauthorized"},
                401
            )

        try:

            order_id = int(parts[3])

            data = read_json(self)

            updates = []
            values = []

            payment_status = data.get(
                "payment_status"
            )

            if payment_status in (
                "pending",
                "paid",
                "failed"
            ):

                updates.append(
                    "payment_status = ?"
                )

                values.append(
                    payment_status
                )

                if payment_status == "paid":

                    updates.append(
                        "paid_at = ?"
                    )

                    values.append(
                        datetime.now(
                            timezone.utc
                        ).isoformat()
                    )

            delivery_status = data.get(
                "delivery_status"
            )

            if delivery_status in (
                "not_delivered",
                "delivered"
            ):

                updates.append(
                    "delivery_status = ?"
                )

                values.append(
                    delivery_status
                )

                if delivery_status == "delivered":

                    updates.append(
                        "delivered_at = ?"
                    )

                    values.append(
                        datetime.now(
                            timezone.utc
                        ).isoformat()
                    )

            if not updates:

                return json_response(
                    self,
                    {
                        "error":
                        "No valid changes supplied"
                    },
                    400
                )

            values.append(order_id)

            con = get_db()

            cursor = con.execute(
                """
                UPDATE orders
                SET
                """ + ", ".join(updates) + """
                WHERE id = ?
                """,
                values
            )

            con.commit()
            con.close()

            if cursor.rowcount == 0:

                return json_response(
                    self,
                    {
                        "error":
                        "Order not found"
                    },
                    404
                )

            return json_response(
                self,
                {"ok": True}
            )

        except Exception as error:

            return json_response(
                self,
                {
                    "error": str(error)
                },
                400
            )


ThreadingHTTPServer.allow_reuse_address = True
if __name__ == "__main__":

    init_db()

    print("")
    print("====================================")
    print("   BUILDVERSE STORE BACKEND")
    print("====================================")
    print("")
    print(
        "Store:  http://127.0.0.1:8080"
    )
    print(
        "Admin:  http://127.0.0.1:8080/admin.html"
    )
    print("")
    print("Admin key:")
    print("Admin key loaded from environment")
    print("")
    print("Server running...")
    print("====================================")
    print("")

    server = ThreadingHTTPServer(
        (HOST, PORT),
        BuildVerseServer
    )

    server.serve_forever()
