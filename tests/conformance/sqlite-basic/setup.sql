create table customers (
  id integer primary key,
  email text not null unique,
  name text,
  created_at text default (datetime('now')),
  tier text default 'basic' check (tier in ('basic', 'gold'))
);
create table products (
  sku text primary key,
  title text not null,
  price real not null,
  price_with_tax real generated always as (price * 1.2) virtual
);
create table orders (
  id integer primary key autoincrement,
  customer_id integer not null references customers(id),
  ordered_at text not null,
  status text not null default 'new'
);
create index idx_orders_customer on orders(customer_id);
create table order_items (
  order_id integer not null,
  line_no integer not null,
  sku text not null,
  qty integer not null default 1,
  primary key (order_id, line_no),
  foreign key (order_id) references orders(id),
  foreign key (sku) references products(sku)
);
create unique index ux_items_order_sku on order_items(order_id, sku);
create view customer_orders as
  select c.id, c.name, count(o.id) as orders from customers c left join orders o on o.customer_id = c.id group by c.id, c.name;
insert into customers (id, email, name, created_at, tier) values (1, 'ann@example.com', 'Ann', '2024-01-01', 'gold'), (2, 'bob@example.com', 'Bob', '2024-02-01', 'basic');
insert into products (sku, title, price) values ('A-1', 'Kettle', 25.0), ('B-2', 'Teapot', 12.5);
insert into orders (customer_id, ordered_at) values (1, '2024-03-01'), (2, '2024-03-02');
insert into order_items values (1, 1, 'A-1', 1), (1, 2, 'B-2', 2), (2, 1, 'B-2', 1);
