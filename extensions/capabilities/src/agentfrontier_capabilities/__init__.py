"""agentfrontier-capabilities: generative and validating capabilities over the AgentFrontier SDK, independent of
any agent framework. The LLM is optional and sits behind a provider protocol; deterministic parts come first.

* :mod:`.llm`: the provider protocol and clients (Anthropic, OpenAI, Azure OpenAI, Gemini, OpenAI-compatible),
  structured output, budget;
* :mod:`.sqlcheck`: static SQL validation against the catalog, policy and EXPLAIN;
* :mod:`.nl2sql`: grounded context assembly, generation with one repair, guarded execution, evaluation;
* :mod:`.dq`: data-quality rules and their SQL, dbt, Great Expectations and Soda forms;
* :mod:`.codegen`: dbt model files and static checks;
* :mod:`.profiles`: capability profiles (manifests, schemas, instructions).
"""

from .profiles import CapabilityProfile, builtin_profiles, check_schema, load_profile

__version__ = "0.1.0"

__all__ = ["CapabilityProfile", "builtin_profiles", "check_schema", "load_profile", "__version__"]
