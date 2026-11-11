# dq_rules

Recommend from declared constraints first (primary and unique keys, NOT NULL, foreign keys, CHECK constraints),
then from known values and profiles with a lower confidence. Translate with `to_sql` (a query that counts the rows
breaking the rule), `to_dbt`, `to_great_expectations` or `to_soda`.
