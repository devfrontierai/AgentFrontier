"""Build (or migrate) the sample shop database used by the examples.

    python examples/sample_db.py            # (re)create examples/data/shop.db
    python examples/sample_db.py --migrate  # apply a schema change, to try the schema drift check

Only the standard library is used. The examples call ``ensure()`` themselves, so running this by hand is optional.
"""

from __future__ import annotations

import random
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "shop.db"

SCHEMA = """
create table customers (
  id          integer primary key,
  email       text not null unique,
  full_name   text not null,
  phone       text,
  country     text not null default 'US',
  created_at  text not null
);
create table products (
  sku         text primary key,
  title       text not null,
  category    text not null,
  price       real not null check (price >= 0)
);
create table orders (
  id          integer primary key autoincrement,
  customer_id integer not null references customers(id),
  ordered_at  text not null,
  status      text not null default 'new' check (status in ('new', 'paid', 'shipped', 'cancelled'))
);
create index idx_orders_customer on orders(customer_id);
create index idx_orders_ordered_at on orders(ordered_at);
create table order_items (
  order_id    integer not null references orders(id),
  line_no     integer not null,
  sku         text not null references products(sku),
  qty         integer not null default 1,
  unit_price  real not null,
  primary key (order_id, line_no)
);
create view customer_revenue as
  select c.id as customer_id, c.full_name, c.country,
         count(distinct o.id) as orders, round(sum(i.qty * i.unit_price), 2) as revenue
  from customers c
  join orders o on o.customer_id = c.id and o.status <> 'cancelled'
  join order_items i on i.order_id = o.id
  group by c.id, c.full_name, c.country;
"""

# A typical release: one additive change and three that break consumers.
MIGRATION = """
alter table customers add column loyalty_points integer not null default 0;
alter table customers drop column phone;
alter table orders add column shipped_at text;
drop index idx_orders_ordered_at;
create table products_new (
  sku         text primary key,
  title       text not null,
  category    text not null,
  price       text not null
);
insert into products_new select sku, title, category, cast(price as text) from products;
drop table products;
alter table products_new rename to products;
create table shipments (
  id          integer primary key,
  order_id    integer not null references orders(id),
  carrier     text not null,
  tracking_no text
);
"""

FIRST = ["Ann", "Bob", "Chen", "Dana", "Eli", "Fatima", "Gus", "Hana", "Ivan", "Jo", "Kofi", "Lena", "Mo", "Nia"]
LAST = ["Smith", "Garcia", "Kim", "Patel", "Novak", "Okafor", "Rossi", "Silva", "Tanaka", "Weber"]
COUNTRIES = ["US", "US", "US", "DE", "IN", "JP", "BR", "GB"]
PRODUCTS = [
    ("KIT-001", "Kettle", "kitchen", 25.0), ("KIT-002", "Teapot", "kitchen", 12.5),
    ("KIT-003", "Chef knife", "kitchen", 59.0), ("HOM-001", "Desk lamp", "home", 34.0),
    ("HOM-002", "Throw blanket", "home", 45.0), ("OUT-001", "Camping stove", "outdoor", 89.0),
    ("OUT-002", "Water bottle", "outdoor", 18.0), ("OUT-003", "Headlamp", "outdoor", 27.5),
]


def build(path: Path = DB_PATH) -> Path:
    """Create the sample database with deterministic data (same rows on every machine)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    rnd = random.Random(42)
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        for cid in range(1, 41):
            first, last = rnd.choice(FIRST), rnd.choice(LAST)
            db.execute("insert into customers values (?, ?, ?, ?, ?, ?)",
                       (cid, f"{first}.{last}{cid}@example.com".lower(), f"{first} {last}",
                        f"+1-555-{rnd.randint(1000, 9999)}", rnd.choice(COUNTRIES),
                        f"2025-{rnd.randint(1, 12):02d}-{rnd.randint(1, 28):02d}"))
        db.executemany("insert into products values (?, ?, ?, ?)", PRODUCTS)
        for oid in range(1, 151):
            db.execute("insert into orders (id, customer_id, ordered_at, status) values (?, ?, ?, ?)",
                       (oid, rnd.randint(1, 40), f"2026-{rnd.randint(1, 9):02d}-{rnd.randint(1, 28):02d}",
                        rnd.choices(["paid", "shipped", "new", "cancelled"], [5, 8, 2, 1])[0]))
            for line in range(1, rnd.randint(1, 4) + 1):
                sku, _, _, price = rnd.choice(PRODUCTS)
                db.execute("insert into order_items values (?, ?, ?, ?, ?)",
                           (oid, line, sku, rnd.randint(1, 3), price))
    return path


def ensure(path: Path = DB_PATH) -> Path:
    """Build the sample database if it does not exist yet."""
    return path if path.exists() else build(path)


def migrate(path: Path = DB_PATH) -> None:
    """Apply MIGRATION to an existing sample database."""
    with sqlite3.connect(ensure(path)) as db:
        db.executescript(MIGRATION)


if __name__ == "__main__":
    if "--migrate" in sys.argv[1:]:
        migrate()
        print(f"Migrated {DB_PATH}")
    else:
        print(f"Created {build()}")
