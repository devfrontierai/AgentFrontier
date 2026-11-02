"""Sinks that deliver a run's records in another system's format (spec docs/translators.md): they keep the run's
records, translate them when the run ends, and write the result to a file (``path``) or send it to the system
(``url``). Plus ``kafka``, which streams every record or event as it comes (``pip install "agentfrontier[kafka]"``).

| Type | ``url`` | ``path`` |
|---|---|---|
| ``openlineage`` | POST each event to ``<url>/api/v1/lineage`` (Marquez) or ``<url>`` when it ends in ``/lineage`` | JSON Lines |
| ``openmetadata`` | PUT each request to ``<url>`` (``.../api``), lineage after looking tables up by FQN | JSON Lines |
| ``datahub`` | POST each proposal to ``<url>/aspects?action=ingestProposal`` | JSON Lines |
| ``opa`` | PUT the document to ``<url>/v1/data/<dataPath>`` | JSON |
| ``otlp`` | POST the logs to ``<url>/v1/logs`` | JSON |
| ``focus`` | | CSV |
| ``ocsf`` | POST the events as one JSON array to ``<url>`` | JSON Lines |
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .. import json_util as J
from ..errors import SINK_FAILED, AgentFrontierError
from ..model import Record
from ..translators import TranslationContext, translate
from . import RunContext, Sink


class _Http:
    def __init__(self, headers: dict[str, str], timeout: int = 60):
        self.headers = headers
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def send(self, method: str, url: str, body: Any = None, ok404: bool = False) -> Any:
        data = None if body is None else (body if isinstance(body, bytes) else J.write(body).encode("utf-8"))
        h = dict(self.headers, Accept="application/json")
        if data is not None:
            h["Content-Type"] = "application/json"
        for attempt in range(1, 4):
            req = urllib.request.Request(url, data=data, method=method, headers=h)
            try:
                with self.opener.open(req, timeout=self.timeout) as resp:
                    text = resp.read().decode("utf-8", errors="replace")
                    return J.loads_first(text) if text.strip() else None
            except urllib.error.HTTPError as e:
                if e.code == 404 and ok404:
                    return None
                if e.code in (429, 502, 503, 504) and attempt < 3:
                    time.sleep(0.5 * attempt)
                    continue
                detail = ""
                try:
                    detail = " ".join(e.read()[:400].decode("utf-8", errors="replace").split())
                except Exception:  # noqa: BLE001
                    pass
                raise AgentFrontierError(SINK_FAILED, method + " " + urllib.parse.urlsplit(url).path + " returned HTTP "
                                         + str(e.code) + (": " + detail if detail else "")) from None
            except (urllib.error.URLError, OSError) as e:
                if attempt < 3:
                    time.sleep(0.5 * attempt)
                    continue
                raise AgentFrontierError(SINK_FAILED, "cannot reach " + url + ": " + str(e), e) from e
        return None


def _headers(c: dict, sdk: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    h = c.get("headers")
    for k, v in (h.items() if isinstance(h, dict) else []):
        out[k] = sdk.secrets.resolve(J.jstr(v))
    token = J.text(c, "token")
    if token:
        out.setdefault("Authorization", "Bearer " + sdk.secrets.resolve(token))
    return out


class TranslatingSink(Sink):
    translator = ""

    def __init__(self, c: dict, sdk: Any):
        self.config = c
        self.url = (J.text(c, "url") or "").rstrip("/") or None
        self.path = J.text(c, "path")
        if self.url is None and self.path is None:
            raise AgentFrontierError.invalid_config(self.translator + " sink needs 'url' or 'path'")
        self.options = dict(c.get("options") or {})
        self.http = _Http(_headers(c, sdk)) if self.url else None
        self._records: list[Record] = []
        self._lock = threading.Lock()
        self._ctx: RunContext | None = None
        self.sent = 0

    def begin(self, context: RunContext) -> None:
        self._ctx = context
        with self._lock:
            self._records.clear()

    def write(self, records: list[Record]) -> None:
        with self._lock:
            self._records.extend(records)

    def context(self, report: Any) -> TranslationContext:
        ctx = self._ctx
        tenant = next((J.text(getattr(r, "envelope", None), "tenantId") for r in self._records
                       if getattr(r, "envelope", None)), None) or "default"
        return TranslationContext(source=J.jstr(ctx.source if ctx else ""), definition=ctx.definition if ctx else None,
                                  levels=list(ctx.levels) if ctx else [], run_id=ctx.run_id if ctx else "",
                                  tenant=tenant, started_at=getattr(report, "started_at", None),
                                  finished_at=getattr(report, "finished_at", None), options=self.options)

    def end(self, report: Any) -> None:
        with self._lock:
            out = translate(self.translator, self._records, self.context(report))
        if self.url is not None:
            self.deliver(out)
        else:
            self.save(out)

    def save(self, out: Any) -> None:
        p = Path(self.path)  # type: ignore[arg-type]
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(out, list):
            p.write_text("".join(J.write(x) + "\n" for x in out), encoding="utf-8", newline="\n")
        elif isinstance(out, str):
            p.write_text(out, encoding="utf-8", newline="")
        else:
            p.write_text(J.pretty(out), encoding="utf-8")

    def deliver(self, out: Any) -> None:
        raise NotImplementedError


class OpenLineageSink(TranslatingSink):
    translator = "openlineage"

    def deliver(self, out: Any) -> None:
        endpoint = self.url if self.url.endswith("/lineage") else self.url + "/api/v1/lineage"  # type: ignore[union-attr]
        for e in out:
            self.http.send("POST", endpoint, e)  # type: ignore[union-attr]
            self.sent += 1


class OpenMetadataSink(TranslatingSink):
    translator = "openmetadata"

    def deliver(self, out: Any) -> None:
        ids: dict[str, str] = {}

        def table_id(fqn: str) -> str:
            if fqn not in ids:
                t = self.http.send("GET", self.url + "/v1/tables/name/" + urllib.parse.quote(fqn, safe=""))  # type: ignore[union-attr,operator]
                if not isinstance(t, dict) or not t.get("id"):
                    raise AgentFrontierError(SINK_FAILED, "OpenMetadata has no table " + fqn)
                ids[fqn] = J.jstr(t["id"])
            return ids[fqn]

        for r in out:
            body = r["body"]
            if r["path"] == "/v1/lineage":
                edge = dict(body["edge"])
                for side in ("fromEntity", "toEntity"):
                    edge[side] = {"type": "table", "id": table_id(edge[side]["fqn"])}
                body = {"edge": edge}
            res = self.http.send(r["method"], self.url + r["path"], body)  # type: ignore[union-attr,operator]
            if r["path"] == "/v1/tables" and isinstance(res, dict) and res.get("id"):
                ids[J.jstr(res.get("fullyQualifiedName"))] = J.jstr(res["id"])
            self.sent += 1


class DataHubSink(TranslatingSink):
    translator = "datahub"

    def deliver(self, out: Any) -> None:
        for m in out:
            proposal = dict(m, aspect={"value": json.dumps(m["aspect"], separators=(",", ":")),
                                       "contentType": "application/json"})
            self.http.send("POST", self.url + "/aspects?action=ingestProposal", {"proposal": proposal})  # type: ignore[union-attr,operator]
            self.sent += 1


class OpaSink(TranslatingSink):
    translator = "opa"

    def deliver(self, out: Any) -> None:
        path = J.text(self.config, "dataPath") or "agentfrontier/" + J.jstr(self._ctx.source if self._ctx else "")
        self.http.send("PUT", self.url + "/v1/data/" + path.strip("/"), out)  # type: ignore[union-attr,operator]
        self.sent += 1


class OtlpSink(TranslatingSink):
    translator = "otel"

    def deliver(self, out: Any) -> None:
        if out["resourceLogs"][0]["scopeLogs"][0]["logRecords"]:
            endpoint = self.url if self.url.endswith("/v1/logs") else self.url + "/v1/logs"  # type: ignore[union-attr]
            self.http.send("POST", endpoint, out)  # type: ignore[union-attr]
            self.sent += 1


class FocusSink(TranslatingSink):
    translator = "focus"

    def __init__(self, c: dict, sdk: Any):
        if not J.text(c, "path"):
            raise AgentFrontierError.invalid_config("focus sink needs 'path' (a CSV file)")
        super().__init__(dict(c, url=None), sdk)


class OcsfSink(TranslatingSink):
    translator = "ocsf"

    def deliver(self, out: Any) -> None:
        if out:
            self.http.send("POST", self.url, out)  # type: ignore[union-attr,arg-type]
            self.sent += len(out)


class KafkaSink(Sink):
    """Every record or event to a Kafka topic as it comes: key the event's ``nativeObjectId`` (else the entity and
    name), value its JSON. ``topic`` names the topic; with ``topicPerCategory`` the raw-stream category is appended
    (``<topic>.<category>``)."""

    def __init__(self, c: dict, sdk: Any):
        try:
            from kafka import KafkaProducer
        except ImportError:
            raise AgentFrontierError.invalid_config("the kafka sink needs kafka-python: pip install "
                                                    "\"agentfrontier[kafka]\"") from None
        servers = J.strings(c.get("bootstrapServers") or c.get("bootstrap_servers"))
        if not servers:
            raise AgentFrontierError.invalid_config("kafka sink needs 'bootstrapServers'")
        self.topic = J.text(c, "topic") or "agentfrontier"
        self.per_category = J.boolean(c, "topicPerCategory", False)
        self.model = sdk.entity_model
        extra = dict(c.get("producer") or {})
        try:
            self.producer = KafkaProducer(bootstrap_servers=servers, acks="all", **extra)
        except Exception as e:  # noqa: BLE001
            raise AgentFrontierError(SINK_FAILED, "cannot connect to Kafka: " + str(e), e) from e
        self.sent = 0

    def write(self, records: list[Record]) -> None:
        for r in records:
            env = getattr(r, "envelope", None)
            key = J.jstr(env.get("nativeObjectId")) if env else r.entity + "/" + J.jstr(r.values.get("name"))
            topic = self.topic
            if self.per_category:
                topic += "." + J.jstr(env.get("category") if env else self.model.category_of(r.entity))
            self.producer.send(topic, key=key.encode("utf-8"), value=J.write(r.to_json()).encode("utf-8"))
            self.sent += 1

    def end(self, report: Any) -> None:
        try:
            self.producer.flush(timeout=60)
        except Exception as e:  # noqa: BLE001
            raise AgentFrontierError(SINK_FAILED, "Kafka did not take every message: " + str(e), e) from e

    def close(self) -> None:
        try:
            self.producer.close(timeout=10)
        except Exception:  # noqa: BLE001
            pass


SINKS = {"openlineage": OpenLineageSink, "openmetadata": OpenMetadataSink, "datahub": DataHubSink, "opa": OpaSink,
         "otlp": OtlpSink, "focus": FocusSink, "ocsf": OcsfSink, "kafka": KafkaSink}

__all__ = ["TranslatingSink", "OpenLineageSink", "OpenMetadataSink", "DataHubSink", "OpaSink", "OtlpSink", "FocusSink",
           "OcsfSink", "KafkaSink", "SINKS"]
