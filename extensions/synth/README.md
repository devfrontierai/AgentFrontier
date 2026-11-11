# agentfrontier-synth

Synthetic test data from a schema, its constraints and its foreign keys (phase 6 of
[AGENTFRONTIER-PLAN.md](../../../common-docs/AGENTFRONTIER-PLAN.md), pulled forward for Avero's synthetic-fixture
use case). Standard library only.

| Module | What |
|---|---|
| `spec` | `FixtureSpec`: tables, columns (type, nullability, uniqueness, accepted values with weights, ranges, null share, labels), rows, scenarios (rows with set values, or duplicates of other rows) and rules (`after`: keep a date after another; `sum`: total child rows) |
| `generate` | Deterministic generation from the seed. Parents first; keys and unique columns unique; foreign keys always point at generated rows; invented values (emails on `example.test`, phones in the fictional 555-01xx range) |
| `operators` | `redact`, `hash_value`, `tokenize` (keyed, reversible through a `TokenVault`), `format_preserving` (keyed pseudonymisation, not NIST FPE), `generalise`, `mask_rows` |
| `checks` | `validate_dataset` (keys, not-null, unique, accepted values, ranges, foreign keys), `k_anonymity`, `exact_match_share`, `ks_distance`, `tv_distance`, `utility_report` |
| `export` | CSV, JSON Lines, SQL INSERTs in dependency order (dialect-aware literals), and `fixture_manifest` (a provenance manifest) |

```python
from agentfrontier_synth import FixtureSpec, generate, validate_dataset, export
data, report = generate(spec)
assert validate_dataset(spec, data)["ok"]
files = export(spec, data, report["order"], "sql", "postgres")
```
