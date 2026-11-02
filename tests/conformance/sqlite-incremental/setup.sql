create table customers (id integer primary key, name text not null, email text);
create table orders (id integer primary key, customer_id integer references customers(id), total real);
create table tmp_load (id integer);
create view big_orders as select o.id, o.total, c.email from orders o join customers c on c.id = o.customer_id where o.total > 100;
create table query_log (query_id text primary key, query_text text, user_name text, started_at text);
insert into query_log values ('q1', 'select * from orders', 'ana', '2026-10-30T09:00:00Z');
insert into query_log values ('q2', 'select * from customers', 'bob', '2026-10-30T10:00:00Z');
insert into query_log values ('q3', 'select count(*) from orders', 'ana', '2026-10-30T10:00:00Z');
