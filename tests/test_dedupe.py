"""The DynamoDB guard, run against moto's in-process DynamoDB (no AWS account needed)."""

from __future__ import annotations

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from meeting_summariser.dedupe import RETENTION_SECONDS, RecordingLedger

TABLE = "summariser-ledger"
UUID = "test-meeting-uuid=="


class Clock:
    def __init__(self, now: float = 1_000_000) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def dynamodb(monkeypatch):
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "ap-southeast-2",
    }.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-southeast-2")
        client.create_table(
            TableName=TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "recording_uuid", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "recording_uuid", "KeyType": "HASH"}],
        )
        yield client


def stored(client) -> dict:
    return client.get_item(TableName=TABLE, Key={"recording_uuid": {"S": UUID}})["Item"]


def test_first_claim_succeeds_and_records_a_lease(dynamodb):
    ledger = RecordingLedger(TABLE, dynamodb, clock=Clock(1_000_000))
    assert ledger.claim(UUID, lease_seconds=600) is True
    item = stored(dynamodb)
    assert item["status"] == {"S": "processing"}
    assert item["lease_until"] == {"N": "1000600"}
    assert item["expires_at"] == {"N": str(1_000_000 + RETENTION_SECONDS)}


def test_second_claim_during_the_lease_is_refused(dynamodb):
    clock = Clock()
    ledger = RecordingLedger(TABLE, dynamodb, clock=clock)
    assert ledger.claim(UUID, 600)
    clock.now += 599
    assert ledger.claim(UUID, 600) is False


def test_claim_after_the_lease_runs_out_succeeds(dynamodb):
    clock = Clock()
    ledger = RecordingLedger(TABLE, dynamodb, clock=clock)
    assert ledger.claim(UUID, 600)
    clock.now += 601  # the first attempt timed out without releasing its claim
    assert ledger.claim(UUID, 600) is True


def test_a_done_recording_is_never_claimed_again(dynamodb):
    clock = Clock()
    ledger = RecordingLedger(TABLE, dynamodb, clock=clock)
    assert ledger.claim(UUID, 600)
    ledger.mark_done(UUID)
    clock.now += 10 * 24 * 3600
    assert ledger.claim(UUID, 600) is False
    assert stored(dynamodb)["status"] == {"S": "done"}


def test_a_failed_attempt_can_be_retried_at_once(dynamodb):
    ledger = RecordingLedger(TABLE, dynamodb, clock=Clock())
    assert ledger.claim(UUID, 600)
    ledger.mark_failed(UUID)
    assert stored(dynamodb)["status"] == {"S": "failed"}
    assert ledger.claim(UUID, 600) is True


def test_recordings_are_independent(dynamodb):
    ledger = RecordingLedger(TABLE, dynamodb, clock=Clock())
    assert ledger.claim(UUID, 600)
    assert ledger.claim("another-meeting-uuid==", 600)


def test_other_dynamodb_errors_propagate(dynamodb):
    ledger = RecordingLedger("table-that-does-not-exist", dynamodb, clock=Clock())
    with pytest.raises(ClientError) as caught:
        ledger.claim(UUID, 600)
    assert caught.value.response["Error"]["Code"] == "ResourceNotFoundException"
