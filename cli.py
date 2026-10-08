#!/usr/bin/env python3
"""Terminal chat. Examples:
    python cli.py                          # alex@example.com, offline brain unless ANTHROPIC_API_KEY is set
    python cli.py --email priya@example.com --trace
    python cli.py --brain llm              # force Claude tool calling
"""
import argparse

from support_agent import build_agent

DEMO_CUSTOMERS = {"alex@example.com": "ORD-1001..1003", "priya@example.com": "ORD-2001..2003"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="alex@example.com", help="simulated signed-in customer")
    ap.add_argument("--brain", choices=["auto", "offline", "llm"], default="auto")
    ap.add_argument("--trace", action="store_true", help="show tool calls")
    ap.add_argument("--no-persist", action="store_true", help="don't write runtime/ state")
    args = ap.parse_args()

    agent = build_agent(args.email, brain=args.brain, persist=not args.no_persist)
    print(f"Support assistant [{agent.brain.name}] - signed in as {args.email}  (type 'quit' to exit)")
    print("Try: 'Where is my order ORD-1001?', 'Cancel ORD-1002', 'What's the return policy?'\n")
    while True:
        try:
            msg = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if msg.lower() in {"quit", "exit"}:
            break
        reply = agent.handle(msg)
        if args.trace:
            for c in reply.tool_calls:
                print(f"   [tool] {c['tool']}({c['args']}) -> {'ok' if c['ok'] else c['error']}")
            if reply.blocked:
                print(f"   [guard] blocked: {reply.blocked}")
        print(f"bot> {reply.text}\n")


if __name__ == "__main__":
    main()
