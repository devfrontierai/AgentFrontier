# dbt_model

Validate the SQL first (`dejinja` turns dbt SQL back into plain SQL). Rewrite table references with `to_dbt_refs`,
then write `<folder>/<name>.sql` and `<folder>/<name>.yml` with `dbt_model_files`, tests from `dq_rules`.
