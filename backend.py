"""
Baraza Backend — connects Paystack payments to CJdropshipping order placement.

WHAT THIS DOES:
1. Storefront asks this server to start a payment  -> /api/checkout
2. Customer pays on Paystack's checkout page
3. Paystack confirms the payment back to this server -> /api/paystack/webhook
4. This server then places the real order with CJdropshipping automatically

WHY A BACKEND AT ALL:
Your Paystack secret key and CJ API key must never appear in the public
website's code (anyone could view-source and steal them). This server holds
both secrets privately and is the only thing that talks to Paystack/CJ
directly. The public website only ever talks to THIS server.

HOW TO RUN THIS LOCALLY (for testing):
1. pip install flask requests --break-system-packages   (or without that flag on your machine)
2. Set your keys as environment variables (see bottom of this file for how),
   OR just paste them into the CONFIG section below for local testing only —
   never commit real keys into code you publish anywhere public.
3. python backend.py
4. It runs at http://localhost:5000

IMPORTANT: Paystack's webhook needs a real public URL to reach this server —
localhost won't work for that part until this is deployed somewhere like
Render.com or Railway.app (both have free tiers). See the notes at the
bottom of this file for deployment steps.
"""

from flask import Flask, request, jsonify
import requests
import json
import os
import time

app = Flask(__name__)

# Allow the storefront (hosted on a different domain) to call this backend.
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

@app.route("/api/checkout", methods=["OPTIONS"])
@app.route("/api/paystack/webhook", methods=["OPTIONS"])
def handle_options():
    return "", 200

# ---------------------------------------------------------------
# CONFIG — for local testing you can paste values here directly.
# For anything real/deployed, use environment variables instead
# (see bottom of file) so the keys never end up in code you share.
# ---------------------------------------------------------------
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "PASTE_YOUR_sk_test_KEY_HERE")
CJ_EMAIL = os.environ.get("CJ_EMAIL", "PASTE_YOUR_CJ_ACCOUNT_EMAIL_HERE")
CJ_API_KEY = os.environ.get("CJ_API_KEY", "PASTE_YOUR_CJ_API_KEY_HERE")

ORDERS_FILE = "orders.json"  # simple local file standing in for a real database

# ---------------------------------------------------------------
# CJ access token — CJ tokens last 15 days, so we cache it in memory
# instead of requesting a new one on every single order.
# ---------------------------------------------------------------
_cj_token_cache = {"token": None, "expires_at": 0}

def get_cj_token():
    if _cj_token_cache["token"] and time.time() < _cj_token_cache["expires_at"]:
        return _cj_token_cache["token"]

    resp = requests.post(
        "https://developers.cjdropshipping.com/api2.0/v1/authentication/getAccessToken",
        json={"email": CJ_EMAIL, "apiKey": CJ_API_KEY},
    )
    data = resp.json()
    if not data.get("result"):
        raise Exception(f"CJ login failed: {data.get('message')}")

    token = data["data"]["accessToken"]
    _cj_token_cache["token"] = token
    _cj_token_cache["expires_at"] = time.time() + (14 * 24 * 60 * 60)  # refresh a day early
    return token


def load_orders():
    if os.path.exists(ORDERS_FILE):
        with open(ORDERS_FILE) as f:
            return json.load(f)
    return {}


def save_orders(orders):
    with open(ORDERS_FILE, "w") as f:
        json.dump(orders, f, indent=2)


# ---------------------------------------------------------------
# Step 1: Storefront calls this to start a payment.
# Expects JSON: { "email": "...", "amount_kes": 1234, "cart": [...], "shipping": {...} }
# ---------------------------------------------------------------
@app.route("/api/checkout", methods=["POST"])
def checkout():
    body = request.get_json()
    email = body.get("email")
    amount_kes = body.get("amount_kes")
    cart = body.get("cart", [])
    shipping = body.get("shipping", {})

    if not email or not amount_kes:
        return jsonify({"error": "email and amount_kes are required"}), 400

    resp = requests.post(
        "https://api.paystack.co/transaction/initialize",
        headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
        json={
            "email": email,
            "amount": int(amount_kes * 100),  # Paystack wants the smallest currency unit
            "currency": "KES",
        },
    )
    data = resp.json()
    if not data.get("status"):
        return jsonify({"error": data.get("message")}), 400

    reference = data["data"]["reference"]

    # Save the order as "pending" so we know what to fulfill once payment confirms
    orders = load_orders()
    orders[reference] = {
        "email": email,
        "cart": cart,
        "shipping": shipping,
        "status": "pending_payment",
    }
    save_orders(orders)

    return jsonify({
        "authorization_url": data["data"]["authorization_url"],
        "reference": reference,
    })


# ---------------------------------------------------------------
# Step 2: Paystack calls this automatically once payment succeeds.
# Register this URL in your Paystack dashboard under
# Settings -> API Keys & Webhooks -> Webhook URL, once this server
# has a real public address (see deployment notes at the bottom).
# ---------------------------------------------------------------
@app.route("/api/paystack/webhook", methods=["POST"])
def paystack_webhook():
    event = request.get_json()

    if event.get("event") != "charge.success":
        return jsonify({"received": True})  # ignore anything that isn't a successful payment

    reference = event["data"]["reference"]

    # Always re-verify with Paystack directly rather than trusting the webhook body alone
    verify_resp = requests.get(
        f"https://api.paystack.co/transaction/verify/{reference}",
        headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
    )
    verify_data = verify_resp.json()

    if not verify_data.get("status") or verify_data["data"]["status"] != "success":
        return jsonify({"error": "payment not verified"}), 400

    orders = load_orders()
    order = orders.get(reference)
    if not order:
        return jsonify({"error": "unknown order reference"}), 404

    if order["status"] == "fulfilled":
        return jsonify({"already_fulfilled": True})  # avoid placing the same order twice

    # Payment confirmed — now place the real order with CJdropshipping
    try:
        place_cj_order(order)
        order["status"] = "fulfilled"
    except Exception as e:
        order["status"] = "payment_ok_fulfillment_failed"
        order["error"] = str(e)

    orders[reference] = order
    save_orders(orders)

    return jsonify({"success": True})


def place_cj_order(order):
    """
    Places the real order with CJdropshipping.

    NOTE: CJ's createOrder endpoint needs each item's exact "vid" (variant ID),
    not just the product id — you get that from CJ's product/variant/query
    endpoint first. This function shows the shape of the call; you'll need to
    fill in real variant IDs and confirm required shipping fields against
    CJ's current API docs before this goes live with real orders.
    """
    token = get_cj_token()
    shipping = order["shipping"]

    payload = {
        "orderNumber": f"BZ-{int(time.time())}",
        "shippingCountryCode": shipping.get("countryCode", "KE"),
        "shippingProvince": shipping.get("province", ""),
        "shippingCity": shipping.get("city", ""),
        "shippingAddress": shipping.get("address", ""),
        "shippingCustomerName": shipping.get("name", ""),
        "shippingPhone": shipping.get("phone", ""),
        "shippingZip": shipping.get("zip", ""),
        "fromCountryCode": "CN",
        "logisticName": "CJPacket Ordinary",  # confirm available options for your route in CJ's dashboard
        "products": [
            {"vid": item.get("vid"), "quantity": item.get("qty", 1)}
            for item in order["cart"]
        ],
    }

    resp = requests.post(
        "https://developers.cjdropshipping.com/api2.0/v1/shopping/order/createOrder",
        headers={"CJ-Access-Token": token},
        json=payload,
    )
    data = resp.json()
    if not data.get("result"):
        raise Exception(f"CJ order failed: {data.get('message')}")
    return data


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)

# ---------------------------------------------------------------
# DEPLOYING THIS FOR REAL (so Paystack's webhook can reach it):
#
# 1. Create a free account at render.com or railway.app
# 2. Create a new "Web Service", connect it to a GitHub repo containing
#    this file (plus a requirements.txt with: flask, requests)
# 3. Set PAYSTACK_SECRET_KEY, CJ_EMAIL, CJ_API_KEY as environment
#    variables in that service's dashboard — NEVER commit real keys
#    into the code itself
# 4. Once deployed, you'll get a URL like https://baraza-backend.onrender.com
# 5. Paste that URL + /api/paystack/webhook into Paystack's dashboard
#    under Settings -> API Keys & Webhooks -> Webhook URL
# 6. Update the storefront's checkout button to call
#    https://baraza-backend.onrender.com/api/checkout instead of
#    simulating the order locally
# ---------------------------------------------------------------
