"""One LLM call, with a spending total shared by every LLM step of a run.

The total is kept in <run>/spent.json. A call won't start once the total
has reached the budget.
"""

import json
from pathlib import Path

# Dollars per token, for the models we use: input, output.
PRICES = {
    "bedrock/us.anthropic.claude-opus-4-5-20251101-v1:0": (5e-6, 25e-6),
}


def spent(run):
    ledger = Path(run) / "spent.json"
    return json.loads(ledger.read_text())["usd"] if ledger.exists() else 0.0


def ask(run, model, budget, system, prompt, max_tokens=None):
    """The answer's text, and info: model, tokens, cost, finish reason."""
    import litellm

    if model not in PRICES:
        raise SystemExit(f"no price for {model}; add it to PRICES")
    if spent(run) >= budget:
        raise SystemExit(f"budget reached: ${spent(run):.2f} of ${budget}")
    limit = {} if max_tokens is None else {"max_tokens": max_tokens}
    r = litellm.completion(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        **limit,
    )
    price_in, price_out = PRICES[model]
    usage = r.usage
    usd = usage.prompt_tokens * price_in + usage.completion_tokens * price_out
    info = {
        "model": model,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "usd": round(usd, 4),
        # "length" means the answer was cut off at the token limit.
        "finish_reason": r.choices[0].finish_reason,
    }
    total = round(spent(run) + usd, 4)
    (Path(run) / "spent.json").write_text(json.dumps({"usd": total}))
    return r.choices[0].message.content, info
