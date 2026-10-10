"""Optional DynamoDB guard so that each recording is summarised and e-mailed once.

Zoom can deliver an event more than once, and Lambda retries a failed
asynchronous invocation. Before doing any work, the worker claims the recording
with a conditional PutItem keyed on its UUID. The claim succeeds when:

* there is no item for the recording yet;
* the item says ``failed`` (an earlier attempt raised an error), so a retry can
  succeed;
* the item says ``processing`` but its lease has run out (an earlier attempt
  timed out).

Otherwise the recording is done or another attempt holds it, and the worker
skips it. Only PutItem is used, so the IAM policy needs nothing else. Items
expire through the table's TTL attribute after ``RETENTION_SECONDS``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

RETENTION_SECONDS = 7 * 24 * 60 * 60

PROCESSING = "processing"
DONE = "done"
FAILED = "failed"

_CLAIM_CONDITION = (
    "attribute_not_exists(recording_uuid)"
    " OR #status = :failed"
    " OR (#status = :processing AND lease_until < :now)"
)


class RecordingLedger:
    """Claims recordings in a DynamoDB table keyed on ``recording_uuid``.

    ``client`` is a boto3 DynamoDB client (low-level API).
    """

    def __init__(
        self, table_name: str, client: Any, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._table = table_name
        self._client = client
        self._clock = clock

    def claim(self, recording_uuid: str, lease_seconds: float) -> bool:
        """Mark the recording as in progress, or return False if it is taken or done."""
        now = int(self._clock())
        lease_until = now + max(1, int(lease_seconds))
        try:
            self._client.put_item(
                TableName=self._table,
                Item=_item(recording_uuid, PROCESSING, now, lease_until),
                ConditionExpression=_CLAIM_CONDITION,
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":failed": {"S": FAILED},
                    ":processing": {"S": PROCESSING},
                    ":now": {"N": str(now)},
                },
            )
        except Exception as exc:
            if _error_code(exc) == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def mark_done(self, recording_uuid: str) -> None:
        self._put(recording_uuid, DONE)

    def mark_failed(self, recording_uuid: str) -> None:
        self._put(recording_uuid, FAILED)

    def _put(self, recording_uuid: str, status: str) -> None:
        now = int(self._clock())
        self._client.put_item(TableName=self._table, Item=_item(recording_uuid, status, now))


def _item(
    recording_uuid: str, status: str, now: int, lease_until: int | None = None
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "recording_uuid": {"S": recording_uuid},
        "status": {"S": status},
        "updated_at": {"N": str(now)},
        "expires_at": {"N": str(now + RETENTION_SECONDS)},
    }
    if lease_until is not None:
        item["lease_until"] = {"N": str(lease_until)}
    return item


def _error_code(exc: BaseException) -> str | None:
    """The AWS error code of a botocore ClientError, without importing botocore."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code")
    return None
