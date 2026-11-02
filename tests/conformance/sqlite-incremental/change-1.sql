alter table orders add column status text;
drop table tmp_load;
create table payments (id integer primary key, order_id integer references orders(id), amount real);
insert into query_log values ('q4', 'select * from payments', 'cy', '2026-10-30T10:00:00Z');
insert into query_log values ('q5', 'select 1', 'ana', '2026-10-30T11:30:00Z');
