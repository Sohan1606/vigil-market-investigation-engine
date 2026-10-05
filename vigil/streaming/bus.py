"""Event bus abstraction: real Kafka when a broker is configured, deterministic replay otherwise.

The two implementations expose the same produce/consume contract, so the stream job is identical in
both modes. The active mode is always reported to the UI — simulation is never presented as live.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger

log = get_logger("vigil.streaming.bus")


@dataclass
class BusStatus:
    mode: str                    # KAFKA | LOCAL_REPLAY
    detail: str
    topic: str
    healthy: bool


class EventBus:
    def produce(self, events: List[Dict[str, Any]]) -> int: ...
    def consume(self, limit: Optional[int] = None) -> Iterator[Dict[str, Any]]: ...
    def status(self) -> BusStatus: ...


class KafkaBus(EventBus):
    def __init__(self, bootstrap: str, topic: str) -> None:
        from kafka import KafkaConsumer, KafkaProducer  # type: ignore

        self.topic = topic
        self.bootstrap = bootstrap
        self._producer = KafkaProducer(bootstrap_servers=bootstrap,
                                       value_serializer=lambda v: json.dumps(v).encode())
        self._consumer_factory = lambda: KafkaConsumer(
            topic, bootstrap_servers=bootstrap, auto_offset_reset="earliest",
            enable_auto_commit=False, consumer_timeout_ms=5000,
            value_deserializer=lambda v: json.loads(v.decode()))

    def produce(self, events: List[Dict[str, Any]]) -> int:
        for ev in events:
            self._producer.send(self.topic, ev)
        self._producer.flush()
        return len(events)

    def consume(self, limit: Optional[int] = None) -> Iterator[Dict[str, Any]]:
        consumer = self._consumer_factory()
        for i, msg in enumerate(consumer):
            if limit and i >= limit:
                break
            yield msg.value

    def status(self) -> BusStatus:
        return BusStatus("KAFKA", f"broker {self.bootstrap}", self.topic, True)


class LocalReplayBus(EventBus):
    """Append-only, offset-addressed log on local disk — a deterministic Kafka stand-in.

    Chosen deliberately for the default local setup: it needs no broker, replays identically on
    every machine, and is clearly labelled 'HISTORICAL REPLAY' everywhere in the product.
    """

    def __init__(self, path: Path, topic: str) -> None:
        self.topic = topic
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def produce(self, events: List[Dict[str, Any]]) -> int:
        with self.path.open("a") as fh:
            for ev in events:
                fh.write(json.dumps(ev, default=str) + "\n")
        return len(events)

    def truncate(self) -> None:
        if self.path.exists():
            self.path.unlink()

    def consume(self, limit: Optional[int] = None) -> Iterator[Dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open() as fh:
            for i, line in enumerate(fh):
                if limit and i >= limit:
                    break
                if line.strip():
                    yield json.loads(line)

    def __len__(self) -> int:
        if not self.path.exists():
            return 0
        with self.path.open() as fh:
            return sum(1 for _ in fh)

    def status(self) -> BusStatus:
        return BusStatus("LOCAL_REPLAY", f"deterministic file log ({len(self)} events)", self.topic, True)


def get_bus(cfg: Optional[VigilConfig] = None) -> EventBus:
    cfg = cfg or load_config()
    topic = cfg.get("streaming.topic", "vigil.market.events")
    bootstrap = cfg.kafka_bootstrap
    if bootstrap:
        try:
            bus = KafkaBus(bootstrap, topic)
            log.info("Kafka bus active at %s", bootstrap)
            return bus
        except Exception as exc:
            log.warning("Kafka unavailable (%s) — switching to deterministic historical replay",
                        type(exc).__name__)
    return LocalReplayBus(cfg.processed_root / "stream" / "market_events.jsonl", topic)
