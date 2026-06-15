"""Stripe Checkout / billing-portal / webhook routes.

These are the only pieces that touch the Stripe secret key. They are written as
a self-contained APIRouter so you can lift `app/` straight into your existing
FastAPI repo: `from app.stripe_routes import router; app.include_router(router)`.
"""

from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from . import config
from .supabase_client import (
    get_profile,
    get_user_from_token,
    update_profile,
    update_profile_by_customer,
)

stripe.api_key = config.STRIPE_SECRET_KEY

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
    price_id = config.PLAN_TO_PRICE.get(body.plan)
    if not price_id:
        raise HTTPException(status_code=400, detail="Unknown plan.")

    customer_id = _ensure_customer(user)
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        client_reference_id=user.id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{config.FRONTEND_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{config.FRONTEND_URL}/onboarding?plan={body.plan}",
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
        return_url=f"{config.FRONTEND_URL}/account",
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
    if price_id in config.PRICE_TO_PLAN:
        values["plan"] = config.PRICE_TO_PLAN[price_id]
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


@router.post("/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(default=None)):
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, config.STRIPE_WEBHOOK_SECRET
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
    elif kind in ("customer.subscription.updated", "customer.subscription.deleted"):
        _apply_subscription(obj)

    return {"received": True}
