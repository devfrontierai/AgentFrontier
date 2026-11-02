drop view big_orders;
create view paid_orders as select o.id, p.amount from orders o join payments p on p.order_id = o.id;
