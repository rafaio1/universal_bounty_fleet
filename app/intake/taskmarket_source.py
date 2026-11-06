"""Read-only discovery of funded Base USDC programming tasks on Taskmarket.

This module never claims work, signs transactions, or posts to GitHub.
Task text is untrusted. A candidate requires both Taskmarket's current task
record and a matching Base transaction receipt.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

TASKS_API = "https://api.taskmarket.dev/api/tasks"
TASKS_WEB = "https://taskmarket.dev/tasks"
BASE_RPC = "https://mainnet.base.org"
GITHUB_API = "https://api.github.com"
CHAIN_ID = 8453
DEFAULT_MIN_TASK_REWARD = 1_000_000  # 1 USDC, six decimals
PORTFOLIO_TARGET = 200_000_000  # aspirational target, not a per-task requirement
MAX_PAGES = 5
PAGE_SIZE = 100
TASKMARKET_CONTRACT = "0xddc6cc3e4d11c1f3527b867c7dad4ed9869c33f7"
BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# TaskCreated(bytes32,address,uint256,bytes4,uint256,bool,uint16), from ITMPCore.sol.
TASK_CREATED_TOPIC = "0xe0dc4072f8420c56e984e9c6eec7bad2e5616825f0d1ca581bd96ff3e8eec948"
ISSUE_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)\b",
    re.IGNORECASE,
)
CODE_TAGS = frozenset(
    {"code", "coding", "development", "developer", "engineering", "programming", "bugfix", "open-source", "html", "frontend", "software"}
)


class SourceError(RuntimeError):
    """A source could not be read or returned an unexpected shape."""


def read_json(url: str, *, body: dict[str, Any] | None = None) -> Any:
    """Read one fixed-source JSON endpoint with bounded time and response size."""
    headers = {"Accept": "application/json", "User-Agent": "universal-bounty-fleet-readonly/1"}
    if url.startswith(GITHUB_API):
        token = os.getenv("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urlopen(request, timeout=12) as response:
            raw = response.read(2_000_001)
    except Exception as exc:
        raise SourceError(f"source request failed: {url.split('?')[0]}: {type(exc).__name__}") from exc
    if len(raw) > 2_000_000:
        raise SourceError("source response exceeded 2 MB")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SourceError("source response was not JSON") from exc


def rpc(method: str, params: list[Any], reader: Callable[..., Any] = read_json) -> Any:
    response = reader(BASE_RPC, body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if not isinstance(response, dict) or response.get("error") or "result" not in response:
        raise SourceError(f"Base RPC failed for {method}")
    return response["result"]


def task_pages(
    reader: Callable[..., Any] = read_json, min_reward_units: int = DEFAULT_MIN_TASK_REWARD
) -> list[dict[str, Any]]:
    """Use only Taskmarket's official open-task feed, with a hard pagination bound."""
    tasks: list[dict[str, Any]] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(MAX_PAGES):
        params = {
            "status": "open",
            "minReward": str(min_reward_units),
            "sort": "reward_desc",
            "limit": str(PAGE_SIZE),
        }
        if cursor:
            params["cursor"] = cursor
        page = reader(f"{TASKS_API}?{urlencode(params)}")
        if not isinstance(page, dict) or not isinstance(page.get("tasks"), list):
            raise SourceError("Taskmarket task list had an invalid schema")
        for task in page["tasks"]:
            if not isinstance(task, dict):
                raise SourceError("Taskmarket task item had an invalid schema")
            task_id = task.get("id")
            if not isinstance(task_id, str) or task_id in seen:
                raise SourceError("Taskmarket task ID was missing or repeated")
            seen.add(task_id)
            tasks.append(task)
        if page.get("hasMore") is False:
            return tasks
        cursor = page.get("nextCursor")
        if page.get("hasMore") is not True or not isinstance(cursor, str) or not cursor:
            raise SourceError("Taskmarket pagination was incomplete")
    raise SourceError("Taskmarket task feed exceeded the bounded scan")


def _amount(value: Any) -> int | None:
    if not isinstance(value, str) or not value.isdecimal():
        return None
    return int(value)


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def _issue_link(description: Any) -> tuple[str, str, str, int] | None:
    if not isinstance(description, str):
        return None
    matches = {(m.group(1), m.group(2), int(m.group(3))) for m in ISSUE_RE.finditer(description)}
    if len(matches) != 1:
        return None
    owner, repo, number = matches.pop()
    return (f"https://github.com/{owner}/{repo}/issues/{number}", owner, repo, number)


def _address_topic(address: str) -> str:
    return "0x" + "0" * 24 + address.lower().removeprefix("0x")


def verified_creation(task: dict[str, Any], receipt: Any) -> bool:
    """Correlate the official task with a successful Base escrow transaction."""
    if not isinstance(receipt, dict) or receipt.get("status") != "0x1":
        return False
    tx_hash = task.get("escrowTxHash")
    if not isinstance(tx_hash, str) or receipt.get("transactionHash", "").lower() != tx_hash.lower():
        return False
    reward = _amount(task.get("reward"))
    task_id = task.get("id")
    requester = task.get("requester")
    if reward is None or not isinstance(task_id, str) or not isinstance(requester, str):
        return False
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        return False
    created = False
    transferred = False
    for log in logs:
        if not isinstance(log, dict) or not isinstance(log.get("topics"), list):
            continue
        address = str(log.get("address", "")).lower()
        topics = [str(value).lower() for value in log["topics"]]
        data = log.get("data")
        if not isinstance(data, str) or not data.startswith("0x"):
            continue
        if (
            address == TASKMARKET_CONTRACT
            and len(topics) >= 3
            and topics[0] == TASK_CREATED_TOPIC
            and topics[1] == task_id.lower()
            and topics[2] == _address_topic(requester)
            and len(data) >= 66
        ):
            try:
                created = int(data[2:66], 16) == reward
            except ValueError:
                pass
        if (
            address == BASE_USDC
            and len(topics) >= 3
            and topics[0] == TRANSFER_TOPIC
            and topics[2] == _address_topic(TASKMARKET_CONTRACT)
        ):
            try:
                transferred = transferred or int(data, 16) >= reward
            except ValueError:
                pass
    return created and transferred


def assess_task(
    task: dict[str, Any],
    *,
    now: datetime,
    min_reward_units: int = DEFAULT_MIN_TASK_REWARD,
    reader: Callable[..., Any] = read_json,
    rpc_reader: Callable[..., Any] = read_json,
) -> tuple[dict[str, Any] | None, str]:
    """Return a candidate only after task, eligibility, and chain evidence agree."""
    task_id = task.get("id")
    tx_hash = task.get("escrowTxHash")
    if not isinstance(task_id, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", task_id):
        return None, "invalid_task_id"
    if not isinstance(tx_hash, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", tx_hash):
        return None, "missing_escrow_tx"
    if task.get("chainId") != CHAIN_ID or task.get("status") != "open":
        return None, "wrong_chain_or_status"
    if task.get("mode") != "bounty" or task.get("taskVisibility") != "public":
        return None, "unsupported_mode_or_visibility"
    if task.get("identity") != [] or task.get("verifiedBy") != [] or task.get("hire") is not None:
        return None, "worker_eligibility_unknown"
    if task.get("claimedBy") is not None or task.get("claimedAt") is not None:
        return None, "assigned"
    if task.get("stakeRequired") is not False or task.get("stakeBps") != 0:
        return None, "requires_stake"
    if task.get("submissionWindowOpen") is not True:
        return None, "submission_closed"
    if any(type(task.get(key)) is not int or task[key] < 0 for key in ("submissionCount", "recentActivityCount", "pitchCount")):
        return None, "competition_unknown"
    reward = _amount(task.get("reward"))
    net_reward = _amount(task.get("netReward"))
    if reward is None or net_reward is None or net_reward < min_reward_units or net_reward > reward:
        return None, "reward_below_threshold_or_unknown"
    expiry = _utc(task.get("expiryTime"))
    if expiry is None or expiry <= now + timedelta(hours=1):
        return None, "deadline_unsuitable"
    tags = task.get("tags")
    if not isinstance(tags, list) or not {str(tag).lower() for tag in tags}.intersection(CODE_TAGS):
        return None, "programming_scope_unproven"
    description = task.get("description")
    if not isinstance(description, str) or not description.strip():
        return None, "brief_missing"
    issue = _issue_link(description)
    if ISSUE_RE.search(description) and issue is None:
        return None, "github_issue_ambiguous"
    issue_url = None
    if issue is not None:
        issue_url, owner, repo, number = issue
        github_issue = reader(f"{GITHUB_API}/repos/{owner}/{repo}/issues/{number}")
        github_repo = reader(f"{GITHUB_API}/repos/{owner}/{repo}")
        if (
            not isinstance(github_issue, dict)
            or github_issue.get("state") != "open"
            or "pull_request" in github_issue
            or github_issue.get("assignees") != []
            or not isinstance(github_repo, dict)
            or github_repo.get("archived") is not False
            or github_repo.get("disabled") is not False
        ):
            return None, "github_issue_unavailable"
    receipt = rpc("eth_getTransactionReceipt", [tx_hash], reader=rpc_reader)
    if not verified_creation(task, receipt):
        return None, "escrow_not_chain_verified"
    return {
        "task_id": task_id,
        "task_api_url": f"{TASKS_API}/{task_id}",
        "task_web_url": f"{TASKS_WEB}/{task_id}",
        "github_issue_url": issue_url,
        "reward_usdc": reward / 1_000_000,
        "net_reward_usdc": net_reward / 1_000_000,
        "expiry_utc": expiry.isoformat(),
        "submission_count": task["submissionCount"],
        "recent_activity_count": task["recentActivityCount"],
        "submission_fee_threshold_reached": task["submissionCount"] >= 50,
        "proof_level": "base_receipt_task_created_and_usdc_transfer",
        "escrow_tx_hash": tx_hash,
        "escrow_tx_url": f"https://basescan.org/tx/{tx_hash}",
        "observed_utc": now.isoformat(),
        "write_actions_taken": False,
    }, "candidate"


def scan(
    reader: Callable[..., Any] = read_json,
    rpc_reader: Callable[..., Any] = read_json,
    min_reward_units: int = DEFAULT_MIN_TASK_REWARD,
) -> dict[str, Any]:
    if type(min_reward_units) is not int or min_reward_units < 1:
        raise SourceError("minimum task reward must be positive USDC base units")
    now = datetime.now(timezone.utc)
    tasks = task_pages(reader, min_reward_units=min_reward_units)
    candidates: list[dict[str, Any]] = []
    rejected: Counter[str] = Counter()
    base_open_escrow_units = 0
    base_open_count = 0
    candidate_net_pool_units = 0
    if tasks and rpc("eth_chainId", [], reader=rpc_reader) != hex(CHAIN_ID):
        raise SourceError("RPC endpoint is not Base mainnet")
    for task in tasks:
        if task.get("chainId") == CHAIN_ID and task.get("status") == "open":
            base_open_count += 1
            base_open_escrow_units += _amount(task.get("reward")) or 0
        candidate, reason = assess_task(
            task, now=now, min_reward_units=min_reward_units, reader=reader, rpc_reader=rpc_reader
        )
        if candidate:
            candidates.append(candidate)
            candidate_net_pool_units += _amount(task["netReward"]) or 0
        else:
            rejected[reason] += 1
    return {
        "status": "ok",
        "source": TASKS_API,
        "chain_id": CHAIN_ID,
        "minimum_task_net_reward_usdc": min_reward_units / 1_000_000,
        "portfolio_target_usdc": PORTFOLIO_TARGET / 1_000_000,
        "observed_utc": now.isoformat(),
        "scanned_open_tasks": len(tasks),
        "base_open_tasks_at_or_above_query_minimum": base_open_count,
        "base_open_escrow_pool_usdc": base_open_escrow_units / 1_000_000,
        "candidate_count": len(candidates),
        "candidate_indicative_net_pool_usdc": candidate_net_pool_units / 1_000_000,
        "reward_sum_semantics": "Escrow pools across distinct contests; not expected or realized worker income. Split awards may pay less to one worker.",
        "candidates": candidates,
        "rejected_counts": dict(sorted(rejected.items())),
        "read_only": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Taskmarket Base USDC candidate scan")
    parser.add_argument("--output", default="taskmarket-candidates.json")
    parser.add_argument("--min-task-usdc", default=os.getenv("TASKMARKET_MIN_TASK_USDC", "1"))
    args = parser.parse_args()
    try:
        value = Decimal(args.min_task_usdc)
        units = value * 1_000_000
        if not value.is_finite() or units != units.to_integral_value() or units < 1:
            raise SourceError("--min-task-usdc must be a positive amount with at most six decimals")
        result = scan(min_reward_units=int(units))
        exit_code = 0
    except (SourceError, InvalidOperation) as exc:
        result = {
            "status": "error",
            "source": TASKS_API,
            "candidate_count": 0,
            "candidates": [],
            "reason": str(exc),
            "read_only": True,
        }
        exit_code = 1
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    with open(args.output, "w", encoding="utf-8") as output:
        output.write(rendered)
    print(rendered, end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
