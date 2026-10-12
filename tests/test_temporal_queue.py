import json
from dataclasses import dataclass

import httpx
import pytest
from temporalio.exceptions import WorkflowAlreadyStartedError

from commuter.temporal_queue import (
    CloudflareQueueClient,
    PulledQueueMessage,
    dispatch_queue_messages,
    workflow_id_for_event,
)

VALID_EVENT = {
    "aspect_type": "update",
    "event_time": 1791769242,
    "object_id": 123456789,
    "object_type": "activity",
    "owner_id": 12345678,
    "subscription_id": 123,
}


@dataclass
class FakeQueue:
    settlements: list[tuple[list[str], list[dict]]]

    async def settle(self, acknowledgements, retries):
        self.settlements.append((acknowledgements, retries))


class FakeTemporalClient:
    def __init__(self, error=None):
        self.error = error
        self.started = []

    async def start_workflow(self, workflow, event, **options):
        self.started.append((workflow, event, options))
        if self.error:
            raise self.error


@pytest.mark.asyncio
async def test_dispatch_starts_stable_workflow_then_acknowledges_message():
    event = dict(VALID_EVENT)
    message = PulledQueueMessage(json.dumps(event), "message-1", "lease-1")
    queue = FakeQueue([])
    client = FakeTemporalClient()

    await dispatch_queue_messages([message], queue, client)

    assert client.started[0][1] == event
    assert client.started[0][2]["id"] == workflow_id_for_event(event)
    assert client.started[0][2]["task_queue"] == "commuter-strava"
    assert queue.settlements == [(["lease-1"], [])]


@pytest.mark.asyncio
async def test_dispatch_retries_message_when_temporal_start_fails():
    message = PulledQueueMessage(json.dumps(VALID_EVENT), "message-1", "lease-1")
    queue = FakeQueue([])
    client = FakeTemporalClient(RuntimeError("Temporal unavailable"))

    await dispatch_queue_messages([message], queue, client, retry_delay_seconds=17)

    assert queue.settlements == [([], [{"lease_id": "lease-1", "delay_seconds": 17}])]


@pytest.mark.asyncio
async def test_dispatch_acknowledges_duplicate_workflow_start():
    message = PulledQueueMessage(json.dumps(VALID_EVENT), "message-1", "lease-1")
    queue = FakeQueue([])
    client = FakeTemporalClient(
        WorkflowAlreadyStartedError("workflow-id", "processStravaEvent")
    )

    await dispatch_queue_messages([message], queue, client)

    assert queue.settlements == [(["lease-1"], [])]


def test_workflow_id_is_derived_from_strava_event_identity():
    assert workflow_id_for_event(VALID_EVENT) == (
        "strava:123:activity:123456789:update:1791769242"
    )


@pytest.mark.asyncio
async def test_http_pull_uses_queue_id_and_parses_text_message():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "messages": [
                        {
                            "body": json.dumps(VALID_EVENT),
                            "id": "id-1",
                            "lease_id": "lease-1",
                        }
                    ]
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        queue = CloudflareQueueClient(
            http,
            account_id="account-1",
            queue_id="queue-1",
            api_token="token-1",
            visibility_timeout_ms=45000,
        )
        messages = await queue.pull()

    assert (
        requests[0].url.path
        == "/client/v4/accounts/account-1/queues/queue-1/messages/pull"
    )
    assert requests[0].headers["authorization"] == "Bearer token-1"
    assert json.loads(requests[0].content) == {
        "visibility_timeout_ms": 45000,
        "batch_size": 1,
    }
    assert messages == [PulledQueueMessage(json.dumps(VALID_EVENT), "id-1", "lease-1")]


@pytest.mark.asyncio
async def test_http_ack_sends_acknowledgement_and_delayed_retry():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"success": True, "result": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        queue = CloudflareQueueClient(
            http, account_id="account-1", queue_id="queue-1", api_token="token-1"
        )
        await queue.settle(["lease-a"], [{"lease_id": "lease-b", "delay_seconds": 13}])

    assert requests[0].url.path.endswith("/messages/ack")
    assert json.loads(requests[0].content) == {
        "acks": [{"lease_id": "lease-a"}],
        "retries": [{"lease_id": "lease-b", "delay_seconds": 13}],
    }
