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
    supabase_admin,
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


def _get_or_create_supabase_user(email: str, stripe_customer_id: str) -> str | None:
    """
    Attempt to create a new Supabase user for *email*.
    If the user already exists, Supabase raises an error — we catch it and
    return None so _apply_subscription falls back to matching by stripe_customer_id.
    """
    if not supabase_admin:
        print("WARNING: supabase_admin not configured — cannot create user.")
        return None

    try:
        response = supabase_admin.auth.admin.create_user({
            "email": email,
            "email_confirm": True,  # mark email as confirmed immediately
        })
        user = response.user
        if not user:
            print(f"ERROR: create_user returned no user for {email}")
            return None

        print(f"Created Supabase user {user.id} for {email}")

        # Seed the profile with the Stripe customer id and terms acceptance.
        update_profile(user.id, {
            "stripe_customer_id": stripe_customer_id,
            "terms_accepted_at": datetime.now(timezone.utc).isoformat(),
            "terms_version": "2026-06-29",
        })
        return user.id
    except Exception as e:
        err = str(e).lower()
        if "already" in err or "exists" in err or "registered" in err:
            # User already has an account — subscription will be matched via
            # stripe_customer_id in _apply_subscription.
            print(f"User already exists for {email}, skipping creation.")
        else:
            print(f"ERROR: Failed to create Supabase user for {email}: {e}")
        return None


def _send_create_password_email(to_email: str) -> None:
    """Send a 'create your password' email containing a Supabase magic link
    that exchanges for a session and lands the user on /update-password."""
    if not SMTP_USERNAME or not SMTP_PASSWORD:
        print("WARNING: SMTP credentials not set, cannot send create-password email.")
        return

    # Generate a recovery / password-reset link via the admin API.
    # Supabase will construct a link that, when clicked, exchanges for a
    # session (via PKCE code) and redirects to redirect_to.
    magic_link = None
    if supabase_admin:
        try:
            res = supabase_admin.auth.admin.generate_link({
                "type": "recovery",
                "email": to_email,
                "options": {
                    "redirect_to": f"{FRONTEND_URL}/update-password",
                },
            })
            # The link lives in res.properties.action_link
            props = getattr(res, "properties", None)
            magic_link = getattr(props, "action_link", None)
        except Exception as e:
            print(f"WARNING: Could not generate magic link for {to_email}: {e}")

    if not magic_link:
        raise ValueError(f"Failed to generate magic link for {to_email}")

    msg = EmailMessage()
    msg['Subject'] = "Create your Skyward password"
    msg['From'] = SMTP_FROM_EMAIL
    msg['To'] = to_email

    content = f"""Hi,

Welcome to Skyward — your subscription is now active!

To get started, you'll first need to create a password for your account.
Click the link below to set your password (the link expires in 24 hours):

{magic_link}

Once you've created your password you'll be taken straight to setup,
where we'll walk you through everything before installing Skyward.

If you have any questions, just reply to this email and we'll be happy to help.

— The Skyward Team"""
    msg.set_content(content)

    html_content = f"""<html>
  <body>
    <p>Hi,</p>
    <p>Welcome to Skyward — your subscription is now active!</p>
    <p>To get started, you'll first need to create a password for your account.<br>
    Click the link below to set your password (the link expires in 24 hours):</p>
    <p><a href="{magic_link}">Create password</a></p>
    <p>Once you've created your password you'll be taken straight to setup,<br>
    where we'll walk you through everything before installing Skyward.</p>
    <p>If you have any questions, just reply to this email and we'll be happy to help.</p>
    <p>— The Skyward Team</p>
  </body>
</html>"""
    msg.add_alternative(html_content, subtype='html')

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        print(f"Create-password email sent to {to_email}")
    except Exception as e:
        print(f"ERROR sending create-password email to {to_email}: {e}")


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
        
        customer_details = getattr(obj, "customer_details", {}) or {}
        to_email = (
            customer_details.get("email") if isinstance(customer_details, dict)
            else getattr(customer_details, "email", None)
        )
        stripe_customer_id = getattr(obj, "customer", None)

        # --- Create (or look up) the Supabase user from the purchaser's email ---
        user_id = None
        if to_email and stripe_customer_id:
            user_id = _get_or_create_supabase_user(to_email, stripe_customer_id)
        elif to_email:
            print(f"WARNING: No stripe_customer_id on checkout session for {to_email}")

        if sub_id:
            subscription = stripe.Subscription.retrieve(sub_id)
            _apply_subscription(subscription, override_user_id=user_id)

        if to_email:
            try:
                _send_create_password_email(to_email)
            except Exception as e:
                print(f"CRITICAL: Failed to handle post-checkout email for {to_email}: {e}")
                raise HTTPException(status_code=500, detail="Failed to send create-password email.")
        else:
            print("WARNING: Could not find email in checkout session to send create-password email.")
    elif kind in ("customer.subscription.updated", "customer.subscription.deleted"):
        _apply_subscription(obj)

    return {"received": True}
