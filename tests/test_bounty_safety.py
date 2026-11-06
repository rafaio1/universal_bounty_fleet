"""Safety regressions for payout routing and escrow-triggered claims."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.intake.claim_staker import (
    CONFIRMED_EVM_PAYOUT_ADDRESS,
    ClaimStaker,
    format_claim_comment,
)
from app.config import get_settings
from app.intake.escrow_engine import (
    evaluate_escrow_funding,
    extract_regex_financials,
)
from app import main as gateway


def test_advertised_amount_is_not_escrow_proof():
    result = extract_regex_financials("[Bounty: $500] Fix a bug")
    assert result["amount_usd"] == 500
    assert result["is_funded"] is False


def test_model_claim_is_not_escrow_proof():
    class ModelStub:
        _structured_response = {
            "is_funded": True,
            "escrow_amount_usd": 500,
            "currency": "USDC",
            "confidence": 1.0,
            "reasoning": "The issue says funded.",
        }

    payload = {
        "issue": {"number": 7, "title": "[Bounty: $500]", "body": "Please fix."},
        "repository": {"full_name": "example/repo"},
    }
    result = evaluate_escrow_funding(payload, vertex_client=ModelStub())
    assert result["amount_usd"] == 500
    assert result["is_funded"] is False
    assert result["proof_verified"] is False


def test_claim_comment_uses_only_confirmed_base_wallet_by_default():
    body = format_claim_comment()
    assert CONFIRMED_EVM_PAYOUT_ADDRESS in body
    assert "USDC on Base" in body
    assert "Stellar" not in body
    assert "0xF7b492cCBA473254E392Df444ce2dF4BE0AA29F4" not in body


def test_claim_comment_rejects_wrong_evm_wallet():
    with pytest.raises(ValueError, match="confirmed recipient"):
        format_claim_comment(evm_address="0xF7b492cCBA473254E392Df444ce2dF4BE0AA29F4")


@pytest.mark.parametrize(
    "funding,enabled,expected_funded",
    [
        ({"is_funded": True, "proof_verified": False, "currency": "USDC", "chain": "base", "amount_usd": 500}, True, False),
        ({"is_funded": True, "proof_verified": True, "currency": "USDC", "chain": "ethereum", "amount_usd": 500}, True, True),
        ({"is_funded": True, "proof_verified": True, "currency": "USDC", "chain": "base", "amount_usd": 500}, False, True),
    ],
)
def test_webhook_never_claims_without_all_gates(monkeypatch, funding, enabled, expected_funded):
    import app.intake.sniper_filter as sniper
    import app.intake.escrow_engine as escrow
    import app.intake.claim_staker as staker

    monkeypatch.setattr(sniper, "evaluate_platform_qualification", lambda _: {"qualified": True, "reason": "TEST"})
    monkeypatch.setattr(escrow, "evaluate_escrow_funding", lambda _: funding)
    monkeypatch.setattr(staker, "execute_claim_staking", lambda _: pytest.fail("External claim side effect"))
    monkeypatch.setattr(gateway, "_persist_intake", lambda *_: None)
    monkeypatch.setattr(
        gateway,
        "get_settings",
        lambda: SimpleNamespace(auto_claim_enabled=enabled, github_token="test", app_env="production"),
    )

    payload = {
        "issue": {"number": 7, "title": "Test", "body": "Test", "user": {"login": "test"}, "labels": []},
        "repository": {"full_name": "example/repo"},
    }
    result = asyncio.run(gateway.route_issue_event("opened", payload))
    assert result["is_funded"] is expected_funded
    assert result["staked"] is False


def test_old_evm_environment_cannot_post_claim(monkeypatch):
    monkeypatch.setenv(
        "EVM_PAYOUT_ADDRESS", "0xF7b492cCBA473254E392Df444ce2dF4BE0AA29F4"
    )
    monkeypatch.setenv(
        "STELLAR_PAYOUT_ADDRESS", "GCL6OXAMLD75BMTINA6EMRUDWK5THQUSHMYNLSNBCJAPZJHNYJTUNIBC"
    )
    get_settings.cache_clear()
    github = MagicMock()
    firestore = MagicMock()
    firestore.collection.return_value.document.return_value.get.return_value.exists = False
    payload = {
        "issue": {"number": 7},
        "repository": {"full_name": "example/repo"},
    }
    try:
        with pytest.raises(ValueError, match="confirmed recipient"):
            ClaimStaker().stake_intent(
                payload, github_client=github, firestore_client=firestore
            )
        github.post_issue_comment.assert_not_called()
    finally:
        get_settings.cache_clear()


def test_stellar_environment_is_never_posted(monkeypatch):
    monkeypatch.setenv(
        "STELLAR_PAYOUT_ADDRESS", "GCL6OXAMLD75BMTINA6EMRUDWK5THQUSHMYNLSNBCJAPZJHNYJTUNIBC"
    )
    get_settings.cache_clear()
    try:
        assert "Stellar" not in format_claim_comment()
    finally:
        get_settings.cache_clear()
