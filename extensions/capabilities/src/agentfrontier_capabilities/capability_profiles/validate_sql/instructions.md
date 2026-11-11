# validate_sql

Checks, in order: `parse`, `single_statement`, `read_only`, `tables`, `columns`, lint (`select_star`,
`join_condition`), `transpile` (when a target dialect is given), `policy` (when a policy callback is given) and
`explain`/`cost` (when an explain callback is given). Nothing is executed; `ran` is always false.
