"""Structured (JSON) logging -- see STEP 17. Every HTTP request gets one
JSON access-log line carrying request_id/method/path/status/duration and,
where applicable, investigation_id (see main.py's request_id_middleware).
Background/non-request logs (job_queue.py's lease reclaim, etc.) go through
the same JSON formatter but without those fields -- they're not part of an
HTTP request, so there's nothing to correlate.
"""
import json
import logging
import sys


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "investigation_id", "job_id", "source_name"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
