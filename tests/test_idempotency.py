import pytest
import uuid
from db.models import get_db_connection
from agent.control_loop import _check_and_claim_update, _store_idempotency_reply, run_agent_turn
from skills.billing import start_bill, add_item_to_bill, finalize_bill
from skills.inventory import get_stock


def test_update_claim_is_atomic():
    """Only one caller can claim an update_id; the second gets the cached reply."""
    key = f"IDEM_{uuid.uuid4().hex}"

    first = _check_and_claim_update(key)
    assert first is None

    _store_idempotency_reply(key, "Final reply text", [])

    second = _check_and_claim_update(key)
    assert second is not None
    assert second[0] == "Final reply text"


def test_unclaimed_race_returns_safe_default():
    """If another worker claimed but hasn't stored a reply yet, we return a safe message."""
    key = f"IDEM_RACE_{uuid.uuid4().hex}"
    first = _check_and_claim_update(key)
    assert first is None  # claimed, no reply stored yet

    raced = _check_and_claim_update(key)
    assert raced is not None
    assert "already" in raced[0].lower()


def test_agent_turn_duplicate_update_returns_cached():
    """
    Full control-loop idempotency: a second run_agent_turn with the same update_id
    must NOT re-execute tools — it returns the cached reply from the first turn.
    """
    from unittest.mock import patch
    from agent.harness import TOOL_DISPATCH

    key = f"IDEM_TURN_{uuid.uuid4().hex}"
    tool_calls = {"count": 0}

    def counting_get_stock(query):
        tool_calls["count"] += 1
        return {"status": "success", "product": {"sku_id": "X", "quantity": 1}}

    with patch.dict(TOOL_DISPATCH, {"get_stock": counting_get_stock}):
        # We patch the LLM call itself to return a plain final message
        class FakeMsg:
            content = "Here is your stock info."
            tool_calls = None

        class FakeResp:
            choices = [type("C", (), {"message": FakeMsg()})()]

        with patch("agent.control_loop._call_llm_with_failover", return_value=FakeResp()):
            reply1, _ = run_agent_turn("how much sugar is left?", chat_id=999001, owner_id="owner_test", update_id=key)
            reply2, _ = run_agent_turn("how much sugar is left?", chat_id=999001, owner_id="owner_test", update_id=key)

    assert reply1 == "Here is your stock info."
    assert reply2 == reply1  # returns cached reply from first turn, not a re-execution
    assert tool_calls["count"] == 0
