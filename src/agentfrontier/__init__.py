"""AgentFrontier: a configuration-driven data source SDK. One engine per protocol, one JSON file per source.

Python edition of the shared AgentFrontier spec; it produces the same canonical records as the Java edition.
"""

from .definitions import DefinitionRegistry, Facts, Level, SourceDefinition, VersionRange
from .engine import EngineSession
from .errors import AgentFrontierError
from .model import (AuthConfig, Category, Column, ConnectionConfig, Page, QueryRequest, QueryResult, Record, Scope,
                    SessionStats, Snapshot, SourceInfo, TestResult)
from .pipeline import Pipeline, PipelineConfig, RunReport
from .sdk import AgentFrontier
from .secrets import SecretResolver, Secrets
from .sinks import MemorySink, RunContext, Sink, SinkProvider

SourceSession = EngineSession

__version__ = "0.1.0"

__all__ = [
    "AgentFrontier", "AgentFrontierError", "AuthConfig", "Category", "Column", "ConnectionConfig",
    "DefinitionRegistry", "EngineSession", "Facts", "Level", "MemorySink", "Page", "Pipeline", "PipelineConfig",
    "QueryRequest", "QueryResult", "Record", "RunContext", "RunReport", "Scope", "SecretResolver", "Secrets",
    "SessionStats", "Sink", "SinkProvider", "Snapshot", "SourceDefinition", "SourceInfo", "SourceSession",
    "TestResult", "VersionRange", "__version__",
]
