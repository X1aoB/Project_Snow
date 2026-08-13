from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import random
from uuid import uuid4


CHARACTERS = [f"synthetic-character-{index:02d}" for index in range(1, 23)]
TOPICS = ["character_quality", "model_error", "retrieval", "mobile_ui", "other"]


def events(count: int = 500):
    now = datetime.now(UTC)
    for index in range(count):
        feedback = index % 5 == 0
        yield {
            "event_id": str(uuid4()),
            "event_type": "feedback_submitted" if feedback else "chat_completed",
            "occurred_at": (now - timedelta(seconds=count - index)).isoformat(),
            "character_id": random.choice(CHARACTERS),
            "provider": random.choice(["openai", "deepseek", "dashscope", "zhipu", "moonshot"]),
            "model": "synthetic-model",
            "request_stage": random.choice(["generation", "retrieval", "feedback", "complete"]),
            "error_code": random.choice([None, None, None, "provider_timeout", "generation_busy"]),
            "degraded_services": random.choice([[], [], ["neo4j"], ["qdrant", "embedding"]]),
            "latency_ms": random.randint(120, 12000),
            "feedback_topic": random.choice(TOPICS) if feedback else None,
        }


if __name__ == "__main__":
    for event in events():
        print(json.dumps(event, ensure_ascii=False))
