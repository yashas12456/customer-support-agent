"""FastAPI server + minimal chat UI.   Run: uvicorn api:app --reload   then open http://localhost:8000

DEMO ONLY: the customer email comes from the request body so you can switch personas.
In production derive it from the authenticated session/JWT; never trust the client.
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from support_agent import build_agent
from support_agent.rag import PolicyIndex
from support_agent.store import Store
from support_agent.config import RUNTIME_DIR

app = FastAPI(title="AI Customer Support Agent")
_store, _policies = Store(runtime_dir=RUNTIME_DIR), PolicyIndex()   # shared "backend"
_sessions: dict[str, object] = {}


class ChatIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    email: str = "alex@example.com"
    message: str = Field(max_length=4000)


@app.post("/api/chat")
def chat(body: ChatIn):
    key = f"{body.session_id}:{body.email}"
    if key not in _sessions:
        _sessions[key] = build_agent(body.email, store=_store, policies=_policies)
    reply = _sessions[key].handle(body.message)
    return {
        "reply": reply.text,
        "tool_calls": reply.tool_calls,
        "awaiting_confirmation": reply.awaiting_confirmation,
        "escalated": reply.escalated,
        "blocked": reply.blocked,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")
