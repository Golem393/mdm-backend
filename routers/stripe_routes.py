"""Stripe Checkout / billing-portal / webhook routes.

These are the only pieces that touch the Stripe secret key. They are written as
a self-contained APIRouter so you can lift `app/` straight into your existing
FastAPI repo: `from app.stripe_routes import router; app.include_router(router)`.
"""

from datetime import datetime, timezone
import smtplib
from email.message import EmailMessage

import stripe
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

import os
from .apps import (
    get_profile,
    get_user_from_token,
    update_profile,
    update_profile_by_customer,
)

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", SMTP_USERNAME)

PRICE_TO_PLAN = {
    os.getenv("STRIPE_PRICE_MONTHLY"): "monthly",
    os.getenv("STRIPE_PRICE_YEARLY"): "yearly",
}

PLAN_TO_PRICE = {
    "monthly": os.getenv("STRIPE_PRICE_MONTHLY"),
    "yearly": os.getenv("STRIPE_PRICE_YEARLY"),
}

stripe.api_key = STRIPE_SECRET_KEY

router = APIRouter()


def _authed_user(authorization: str | None):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token.")
    token = authorization.split(" ", 1)[1]
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session.")
    return user


def _ensure_customer(user) -> str:
    """Return the user's Stripe customer id, creating + persisting one if needed."""
    profile = get_profile(user.id) or {}
    customer_id = profile.get("stripe_customer_id")
    if customer_id:
        return customer_id

    customer = stripe.Customer.create(
        email=user.email,
        metadata={"supabase_user_id": user.id},
    )
    update_profile(user.id, {"stripe_customer_id": customer.id})
    return customer.id


class CheckoutBody(BaseModel):
    plan: str  # "monthly" | "yearly"


@router.post("/checkout")
def create_checkout(body: CheckoutBody, authorization: str | None = Header(default=None)):
    user = _authed_user(authorization)
    price_id = PLAN_TO_PRICE.get(body.plan)
    if not price_id:
        raise HTTPException(status_code=400, detail="Unknown plan.")

    customer_id = _ensure_customer(user)
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        client_reference_id=user.id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{FRONTEND_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{FRONTEND_URL}/onboarding?plan={body.plan}",
        metadata={"supabase_user_id": user.id, "plan": body.plan},
        subscription_data={"metadata": {"supabase_user_id": user.id}},
    )
    return {"url": session.url}


@router.post("/portal")
def create_portal(authorization: str | None = Header(default=None)):
    user = _authed_user(authorization)
    profile = get_profile(user.id) or {}
    customer_id = profile.get("stripe_customer_id")
    if not customer_id:
        raise HTTPException(status_code=400, detail="No billing account yet.")

    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=f"{FRONTEND_URL}/account",
    )
    return {"url": session.url}


def _apply_subscription(subscription, override_user_id=None) -> None:
    """Sync a Stripe subscription object onto the matching profile row."""
    customer_id = getattr(subscription, "customer", None)
    status = getattr(subscription, "status", None)  # active | canceled | past_due | ...
    
    items = getattr(subscription, "items", None)
    data = getattr(items, "data", []) if items else []
    price_id = None
    if data and len(data) > 0:
        price = getattr(data[0], "price", None)
        if price:
            price_id = getattr(price, "id", None)
            
    period_end = getattr(subscription, "current_period_end", None)

    values = {
        "subscription_status": status,
        "stripe_subscription_id": getattr(subscription, "id", None),
    }
    if price_id in PRICE_TO_PLAN:
        values["plan"] = PRICE_TO_PLAN[price_id]
    if period_end:
        values["current_period_end"] = datetime.fromtimestamp(
            period_end, tz=timezone.utc
        ).isoformat()


    # Prefer the explicit user id (passed in or set on the subscription metadata) and fall
    # back to matching on the Stripe customer id.
    metadata = getattr(subscription, "metadata", {}) or {}
    metadata_user_id = metadata.get("supabase_user_id") if isinstance(metadata, dict) else getattr(metadata, "supabase_user_id", None)
    user_id = override_user_id or metadata_user_id
    
    if user_id:
        update_profile(user_id, values)
    elif customer_id:
        update_profile_by_customer(customer_id, values)
    else:
        print("DEBUG _apply_subscription: No user_id or customer_id found to update!")


def _send_setup_email(to_email: str):
    if not SMTP_USERNAME or not SMTP_PASSWORD:
        print("WARNING: SMTP credentials not set, cannot send setup email.")
        return

    msg = EmailMessage()
    msg['Subject'] = "Welcome to Skyward"
    msg['From'] = SMTP_FROM_EMAIL
    msg['To'] = to_email

    content = """Hi,

Welcome to Skyward.

Your subscription is now active and you're ready to begin setup.

To get started, please use the link below:

https://skywardos.com/setup

The setup guide will walk you through everything you need to do before installing Skyward, including important steps to help protect your data.

If you have any questions or run into any issues during setup, simply reply to this email and we'll be happy to help.

— The Skyward Team"""
    msg.set_content(content)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        print(f"Setup email sent successfully to {to_email}")
    except Exception as e:
        print(f"Error sending setup email to {to_email}: {e}")


@router.post("/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(default=None)):
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")

    kind = event["type"]
    obj = event["data"]["object"]

    if kind == "checkout.session.completed":
        # Fetch the full subscription so we get status / period / price.
        sub_id = getattr(obj, "subscription", None)
        if sub_id:
            subscription = stripe.Subscription.retrieve(sub_id)
            # Carry the user id from the checkout session metadata.
            metadata = getattr(obj, "metadata", {}) or {}
            checkout_user_id = (
                metadata.get("supabase_user_id") if isinstance(metadata, dict) else getattr(metadata, "supabase_user_id", None)
            ) or getattr(obj, "client_reference_id", None)
            
            _apply_subscription(subscription, override_user_id=checkout_user_id)
            
            customer_details = getattr(obj, "customer_details", {}) or {}
            to_email = customer_details.get("email") if isinstance(customer_details, dict) else getattr(customer_details, "email", None)
            if to_email:
                _send_setup_email(to_email)
            else:
                print("WARNING: Could not find email in checkout session to send setup email.")
    elif kind in ("customer.subscription.updated", "customer.subscription.deleted"):
        _apply_subscription(obj)

    return {"received": True}
