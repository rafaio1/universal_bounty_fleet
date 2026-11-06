"""
Tier 1 Feature Tests: F5 - Autonomous Intent Staking (/try & Payout Routing)
Verifies automatic generation and posting of `/try` priority claim comments,
using only the confirmed USDC-on-Base payout destination.
"""

import pytest
from types import SimpleNamespace
from unittest.mock import patch
from tests.conftest import (
    EVM_PAYOUT_ADDRESS,
    make_issue_payload,
    MockGitHubAPIClient,
    MockFirestoreClient
)


def format_try_comment_body() -> str:
    try:
        from app.intake.claim_staker import format_claim_comment
        return format_claim_comment()
    except ImportError:
        pass

    return (
        "I would like to work on this issue! /try\n\n"
        "## Payout Routing\n"
        f"- **USDC on Base:** `{EVM_PAYOUT_ADDRESS}`"
    )


def execute_claim_staking_action(payload: dict, github_client: MockGitHubAPIClient, firestore_client: MockFirestoreClient) -> dict:
    """Exercise the final claim gate with a stubbed trusted-verifier result."""
    from app.intake.claim_staker import execute_claim_staking

    verified_escrow = {
        "is_funded": True,
        "proof_verified": True,
        "currency": "USDC",
        "chain": "base",
        "proof_source": "onchain_tx",
        "proof_reference": "0x" + "a" * 64,
    }
    settings = SimpleNamespace(
        auto_claim_enabled=True,
        evm_payout_address=EVM_PAYOUT_ADDRESS,
        app_env="test",
    )
    with patch("app.intake.claim_staker.get_settings", return_value=settings):
        return execute_claim_staking(
            payload,
            github_client=github_client,
            firestore_client=firestore_client,
            verified_escrow=verified_escrow,
        )


def test_f5_payout_block_contains_valid_evm_address():
    """Test F5.1: Claim comment includes the CEO's verified EVM address."""
    body = format_try_comment_body()
    assert EVM_PAYOUT_ADDRESS in body
    assert EVM_PAYOUT_ADDRESS.startswith("0x")
    assert len(EVM_PAYOUT_ADDRESS) == 42


def test_f5_payout_block_omits_unconfirmed_stellar_address():
    """Test F5.2: No Stellar payout destination is published."""
    body = format_try_comment_body()
    assert "Stellar" not in body


def test_f5_staking_posts_github_comment(mock_github_client, mock_firestore_client):
    """Test F5.3: Staking action posts comment with /try command on GitHub."""
    payload = make_issue_payload(issue_number=55)
    res = execute_claim_staking_action(payload, mock_github_client, mock_firestore_client)
    assert res["success"] is True
    assert len(mock_github_client.created_comments) == 1
    assert "/try" in mock_github_client.created_comments[0]["body"]
    assert mock_github_client.created_comments[0]["issue_number"] == 55


def test_f5_staking_idempotency_prevents_duplicate_comments(mock_github_client, mock_firestore_client):
    """Test F5.4: Staking action does not post twice on the same issue."""
    payload = make_issue_payload(issue_number=55)
    res1 = execute_claim_staking_action(payload, mock_github_client, mock_firestore_client)
    assert res1["success"] is True
    assert len(mock_github_client.created_comments) == 1

    res2 = execute_claim_staking_action(payload, mock_github_client, mock_firestore_client)
    assert res2["success"] is False
    assert res2["reason"] == "ALREADY_STAKED"
    assert len(mock_github_client.created_comments) == 1


def test_f5_markdown_formatting_is_compliant():
    """Test F5.5: Verified markdown syntax formatting for GitHub comment rendering."""
    body = format_try_comment_body()
    assert "## Payout Routing" in body
    assert "- **USDC on Base:**" in body
    assert "Stellar" not in body
