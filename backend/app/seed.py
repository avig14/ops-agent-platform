"""Idempotent synthetic seed data. Run: python -m app.seed

All data is synthetic (the free Gemini tier may use inputs for training; no real PII is ever stored).
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

from . import db
from .auth import hash_password

DEMO_PASSWORD = "Demo@2026"

USERS = [
    # id, email, name, role, region
    ("u-req-west", "priya.requester@demo.test", "Priya Nair (Store Ops, Pune)", "requester", "West"),
    ("u-req-south", "karthik.requester@demo.test", "Karthik Rao (Store Ops, Bengaluru)", "requester", "South"),
    ("u-mgr-west", "rahul.manager@demo.test", "Rahul Mehta (Ops Manager, West)", "manager", "West"),
    ("u-mgr-south", "sneha.manager@demo.test", "Sneha Iyer (Ops Manager, South)", "manager", "South"),
    ("u-rh-west", "anita.regional@demo.test", "Anita Desai (Regional Head, West)", "regional_head", "West"),
    ("u-rh-south", "meera.regional@demo.test", "Meera Krishnan (Regional Head, South)", "regional_head", "South"),
    ("u-fin", "vikram.finance@demo.test", "Vikram Shah (Finance Director)", "finance_director", None),
    ("u-admin", "admin@demo.test", "Platform Admin", "admin", None),
]
SERVICE_USER = ("u-ai-agent", "ai-agent@system.local", "AI Agent (service identity)", "service", None)

STORES = [
    ("PUN-01", "Pune Phoenix Store", "Pune", "West"),
    ("MUM-01", "Mumbai Andheri Store", "Mumbai", "West"),
    ("BLR-01", "Bengaluru Indiranagar Store", "Bengaluru", "South"),
    ("DEL-01", "Delhi Saket Store", "Delhi", "North"),
]
SUPPLIERS = [
    ("SUP-ACME", "Acme Display Systems", "orders@acme-display.test", 10, 0.88),
    ("SUP-BRIGHT", "BrightFab Displays", "sales@brightfab.test", 4, 0.92),
    ("SUP-LOCAL", "Pune Local Fixtures", "hello@punefixtures.test", 2, 0.80),
]
ITEMS = [
    ("ITM-DISP", "Display unit (standard)", 4000.00, "SUP-ACME"),
    ("ITM-DISP-X", "Display unit (express)", 4600.00, "SUP-BRIGHT"),
    ("ITM-SHELF", "Gondola shelf", 1800.00, "SUP-LOCAL"),
    ("ITM-LED", "LED signage panel", 9500.00, "SUP-BRIGHT"),
]
# item, location, on_hand
INVENTORY = [
    ("ITM-DISP", "Warehouse-West", 120),   # the sample email: "only 120 units are currently available"
    ("ITM-DISP", "Store-MUM-01", 30),      # transfer candidate
    ("ITM-DISP", "Warehouse-North", 60),
    ("ITM-DISP-X", "Vendor-BRIGHT-stock", 400),
    ("ITM-SHELF", "Warehouse-West", 80),
    ("ITM-LED", "Warehouse-West", 12),
]


async def seed() -> None:
    today = date.today()
    pw = hash_password(DEMO_PASSWORD)
    async with db.pool.connection() as c:
        async with c.transaction():
            for uid, email, name, role, region in USERS:
                await c.execute("INSERT INTO users(id,email,name,role,region,password_hash) VALUES (%s,%s,%s,%s,%s,%s) "
                                "ON CONFLICT (id) DO NOTHING", (uid, email, name, role, region, pw))
            sid, semail, sname, srole, sregion = SERVICE_USER  # unusable password hash: service identity cannot log in
            await c.execute("INSERT INTO users(id,email,name,role,region,password_hash) VALUES (%s,%s,%s,%s,%s,'!disabled') "
                            "ON CONFLICT (id) DO NOTHING", (sid, semail, sname, srole, sregion))
            for sid_, name, city, region in STORES:
                opens = today + timedelta(days=7) if sid_ == "PUN-01" else None
                await c.execute("INSERT INTO stores(id,name,city,region,opens_on) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                                (sid_, name, city, region, opens))
            for row in SUPPLIERS:
                await c.execute("INSERT INTO suppliers(id,name,email,lead_time_days,reliability) VALUES (%s,%s,%s,%s,%s) "
                                "ON CONFLICT (id) DO NOTHING", row)
            for row in ITEMS:
                await c.execute("INSERT INTO items(id,name,unit_cost,supplier_id) VALUES (%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING", row)
            have = (await (await c.execute("SELECT count(*) n FROM inventory_snapshots")).fetchone())["n"]
            if have == 0:
                for item, loc, qty in INVENTORY:
                    await c.execute("INSERT INTO inventory_snapshots(item_id,location,on_hand) VALUES (%s,%s,%s)", (item, loc, qty))
            await c.execute("INSERT INTO purchase_orders(id,item_id,supplier_id,qty,destination_store_id,promised_date,status) "
                            "VALUES ('PO-7781','ITM-DISP','SUP-ACME',500,'PUN-01',%s,'open') ON CONFLICT (id) DO NOTHING",
                            (today + timedelta(days=3),))


async def main() -> None:
    await db.open_pools()
    try:
        await seed()
        print("seed ok")
    finally:
        await db.close_pools()


if __name__ == "__main__":
    asyncio.run(main())
