# nl_to_sql

1. Assemble context: the tables whose names and columns match the question, their foreign-key neighbours, the join
   conditions between them and the business terms the question names. Tables the policy denies are left out.
2. Ask the model for `{"sql": ..., "assumptions": [...]}`: one read-only query in the source's dialect.
3. Validate it (`validate_sql`). When validation fails, ask once more with the errors.
4. Run it only through the caller's guarded `execute` callback, and only when validation passed.
5. Return the SQL, the validation report, the assumptions and a provenance manifest.
