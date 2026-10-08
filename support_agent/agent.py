"""Agent orchestrator.

One user message flows through:

  input guard -> pending-confirmation check -> escalation rules -> brain (tool loop)
                                                                    |
                                          every tool call goes through `_execute`
                                          (allow-list args, confirm sensitive, audit, trace)

The "brain" is pluggable: `AnthropicBrain` (LLM tool calling) or `OfflineBrain`
(deterministic rules, no API key). Both use the same tools and the same safety layer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .audit import AuditLog
from .config import MAX_HISTORY_TURNS, RUNTIME_DIR
from .rag import PolicyIndex
from .safety import (PendingAction, check_escalation, check_input, parse_confirmation, redact)
from .store import Store
from .tools import TOOLS, ToolContext, create_support_ticket


@dataclass
class AgentReply:
    text: str
    tool_calls: list[dict] = field(default_factory=list)   # trace: name, args, ok, error
    awaiting_confirmation: bool = False
    escalated: bool = False
    blocked: str = ""                                       # reason if the input guard fired


class SupportAgent:
    def __init__(self, store: Store, policies: PolicyIndex, customer_email: str,
                 brain, audit: AuditLog | None = None):
        self.ctx = ToolContext(store=store, policies=policies, customer_email=customer_email)
        self.brain = brain
        self.audit = audit or AuditLog(None)
        self.history: list[dict] = []
        self.session: dict = {}
        self.pending: PendingAction | None = None
        self._trace: list[dict] = []

    # ------------------------------------------------------------------ public API
    def handle(self, message: str) -> AgentReply:
        self._trace = []
        verdict = check_input(message)
        if not verdict.ok:
            self.audit.write(event="input_blocked", reason=verdict.reason, customer=self.ctx.customer_email,
                             message=message[:200])
            return self._finish(message, AgentReply(self._blocked_text(verdict.reason), blocked=verdict.reason))

        if self.pending:
            reply = self._resolve_pending(message)
            if reply:
                return self._finish(message, reply)

        escalation = check_escalation(message)
        if escalation:
            res = self._execute("create_support_ticket", {
                "subject": f"Escalation: {escalation.reason}",
                "description": f"Customer message: {message}",
                "priority": escalation.priority,
                "order_id": self.session.get("last_order_id") or "",
            })
            ticket = res.get("ticket_id")
            text = escalation.customer_message
            if ticket:
                text += f" Your ticket number is {ticket} (response {res['response_time']})."
            return self._finish(message, AgentReply(text, escalated=True))

        text = self.brain.respond(message, self.history[-MAX_HISTORY_TURNS:], self._execute, self.session)
        return self._finish(message, AgentReply(text, awaiting_confirmation=self.pending is not None))

    # ------------------------------------------------------------------ internals
    def _finish(self, user_message: str, reply: AgentReply) -> AgentReply:
        reply.tool_calls = list(self._trace)
        reply.awaiting_confirmation = reply.awaiting_confirmation or self.pending is not None
        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": reply.text})
        return reply

    @staticmethod
    def _blocked_text(reason: str) -> str:
        if reason == "empty":
            return "Sorry, I didn't catch that. How can I help?"
        if reason == "too_long":
            return "That message is a bit long for me. Could you shorten it to the key details?"
        return ("I can't help with that request. I can check orders, cancel eligible orders, track "
                "refunds, find products, and answer questions about our policies. What do you need?")

    def _resolve_pending(self, message: str) -> AgentReply | None:
        """Handle the customer's yes/no to a pending state-changing action (decided in code, not by the LLM)."""
        pending, self.pending = self.pending, None
        answer = parse_confirmation(message)
        if answer is None:
            return None          # unrelated message: drop the pending action and carry on
        if answer is False:
            self.audit.write(event="action_declined", tool=pending.tool, args=pending.args)
            return AgentReply("No problem, I haven't changed anything. Is there anything else I can help with?")
        result = self._run_tool(pending.tool, pending.args, dry_run=False)
        if result.get("ok") and pending.tool == "cancel_order":
            return AgentReply(
                f"Done. Order {result['order_id']} has been cancelled. A refund of "
                f"{result['refund_amount']:.2f} {result['currency']} will go back to your original payment "
                f"method within {result['refund_expected']}. Anything else I can help with?")
        return AgentReply(result.get("message", "Something went wrong, and nothing was changed."))

    def _run_tool(self, name: str, args: dict, dry_run: bool | None = None) -> dict:
        tool = TOOLS[name]
        clean = {k: v for k, v in args.items() if k in tool.properties}      # allow-list arguments
        if tool.sensitive and dry_run is not None:
            clean["dry_run"] = dry_run
        try:
            return tool.fn(self.ctx, **clean)
        except TypeError as exc:
            return {"ok": False, "error": "invalid_arguments", "message": str(exc)}
        except Exception:                                                     # never leak a stack trace
            return {"ok": False, "error": "internal_error",
                    "message": "A system error occurred. Please try again or ask for a human agent."}

    def _execute(self, name: str, args: dict) -> dict:
        """The only path from a brain to a tool."""
        args = dict(args or {})
        if name not in TOOLS:
            result = {"ok": False, "error": "unknown_tool", "message": f"No such tool: {name}"}
        elif TOOLS[name].sensitive:
            # Never run directly: validate with a dry run, then wait for explicit customer approval.
            result = self._run_tool(name, args, dry_run=True)
            if result.get("ok"):
                clean = {k: v for k, v in args.items() if k in TOOLS[name].properties}
                summary = (f"Cancel order {result['order_id']} ({', '.join(result['items'])}) and refund "
                           f"{result['refund_amount']:.2f} {result['currency']}")
                self.pending = PendingAction(name, clean, summary)
                result = {"ok": True, "status": "awaiting_customer_confirmation",
                          "message": f"{summary}. Ask the customer to reply 'yes' to confirm or 'no' to keep the order.",
                          "order_id": result["order_id"]}
        else:
            result = self._run_tool(name, args)

        if name == "get_order_status" and result.get("ok"):
            self.session["last_order_id"] = result["order_id"]
        self._trace.append({"tool": name, "args": args, "ok": result.get("ok", False),
                            "error": result.get("error")})
        self.audit.write(event="tool_call", tool=name, args=args, ok=result.get("ok"),
                         error=result.get("error"), customer=self.ctx.customer_email)
        return result


def build_agent(customer_email: str, brain: str = "auto", persist: bool = True,
                store: Store | None = None, policies: PolicyIndex | None = None) -> SupportAgent:
    """Factory. brain: 'auto' (LLM if ANTHROPIC_API_KEY is set, else offline), 'llm', or 'offline'."""
    store = store or Store(runtime_dir=RUNTIME_DIR if persist else None)
    policies = policies or PolicyIndex()
    use_llm = brain == "llm" or (brain == "auto" and os.getenv("ANTHROPIC_API_KEY"))
    if use_llm:
        from .llm_brain import AnthropicBrain
        engine = AnthropicBrain()
    else:
        from .offline_brain import OfflineBrain
        engine = OfflineBrain()
    audit = AuditLog(RUNTIME_DIR / "audit.jsonl" if persist else None)
    return SupportAgent(store, policies, customer_email, engine, audit)


__all__ = ["SupportAgent", "AgentReply", "build_agent", "create_support_ticket", "redact"]
