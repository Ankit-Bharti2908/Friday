"""Routing accuracy eval — needs a working `fast` tier (Ollama or a cloud key).

    uv run python -m tests.eval_routing

Run after every router-prompt or model change; treat < 90% as a regression.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from core.router import INTENT_TO_PROFILE, classify

CASES = json.loads((Path(__file__).parent / "routing_cases.json").read_text())


async def main() -> None:
    correct = 0
    for case in CASES:
        profile, route = await classify(case["text"])
        expected = INTENT_TO_PROFILE[case["intent"]]
        got_intent = route.intent if route else "?"
        ok = (route is not None and route.intent == case["intent"]) or profile == expected
        correct += ok
        print(f"{'✓' if ok else '✗'}  {case['text'][:55]:<55} want={case['intent']:<8} got={got_intent}")
    pct = 100 * correct / len(CASES)
    print(f"\naccuracy: {correct}/{len(CASES)} = {pct:.0f}%  ({'PASS' if pct >= 90 else 'REGRESSION — fix router prompt'})")


if __name__ == "__main__":
    asyncio.run(main())
