"""Source-shape and fail-closed checks; no network or wallet operations."""

from datetime import datetime, timedelta, timezone

import pytest

from app.intake import taskmarket_source as source


TASK_ID = "0x" + "12" * 32
TX_HASH = "0x" + "34" * 32
REQUESTER = "0x" + "56" * 20
REWARD = 220_000_000


def task(**changes):
    record = {
        "id": TASK_ID,
        "requester": REQUESTER,
        "reward": str(REWARD),
        "netReward": "210000000",
        "escrowTxHash": TX_HASH,
        "chainId": source.CHAIN_ID,
        "status": "open",
        "mode": "bounty",
        "taskVisibility": "public",
        "claimedBy": None,
        "claimedAt": None,
        "stakeRequired": False,
        "stakeBps": 0,
        "submissionWindowOpen": True,
        "submissionCount": 0,
        "recentActivityCount": 0,
        "pitchCount": 0,
        "expiryTime": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
        "tags": ["programming"],
        "description": "Implement https://github.com/example/project/issues/7",
    }
    record.update(changes)
    return record


def receipt(*, transfer=True, created=True):
    logs = []
    if transfer:
        logs.append({
            "address": source.BASE_USDC,
            "topics": [source.TRANSFER_TOPIC, source._address_topic(REQUESTER), source._address_topic(source.TASKMARKET_CONTRACT)],
            "data": hex(REWARD),
        })
    if created:
        logs.append({
            "address": source.TASKMARKET_CONTRACT,
            "topics": [source.TASK_CREATED_TOPIC, TASK_ID, source._address_topic(REQUESTER), "0x" + "00" * 32],
            "data": "0x" + f"{REWARD:064x}" + "00" * 192,
        })
    return {"status": "0x1", "transactionHash": TX_HASH, "logs": logs}


def github_reader(url, **kwargs):
    if url.endswith("/issues/7"):
        return {"state": "open", "assignees": []}
    if url.endswith("/repos/example/project"):
        return {"archived": False, "disabled": False}
    raise AssertionError(f"Unexpected URL: {url}")


def rpc_reader(url, *, body):
    if body["method"] == "eth_chainId":
        return {"result": hex(source.CHAIN_ID)}
    if body["method"] == "eth_getTransactionReceipt":
        return {"result": receipt()}
    raise AssertionError(body)


def test_empty_official_feed_reports_zero_candidates():
    def reader(url, **kwargs):
        assert "status=open" in url and "minReward=200000000" in url
        return {"tasks": [], "hasMore": False, "nextCursor": None}

    result = source.scan(reader=reader, rpc_reader=lambda *args, **kwargs: pytest.fail("RPC should not be called"))
    assert result["candidate_count"] == 0
    assert result["candidates"] == []
    assert result["scanned_open_tasks"] == 0


def test_synthetic_verified_shape_is_candidate():
    candidate, reason = source.assess_task(
        task(), now=datetime.now(timezone.utc), reader=github_reader, rpc_reader=rpc_reader
    )
    assert reason == "candidate"
    assert candidate["github_issue_url"] == "https://github.com/example/project/issues/7"
    assert candidate["net_reward_usdc"] == 210.0
    assert candidate["proof_level"] == "base_receipt_task_created_and_usdc_transfer"
    assert candidate["write_actions_taken"] is False


@pytest.mark.parametrize("change", [
    {"escrowTxHash": None},
    {"chainId": 42431},
    {"status": "completed"},
    {"claimedBy": REQUESTER},
    {"stakeRequired": True},
    {"submissionCount": 1},
    {"pitchCount": 1},
    {"netReward": "199999999"},
    {"tags": ["research"]},
    {"description": "No actionable issue"},
])
def test_unfunded_wrong_chain_claimed_or_unactionable_is_rejected(change):
    candidate, reason = source.assess_task(
        task(**change), now=datetime.now(timezone.utc), reader=github_reader, rpc_reader=rpc_reader
    )
    assert candidate is None
    assert reason != "candidate"


@pytest.mark.parametrize("bad_receipt", [
    receipt(transfer=False),
    receipt(created=False),
    {**receipt(), "status": "0x0"},
    {**receipt(), "transactionHash": "0x" + "ab" * 32},
])
def test_receipt_requires_success_transfer_and_task_correlation(bad_receipt):
    assert source.verified_creation(task(), bad_receipt) is False


def test_incomplete_pagination_fails_closed():
    with pytest.raises(source.SourceError, match="pagination"):
        source.task_pages(lambda url: {"tasks": [], "hasMore": True, "nextCursor": None})
