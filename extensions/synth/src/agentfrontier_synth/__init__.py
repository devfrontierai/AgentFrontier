"""agentfrontier-synth: synthetic test data from a schema, its constraints and foreign keys; masking and
tokenization operators; validation, privacy and utility reports; fixture files with a manifest.

* :mod:`.spec`: what to generate (tables, columns, rows, scenarios, rules);
* :mod:`.generate`: deterministic generation, parents first, keys unique, foreign keys valid;
* :mod:`.operators`: redact, hash, tokenize (with a vault interface), format-preserving pseudonymisation, generalise;
* :mod:`.checks`: dataset validation, k-anonymity, exact-match leakage, distribution distances;
* :mod:`.export`: CSV, JSON Lines, SQL INSERTs and the fixture manifest.
"""

from .checks import exact_match_share, k_anonymity, utility_report, validate_dataset
from .export import export, fixture_manifest
from .generate import GenerationError, generate
from .spec import ColumnSpec, FixtureSpec, Scenario, TableSpec

__version__ = "0.1.0"

__all__ = ["ColumnSpec", "FixtureSpec", "GenerationError", "Scenario", "TableSpec", "exact_match_share", "export",
           "fixture_manifest", "generate", "k_anonymity", "utility_report", "validate_dataset", "__version__"]
