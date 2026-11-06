import os
import calendar
from datetime import date
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in .env")

app = FastAPI(
    title="SetAndForget API",
    description="Subscription tracking and roommate bill splitting",
    version="1.0.0"
)

# Change these origins to your actual frontend address in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------
# AUTHENTICATION
# --------------------------------------------------

def get_db(authorization: Optional[str] = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid authorization token"
        )

    token = authorization.split(" ", 1)[1]

    try:
        db: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

        # Verify the user's Supabase access token.
        auth_response = db.auth.get_user(token)

        if not auth_response or not auth_response.user:
            raise HTTPException(status_code=401, detail="Invalid token")

        # Forward the user's JWT so PostgreSQL RLS applies.
        db.postgrest.auth(token)

        return db, auth_response.user

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=401,
            detail="Authentication failed"
        )


def handle_db_error(error):
    raise HTTPException(status_code=400, detail=str(error))


def get_owned_row(db, table, row_id, owner_id):
    result = (
        db.table(table)
        .select("*")
        .eq("id", row_id)
        .eq("owner_id", owner_id)
        .maybe_single()
        .execute()
    )

    if not result or not result.data:
        raise HTTPException(status_code=404, detail=f"{table} record not found")

    return result.data


def clean_payload(payload, allowed_fields):
    return {
        key: value
        for key, value in payload.items()
        if key in allowed_fields
    }


# --------------------------------------------------
# HOME
# --------------------------------------------------

@app.get("/")
def home():
    return {
        "message": "SetAndForget API is running",
        "status": "online"
    }


@app.get("/api/me")
def get_me(auth=Depends(get_db)):
    db, user = auth

    return {
        "id": user.id,
        "email": user.email
    }


# --------------------------------------------------
# USER SETTINGS
# --------------------------------------------------

SETTINGS_FIELDS = {
    "venmo_handle",
    "zelle_email",
    "payment_link",
    "auto_clear_balances",
    "trial_alerts",
    "unpaid_share_alerts",
    "weekly_summary",
    "trial_warning_days",
    "due_soon_alerts",
    "unpaid_warning_days",
    "currency",
    "default_category"
}


@app.get("/api/settings")
def get_settings(auth=Depends(get_db)):
    db, user = auth

    result = (
        db.table("user_settings")
        .select("*")
        .eq("user_id", user.id)
        .maybe_single()
        .execute()
    )

    return (result.data if result else None) or {}


@app.patch("/api/settings")
def update_settings(payload: dict, auth=Depends(get_db)):
    db, user = auth

    data = clean_payload(payload, SETTINGS_FIELDS)

    if not data:
        raise HTTPException(status_code=400, detail="No valid settings supplied")

    data["user_id"] = user.id

    try:
        result = (
            db.table("user_settings")
            .upsert(data, on_conflict="user_id")
            .execute()
        )
        return result.data[0] if result.data else {"message": "Settings saved"}

    except Exception as error:
        handle_db_error(error)


# --------------------------------------------------
# SUBSCRIPTIONS
# --------------------------------------------------

SUBSCRIPTION_FIELDS = {
    "name",
    "description",
    "cost",
    "currency",
    "billing_cycle",
    "next_billing_date",
    "category",
    "is_trial",
    "trial_end_date",
    "is_active"
}


@app.get("/api/subscriptions")
def get_subscriptions(auth=Depends(get_db)):
    db, user = auth

    result = (
        db.table("subscriptions")
        .select("*")
        .eq("owner_id", user.id)
        .order("created_at", desc=True)
        .execute()
    )

    return result.data


@app.post("/api/subscriptions")
def add_subscription(payload: dict, auth=Depends(get_db)):
    db, user = auth

    data = clean_payload(payload, SUBSCRIPTION_FIELDS)

    if not data.get("name") or data.get("cost") is None:
        raise HTTPException(
            status_code=400,
            detail="Subscription name and cost are required"
        )

    data["owner_id"] = user.id

    try:
        result = db.table("subscriptions").insert(data).execute()
        return result.data[0]

    except Exception as error:
        handle_db_error(error)


@app.patch("/api/subscriptions/{subscription_id}")
def edit_subscription(
    subscription_id: str,
    payload: dict,
    auth=Depends(get_db)
):
    db, user = auth

    get_owned_row(db, "subscriptions", subscription_id, user.id)

    data = clean_payload(payload, SUBSCRIPTION_FIELDS)

    if not data:
        raise HTTPException(status_code=400, detail="No valid fields supplied")

    try:
        result = (
            db.table("subscriptions")
            .update(data)
            .eq("id", subscription_id)
            .eq("owner_id", user.id)
            .execute()
        )
        return result.data[0]

    except Exception as error:
        handle_db_error(error)


@app.delete("/api/subscriptions/{subscription_id}")
def delete_subscription(subscription_id: str, auth=Depends(get_db)):
    db, user = auth

    get_owned_row(db, "subscriptions", subscription_id, user.id)

    db.table("subscriptions").delete() \
        .eq("id", subscription_id) \
        .eq("owner_id", user.id) \
        .execute()

    return {"message": "Subscription deleted"}


# --------------------------------------------------
# ROOMMATES
# --------------------------------------------------

ROOMMATE_FIELDS = {
    "name",
    "email",
    "payment_handle"
}


@app.get("/api/roommates")
def get_roommates(auth=Depends(get_db)):
    db, user = auth

    result = (
        db.table("roommates")
        .select("*")
        .eq("owner_id", user.id)
        .order("created_at", desc=True)
        .execute()
    )

    return result.data


@app.post("/api/roommates")
def add_roommate(payload: dict, auth=Depends(get_db)):
    db, user = auth

    data = clean_payload(payload, ROOMMATE_FIELDS)

    if not data.get("name"):
        raise HTTPException(status_code=400, detail="Roommate name is required")

    data["owner_id"] = user.id

    try:
        result = db.table("roommates").insert(data).execute()
        return result.data[0]

    except Exception as error:
        handle_db_error(error)


@app.delete("/api/roommates/{roommate_id}")
def delete_roommate(roommate_id: str, auth=Depends(get_db)):
    db, user = auth

    get_owned_row(db, "roommates", roommate_id, user.id)

    db.table("roommates").delete() \
        .eq("id", roommate_id) \
        .eq("owner_id", user.id) \
        .execute()

    return {"message": "Roommate deleted"}


# --------------------------------------------------
# SUBSCRIPTION SHARES
# --------------------------------------------------

@app.get("/api/subscription-shares")
def get_all_subscription_shares(auth=Depends(get_db)):
    db, user = auth

    result = (
        db.table("subscription_shares")
        .select("*, roommates(name)")
        .eq("owner_id", user.id)
        .execute()
    )

    return result.data


@app.get("/api/subscriptions/{subscription_id}/shares")
def get_subscription_shares(subscription_id: str, auth=Depends(get_db)):
    db, user = auth

    get_owned_row(db, "subscriptions", subscription_id, user.id)

    result = (
        db.table("subscription_shares")
        .select("*, roommates(name, email)")
        .eq("subscription_id", subscription_id)
        .eq("owner_id", user.id)
        .execute()
    )

    return result.data


@app.post("/api/subscriptions/{subscription_id}/shares")
def add_subscription_share(
    subscription_id: str,
    payload: dict,
    auth=Depends(get_db)
):
    db, user = auth

    get_owned_row(db, "subscriptions", subscription_id, user.id)

    roommate_id = payload.get("roommate_id")
    share_amount = payload.get("share_amount")

    if not roommate_id or share_amount is None:
        raise HTTPException(
            status_code=400,
            detail="roommate_id and share_amount are required"
        )

    get_owned_row(db, "roommates", roommate_id, user.id)

    data = {
        "subscription_id": subscription_id,
        "roommate_id": roommate_id,
        "owner_id": user.id,
        "share_amount": share_amount,
        "is_settled": bool(payload.get("is_settled", False))
    }

    try:
        result = db.table("subscription_shares").insert(data).execute()
        return result.data[0]

    except Exception as error:
        handle_db_error(error)


@app.patch("/api/subscription-shares/{share_id}")
def update_subscription_share(
    share_id: str,
    payload: dict,
    auth=Depends(get_db)
):
    db, user = auth

    updates = {}

    if "is_settled" in payload:
        updates["is_settled"] = bool(payload["is_settled"])

    if "share_amount" in payload:
        try:
            amount = round(float(payload["share_amount"]), 2)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="share_amount must be a number")

        if amount < 0:
            raise HTTPException(status_code=400, detail="share_amount cannot be negative")

        updates["share_amount"] = amount

    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields supplied")

    try:
        result = (
            db.table("subscription_shares")
            .update(updates)
            .eq("id", share_id)
            .eq("owner_id", user.id)
            .execute()
        )
    except Exception as error:
        handle_db_error(error)

    if not result.data:
        raise HTTPException(status_code=404, detail="Share not found")

    return result.data[0]


# --------------------------------------------------
# GROUPS
# --------------------------------------------------

@app.get("/api/groups")
def get_groups(auth=Depends(get_db)):
    db, user = auth

    result = (
        db.table("groups")
        .select("*")
        .eq("owner_id", user.id)
        .order("created_at", desc=True)
        .execute()
    )

    return result.data


@app.post("/api/groups")
def add_group(payload: dict, auth=Depends(get_db)):
    db, user = auth

    name = payload.get("name")

    if not name:
        raise HTTPException(status_code=400, detail="Group name is required")

    data = {
        "owner_id": user.id,
        "name": name,
        "description": payload.get("description"),
        "currency": payload.get("currency", "USD")
    }

    try:
        result = db.table("groups").insert(data).execute()
        group = result.data[0]

        # Add the group owner as its first member.
        db.table("group_members").insert({
            "group_id": group["id"],
            "owner_id": user.id,
            "display_name": payload.get("owner_name", "Owner"),
            "is_owner": True
        }).execute()

        return group

    except Exception as error:
        handle_db_error(error)


@app.delete("/api/groups/{group_id}")
def delete_group(group_id: str, auth=Depends(get_db)):
    db, user = auth

    get_owned_row(db, "groups", group_id, user.id)

    db.table("groups").delete() \
        .eq("id", group_id) \
        .eq("owner_id", user.id) \
        .execute()

    return {"message": "Group deleted"}


# --------------------------------------------------
# GROUP MEMBERS
# --------------------------------------------------

@app.get("/api/groups/{group_id}/members")
def get_group_members(group_id: str, auth=Depends(get_db)):
    db, user = auth

    get_owned_row(db, "groups", group_id, user.id)

    result = (
        db.table("group_members")
        .select("*")
        .eq("group_id", group_id)
        .eq("owner_id", user.id)
        .execute()
    )

    return result.data


@app.post("/api/groups/{group_id}/members")
def add_group_member(
    group_id: str,
    payload: dict,
    auth=Depends(get_db)
):
    db, user = auth

    get_owned_row(db, "groups", group_id, user.id)

    if not payload.get("display_name"):
        raise HTTPException(status_code=400, detail="Member name is required")

    data = {
        "group_id": group_id,
        "owner_id": user.id,
        "display_name": payload["display_name"],
        "is_owner": False
    }

    try:
        result = db.table("group_members").insert(data).execute()
        return result.data[0]

    except Exception as error:
        handle_db_error(error)


# --------------------------------------------------
# GROUP BILLS
# --------------------------------------------------

BILL_FIELDS = {
    "title",
    "description",
    "total_cost",
    "currency",
    "billing_cycle",
    "payer_member_id",
    "next_billing_date",
    "is_active"
}


@app.get("/api/groups/{group_id}/bills")
def get_group_bills(group_id: str, auth=Depends(get_db)):
    db, user = auth

    get_owned_row(db, "groups", group_id, user.id)

    result = (
        db.table("group_bills")
        .select("*")
        .eq("group_id", group_id)
        .eq("owner_id", user.id)
        .order("created_at", desc=True)
        .execute()
    )

    return result.data


@app.post("/api/groups/{group_id}/bills")
def add_group_bill(
    group_id: str,
    payload: dict,
    auth=Depends(get_db)
):
    db, user = auth

    get_owned_row(db, "groups", group_id, user.id)

    data = clean_payload(payload, BILL_FIELDS)

    if not data.get("title") or data.get("total_cost") is None:
        raise HTTPException(
            status_code=400,
            detail="Bill title and total_cost are required"
        )

    payer_id = data.get("payer_member_id")

    if payer_id:
        member = (
            db.table("group_members")
            .select("id")
            .eq("id", payer_id)
            .eq("group_id", group_id)
            .eq("owner_id", user.id)
            .maybe_single()
            .execute()
        )

        if not member or not member.data:
            raise HTTPException(status_code=400, detail="Invalid payer member")

    # Optional: create the shares together with the bill.
    shares_in = payload.get("shares") or []

    if shares_in:
        members_res = (
            db.table("group_members")
            .select("id")
            .eq("group_id", group_id)
            .eq("owner_id", user.id)
            .execute()
        )
        valid_ids = {m["id"] for m in (members_res.data or [])}

        for sh in shares_in:
            if sh.get("member_id") not in valid_ids or sh.get("share_amount") is None:
                raise HTTPException(status_code=400, detail="Invalid bill share")

    data["group_id"] = group_id
    data["owner_id"] = user.id

    try:
        result = db.table("group_bills").insert(data).execute()
        bill = result.data[0]
    except Exception as error:
        handle_db_error(error)

    if shares_in:
        rows = [
            {
                "bill_id": bill["id"],
                "group_id": group_id,
                "owner_id": user.id,
                "member_id": sh["member_id"],
                "share_amount": sh["share_amount"],
                "periods_owed": int(sh.get("periods_owed", 1)),
                "is_settled": bool(sh.get("is_settled", False)),
            }
            for sh in shares_in
        ]

        try:
            db.table("group_bill_shares").insert(rows).execute()
        except Exception as error:
            # Roll back so we never leave a bill without its shares.
            db.table("group_bills").delete().eq("id", bill["id"]).eq("owner_id", user.id).execute()
            handle_db_error(error)

    return bill


# --------------------------------------------------
# GROUP BILL SHARES
# --------------------------------------------------

@app.get("/api/bills/{bill_id}/shares")
def get_bill_shares(bill_id: str, auth=Depends(get_db)):
    db, user = auth

    get_owned_row(db, "group_bills", bill_id, user.id)

    result = (
        db.table("group_bill_shares")
        .select("*")
        .eq("bill_id", bill_id)
        .eq("owner_id", user.id)
        .execute()
    )

    return result.data


@app.post("/api/bills/{bill_id}/shares")
def add_bill_share(
    bill_id: str,
    payload: dict,
    auth=Depends(get_db)
):
    db, user = auth

    bill = get_owned_row(db, "group_bills", bill_id, user.id)

    member_id = payload.get("member_id")
    share_amount = payload.get("share_amount")

    if not member_id or share_amount is None:
        raise HTTPException(
            status_code=400,
            detail="member_id and share_amount are required"
        )

    member = (
        db.table("group_members")
        .select("id")
        .eq("id", member_id)
        .eq("group_id", bill["group_id"])
        .eq("owner_id", user.id)
        .maybe_single()
        .execute()
    )

    if not member or not member.data:
        raise HTTPException(status_code=400, detail="Invalid group member")

    data = {
        "bill_id": bill_id,
        "group_id": bill["group_id"],
        "owner_id": user.id,
        "member_id": member_id,
        "share_amount": share_amount,
        "periods_owed": payload.get("periods_owed", 1),
        "is_settled": False
    }

    try:
        result = db.table("group_bill_shares").insert(data).execute()
        return result.data[0]

    except Exception as error:
        handle_db_error(error)


@app.patch("/api/bill-shares/{share_id}")
def update_bill_share(
    share_id: str,
    payload: dict,
    auth=Depends(get_db)
):
    db, user = auth

    updates = {}

    if "is_settled" in payload:
        updates["is_settled"] = bool(payload["is_settled"])

    if "periods_owed" in payload:
        updates["periods_owed"] = payload["periods_owed"]

    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields supplied")

    result = (
        db.table("group_bill_shares")
        .update(updates)
        .eq("id", share_id)
        .eq("owner_id", user.id)
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=404, detail="Bill share not found")

    return result.data[0]


# --------------------------------------------------
# GROUP ACTIVITY
# --------------------------------------------------

@app.get("/api/groups/{group_id}/activity")
def get_group_activity(group_id: str, auth=Depends(get_db)):
    db, user = auth

    get_owned_row(db, "groups", group_id, user.id)

    result = (
        db.table("group_activity")
        .select("*")
        .eq("group_id", group_id)
        .eq("owner_id", user.id)
        .order("created_at", desc=True)
        .execute()
    )

    return result.data


@app.post("/api/groups/{group_id}/activity")
def add_group_activity(
    group_id: str,
    payload: dict,
    auth=Depends(get_db)
):
    db, user = auth

    get_owned_row(db, "groups", group_id, user.id)

    if not payload.get("event_type"):
        raise HTTPException(status_code=400, detail="event_type is required")

    data = {
        "group_id": group_id,
        "owner_id": user.id,
        "event_type": payload["event_type"],
        "description": payload.get("description"),
        "amount": payload.get("amount"),
        "payment_method": payload.get("payment_method"),
        "metadata": payload.get("metadata", {}),
        "bill_id": payload.get("bill_id"),
        "actor_member_id": payload.get("actor_member_id"),
        "target_member_id": payload.get("target_member_id")
    }

    try:
        result = db.table("group_activity").insert(data).execute()
        return result.data[0]

    except Exception as error:
        handle_db_error(error)


# --------------------------------------------------
# GROUP EXTRAS (used by groupsplits.html)
# --------------------------------------------------

def add_months(d: date, months: int) -> date:
    """Add calendar months, clamping to the last day of the target month."""
    index = d.month - 1 + months
    year = d.year + index // 12
    month = index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


@app.get("/api/groups/{group_id}/overview")
def get_group_overview(group_id: str, auth=Depends(get_db)):
    """Everything the Group Splits page needs for one group, in a single call."""
    db, user = auth

    get_owned_row(db, "groups", group_id, user.id)

    def fetch(table, order_desc=False, limit=None):
        query = (
            db.table(table)
            .select("*")
            .eq("group_id", group_id)
            .eq("owner_id", user.id)
        )
        if order_desc:
            query = query.order("created_at", desc=True)
        if limit:
            query = query.limit(limit)
        return query.execute().data or []

    return {
        "members": fetch("group_members"),
        "bills": fetch("group_bills", order_desc=True),
        "shares": fetch("group_bill_shares"),
        "activity": fetch("group_activity", order_desc=True, limit=50),
    }


@app.post("/api/groups/{group_id}/advance")
def advance_group_bills(group_id: str, auth=Depends(get_db)):
    """
    Roll every overdue monthly/yearly bill forward to its next due date and add
    one unpaid period for each non-paying member per cycle that passed.
    The date update is conditional on the old date, so two browser tabs
    calling this at once cannot double-count a cycle.
    """
    db, user = auth

    get_owned_row(db, "groups", group_id, user.id)

    bills = (
        db.table("group_bills")
        .select("*")
        .eq("group_id", group_id)
        .eq("owner_id", user.id)
        .eq("is_active", True)
        .execute()
    ).data or []

    today = date.today()
    advanced = 0

    for bill in bills:
        cycle = bill.get("billing_cycle")
        due_raw = bill.get("next_billing_date")

        if cycle not in ("monthly", "yearly") or not due_raw:
            continue

        due = date.fromisoformat(due_raw)
        step = 1 if cycle == "monthly" else 12
        cycles = 0

        while due < today and cycles < 240:
            due = add_months(due, step)
            cycles += 1

        if not cycles:
            continue

        updated = (
            db.table("group_bills")
            .update({"next_billing_date": due.isoformat()})
            .eq("id", bill["id"])
            .eq("owner_id", user.id)
            .eq("next_billing_date", due_raw)
            .execute()
        )

        if not updated.data:
            continue  # another session already advanced this bill

        shares = (
            db.table("group_bill_shares")
            .select("*")
            .eq("bill_id", bill["id"])
            .eq("owner_id", user.id)
            .execute()
        ).data or []

        for share in shares:
            if share["member_id"] == bill.get("payer_member_id"):
                continue

            db.table("group_bill_shares").update({
                "periods_owed": (share.get("periods_owed") or 0) + cycles,
                "is_settled": False
            }).eq("id", share["id"]).eq("owner_id", user.id).execute()

        advanced += 1

    return {"advanced": advanced}


@app.delete("/api/bills/{bill_id}")
def delete_group_bill(bill_id: str, auth=Depends(get_db)):
    db, user = auth

    get_owned_row(db, "group_bills", bill_id, user.id)

    db.table("group_bill_shares").delete() \
        .eq("bill_id", bill_id) \
        .eq("owner_id", user.id) \
        .execute()

    # Keep the activity history, just detach it from the deleted bill.
    db.table("group_activity").update({"bill_id": None}) \
        .eq("bill_id", bill_id) \
        .eq("owner_id", user.id) \
        .execute()

    db.table("group_bills").delete() \
        .eq("id", bill_id) \
        .eq("owner_id", user.id) \
        .execute()

    return {"message": "Bill deleted"}


# --------------------------------------------------
# ANALYTICS
# --------------------------------------------------

@app.get("/api/analytics")
def get_analytics(auth=Depends(get_db)):
    db, user = auth

    subscriptions = (
        db.table("subscriptions")
        .select("*")
        .eq("owner_id", user.id)
        .eq("is_active", True)
        .execute()
    ).data

    shares = (
        db.table("subscription_shares")
        .select("*")
        .eq("owner_id", user.id)
        .execute()
    ).data

    monthly_total = 0.0
    yearly_total = 0.0
    categories = {}

    for sub in subscriptions:
        cost = float(sub["cost"])
        cycle = sub["billing_cycle"]

        if cycle == "monthly":
            monthly_total += cost
            yearly_total += cost * 12
        elif cycle == "yearly":
            monthly_total += cost / 12
            yearly_total += cost

        category = sub.get("category") or "Other"
        categories[category] = categories.get(category, 0) + cost

    unsettled = sum(
        float(share["share_amount"])
        for share in shares
        if not share["is_settled"]
    )

    return {
        "active_subscriptions": len(subscriptions),
        "monthly_total": round(monthly_total, 2),
        "yearly_total": round(yearly_total, 2),
        "unsettled_subscription_shares": round(unsettled, 2),
        "category_totals": categories
    }


# --------------------------------------------------
# RUN WITH:
# uvicorn main:app --reload
# --------------------------------------------------