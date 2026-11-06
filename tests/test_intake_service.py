"""
Acceptance Test 1: Intake Service & Autonomous Intent Staking
Verifies that issue text and model claims cannot trigger external claim comments.
"""

import json
import pytest
from unittest.mock import MagicMock, patch

from tests.conftest import (
    EVM_PAYOUT_ADDRESS,
    make_issue_payload,
    MockGitHubAPIClient,
    MockVertexAIClient,
    MockFirestoreClient
)


def run_intake_pipeline(payload: dict, github_client: MockGitHubAPIClient, vertex_client: MockVertexAIClient, firestore_client: MockFirestoreClient):
    """Exercise the real intake modules with fail-closed funding checks."""
    from app.config import get_settings
    from app.intake.sniper_filter import evaluate_platform_qualification
    from app.intake.escrow_engine import evaluate_escrow_funding
    from app.intake.claim_staker import execute_claim_staking

    qual_res = evaluate_platform_qualification(payload)
    if not qual_res.get("qualified", False):
        return {"qualified": False, "staked": False, "reason": qual_res.get("reason")}

    escrow_res = evaluate_escrow_funding(payload, vertex_client=vertex_client)
    funded = bool(escrow_res.get("is_funded") and escrow_res.get("proof_verified"))
    eligible = (
        str(escrow_res.get("currency", "")).upper() == "USDC"
        and str(escrow_res.get("chain", "")).lower() == "base"
    )
    if not funded or not eligible or not get_settings().auto_claim_enabled:
        return {"qualified": False, "staked": False, "reason": "Escrow unverified or auto-claim disabled"}

    staked_res = execute_claim_staking(
        payload, github_client=github_client, firestore_client=firestore_client
    )
    return {
        "qualified": True,
        "staked": staked_res.get("success", False),
        "comment_id": staked_res.get("comment_id"),
        "amount": escrow_res.get("amount_usd", 0.0),
    }


def test_acceptance_1_issue_text_cannot_trigger_claim(
    mock_funded_issue_payload,
    mock_github_client,
    mock_vertex_client,
    mock_firestore_client
):
    """A mock issue statement is not authoritative escrow proof."""
    result = run_intake_pipeline(
        mock_funded_issue_payload,
        github_client=mock_github_client,
        vertex_client=mock_vertex_client,
        firestore_client=mock_firestore_client,
    )

    assert result["qualified"] is False
    assert result["staked"] is False
    assert mock_github_client.created_comments == []


def test_acceptance_1_unfunded_issue_is_rejected_without_comment(
    mock_unfunded_issue_payload,
    mock_github_client,
    mock_vertex_client,
    mock_firestore_client
):
    """
    Acceptance Test 1 Negative:
    Unfunded issue must not trigger `/try` comment on GitHub.
    """
    mock_vertex_client.default_response = json.dumps({
        "is_funded": False,
        "escrow_amount_usd": 0.0,
        "confidence": 0.95,
        "reasoning": "No escrow confirmation or grant pool allocation found."
    })

    result = run_intake_pipeline(
        mock_unfunded_issue_payload,
        github_client=mock_github_client,
        vertex_client=mock_vertex_client,
        firestore_client=mock_firestore_client
    )

    assert result["qualified"] is False
    assert result["staked"] is False
    assert len(mock_github_client.created_comments) == 0


@pytest.mark.parametrize("banned_platform", ["algora.io", "polar.sh", "twentyhq/twenty", "opire.dev"])
def test_acceptance_1_banned_platforms_rejected(
    banned_platform,
    mock_github_client,
    mock_vertex_client,
    mock_firestore_client
):
    """
    Acceptance Test 1 Constraint:
    Rejects issues originating from or mentioning banned platforms (Algora, Polar, twentyhq/twenty, Opire).
    """
    payload = make_issue_payload(
        title=f"Bounty on {banned_platform}",
        body=f"Check reward details on {banned_platform} for this issue."
    )
    result = run_intake_pipeline(
        payload,
        github_client=mock_github_client,
        vertex_client=mock_vertex_client,
        firestore_client=mock_firestore_client
    )

    assert result["qualified"] is False
    assert result["staked"] is False
    assert len(mock_github_client.created_comments) == 0


def test_acceptance_1_competitor_claim_blocks_staking(
    mock_competitor_claimed_payload,
    mock_github_client,
    mock_vertex_client,
    mock_firestore_client
):
    """
    Acceptance Test 1 Anti-Sniping:
    If a competitor has already claimed or staked the issue, abort immediately.
    """
    result = run_intake_pipeline(
        mock_competitor_claimed_payload,
        github_client=mock_github_client,
        vertex_client=mock_vertex_client,
        firestore_client=mock_firestore_client
    )

    assert result["qualified"] is False
    assert result["staked"] is False
    assert len(mock_github_client.created_comments) == 0
