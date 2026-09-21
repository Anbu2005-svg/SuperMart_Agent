import json
import time
import logging
from typing import Dict, Any, List, Optional, Tuple
from db.models import get_db_connection, immediate_transaction
from skills.preferences import get_all_preferences
from agent.harness import get_llm_client, failover_to_next_key, reset_key_rotation, SYSTEM_PROMPT, TOOLS_SCHEMA, TOOL_DISPATCH, MODEL_NAME

logger = logging.getLogger(__name__)

# Global in-memory conversation state keyed by chat_id
CONVERSATION_HISTORY: Dict[int, List[Dict[str, Any]]] = {}

# Cached idempotency results for this process: {update_id: (reply_text, files)}
_IDEMPOTENCY_CACHE: Dict[str, Tuple[str, List[str]]] = {}


def clear_conversation(chat_id: int):
    """Clear in-memory chat session history (used by /new command). Preferences persist in DB!"""
    CONVERSATION_HISTORY[chat_id] = []


def _check_and_claim_update(update_id: str) -> Optional[Tuple[str, List[str]]]:
    """
    Atomically claim an update_id at the START of the turn.

    Returns:
      - None if this is a NEW update (claim succeeded) → processing proceeds.
      - (reply_text, files) tuple if this update was already processed → return cached result.

    The INSERT ... ON CONFLICT DO NOTHING + RETURNING pattern is atomic: two racing
    workers can never both claim the same update_id, even across bot restarts,
    because the row insert is the single source of truth in PostgreSQL.
    """
    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO idempotency_log (update_id)
                VALUES (%s)
                ON CONFLICT (update_id) DO NOTHING
                RETURNING update_id
            """, (str(update_id),))
            claimed = cur.fetchone()
            if claimed is None:
                # Duplicate delivery — fetch the cached reply (if stored yet)
                cur.execute("SELECT reply_text, file_paths FROM idempotency_log WHERE update_id = %s", (str(update_id),))
                row = cur.fetchone()
                if row and row.get("reply_text"):
                    import json as _json
                    try:
                        files = _json.loads(row["file_paths"]) if row.get("file_paths") else []
                    except (ValueError, TypeError):
                        files = []
                    return (row["reply_text"], files)
                # Processed by another worker but reply not yet stored (race) — safe default
                return ("This update is already being processed.", [])
            cur.close()
        return None
    finally:
        conn.close()


def _store_idempotency_reply(update_id: str, reply_text: str, files: List[str]):
    """Persist the final reply for this update so redelivered duplicates return it verbatim."""
    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("""
                UPDATE idempotency_log
                SET reply_text = %s, file_paths = %s
                WHERE update_id = %s
            """, (reply_text, json.dumps(files), str(update_id)))
            cur.close()
    except Exception as e:
        logger.warning(f"Could not cache idempotency reply for {update_id}: {e}")
    finally:
        conn.close()


def _call_llm_with_failover(messages, tools, max_tokens=1000):
    """
    Call the LLM API with automatic failover to the backup API key on rate limit (429) errors.
    Tries current key → on 429/rate limit → switches to next key → retries once.
    """
    max_attempts = 3  # key1 → key2 → key1 (with small delay)
    for attempt in range(max_attempts):
        client = get_llm_client()
        try:
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                max_tokens=max_tokens
            )
            return response
        except Exception as e:
            error_str = str(e)
            # Check if it's a rate limit error (429) or token limit exceeded
            is_rate_limit = "429" in error_str or "rate_limit" in error_str.lower() or "too many requests" in error_str.lower()
            if is_rate_limit and attempt < max_attempts - 1:
                logger.warning(f"Rate limit hit on attempt {attempt + 1}: {error_str[:100]}")
                failover_to_next_key()
                time.sleep(1)  # Brief pause before retrying with new key
                continue
            else:
                raise  # Re-raise non-rate-limit errors or final attempt failures


def run_agent_turn(
    user_message: str,
    chat_id: int,
    owner_id: str,
    update_id: Optional[str] = None
) -> Tuple[str, List[str]]:
    """
    Executes a multi-turn agent control loop for an incoming user message.
    Returns a tuple of: (final_reply_text, list_of_generated_file_paths)
    """
    # Reset key rotation at start of each user turn
    reset_key_rotation()

    # ── 1. Idempotency: atomically claim the update BEFORE any tool can run ──
    if update_id:
        cached = _check_and_claim_update(str(update_id))
        if cached is not None:
            logger.info(f"Duplicate update_id {update_id} — returning cached reply.")
            return cached

    # ── 2. Fetch persistent standing preferences & shop session info ──
    from skills.auth import get_user_session
    session = get_user_session(owner_id)
    shop_info = ""
    if session:
        shop_info = (
            f"ACTIVE CONNECTED SHOP SESSION:\n"
            f"• Shop Name: {session['shop_name']}\n"
            f"• Address: {session['shop_address'] or 'Not specified'}\n"
            f"• GSTIN: {session['shop_gstin'] or 'Not specified'}\n"
        )

    prefs = get_all_preferences(owner_id)
    pref_str = "\n".join([f"- {k}: {v}" for k, v in prefs.items()]) if prefs else "None set yet."

    dynamic_system_prompt = f"{SYSTEM_PROMPT}\n\n{shop_info}\nSTANDING OWNER PREFERENCES (Persisted in DB across chats):\n{pref_str}\n"

    # ── 3. Initialize or fetch conversation history ──
    if chat_id not in CONVERSATION_HISTORY:
        CONVERSATION_HISTORY[chat_id] = []

    messages = CONVERSATION_HISTORY[chat_id]

    # Prepend dynamic system message if not present
    if not messages or messages[0].get("role") != "system":
        messages.insert(0, {"role": "system", "content": dynamic_system_prompt})
    else:
        messages[0] = {"role": "system", "content": dynamic_system_prompt}

    # Append user input
    messages.append({"role": "user", "content": user_message})

    # ⚡ Smart Context Compression: Summarize older chat turns into a single context memory message
    if len(messages) > 11:
        # System prompt is index 0
        system_msg = messages[0]
        # Older turns to summarize: from index 1 up to index -10
        older_turns = messages[1:-10]
        recent_turns = messages[-10:]

        # Build concise summary block of past context
        summary_lines = []
        for m in older_turns:
            role = m.get("role")
            content = m.get("content")
            if role == "user" and content:
                summary_lines.append(f"User asked: {content}")
            elif role == "assistant" and content and not m.get("tool_calls"):
                summary_lines.append(f"Agent summary: {content[:150]}...")

        summary_text = "\n".join(summary_lines[-6:])  # Keep key highlights
        context_summary_msg = {
            "role": "user",
            "content": f"[CONVERSATION CONTEXT SUMMARY OF EARLIER TURNS]:\n{summary_text}\n\n[Continuing conversation below]:"
        }

        CONVERSATION_HISTORY[chat_id] = [system_msg, context_summary_msg] + recent_turns
        messages = CONVERSATION_HISTORY[chat_id]

    generated_files: List[str] = []
    max_steps = 15
    step_count = 0

    # ── 4. Multi-step Agent Loop ──
    while step_count < max_steps:
        step_count += 1
        try:
            response = _call_llm_with_failover(messages, TOOLS_SCHEMA)
        except Exception as e:
            logger.error(f"Error calling LLM API: {e}")
            error_reply = "Apologies, an error occurred while processing your request with the AI engine. Please try again in a moment."
            if update_id:
                _store_idempotency_reply(str(update_id), error_reply, [])
            return (error_reply, [])

        assistant_msg = response.choices[0].message

        # Format message object to append to messages history
        msg_dict = {"role": "assistant"}
        if assistant_msg.content:
            msg_dict["content"] = assistant_msg.content
        if assistant_msg.tool_calls:
            msg_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    }
                } for tc in assistant_msg.tool_calls
            ]

        messages.append(msg_dict)

        # If no tool calls requested, we have the final model response!
        if not assistant_msg.tool_calls:
            final_text = assistant_msg.content or "Done."
            # Cache the final reply so Telegram redeliveries return it verbatim
            if update_id:
                _store_idempotency_reply(str(update_id), final_text, generated_files)
            return (final_text, generated_files)

        # ── 5. Handle Tool Execution ──
        for tc in assistant_msg.tool_calls:
            func_name = tc.function.name
            func_args_str = tc.function.arguments

            try:
                func_args = json.loads(func_args_str) if func_args_str else {}
            except Exception:
                func_args = {}

            # Inject owner_id where applicable
            if func_name in ["set_preference", "get_preference"]:
                func_args["owner_id"] = str(owner_id)

            logger.info(f"Executing Tool Call: {func_name} with args: {func_args}")

            if func_name in TOOL_DISPATCH:
                try:
                    tool_result = TOOL_DISPATCH[func_name](**func_args)

                    # Auto-update default_payment_mode preference when payment_mode is specified
                    if func_name in ["quick_create_bill", "finalize_bill"] and func_args.get("payment_mode"):
                        try:
                            from skills.preferences import set_preference
                            set_preference(str(owner_id), "default_payment_mode", str(func_args["payment_mode"]).lower().strip())
                        except Exception as e_pref:
                            logger.warning(f"Could not auto-update default payment mode preference: {e_pref}")

                    # Track file generation output if applicable
                    if isinstance(tool_result, dict) and "file_path" in tool_result:
                        generated_files.append(tool_result["file_path"])

                    result_content = json.dumps(tool_result, default=str)
                except Exception as err:
                    logger.error(f"Tool '{func_name}' raised: {err}", exc_info=True)
                    result_content = json.dumps({"status": "error", "message": f"Tool execution failed: {str(err)[:200]}"})
            else:
                result_content = json.dumps({"status": "error", "message": f"Tool '{func_name}' not recognized."})

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result_content
            })

    max_iter_reply = "I reached the maximum number of processing steps for this request. Please rephrase or continue in a new message."
    if update_id:
        _store_idempotency_reply(str(update_id), max_iter_reply, generated_files)
    return (max_iter_reply, generated_files)
