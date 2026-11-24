"""What a model request cost, in credits (1 credit = 1 US cent), for DeepSeek, OpenAI and Claude.

No provider sends a cost back: each request is priced from its usage JSON and the provider's published prices.
Prices read 2026-10-24 from api-docs.deepseek.com/quick_start/pricing, developers.openai.com/api/docs/pricing
and platform.claude.com/docs/en/about-claude/pricing. Update them here when a provider changes its prices; a
model missing here is recorded with no price rather than a guess.

DeepSeek's usage says nothing about peak or off-peak: the tier follows from when the request ran. Peak is
01:00-04:00 and 06:00-10:00 UTC, Monday to Friday, except Chinese public holidays; every other hour is off-peak
at half price. OpenAI and Claude have no time-based prices.
"""

from datetime import datetime, timedelta, timezone

from .config import settings

# USD per 1M tokens at peak: (input cache hit, input cache miss, output). Off-peak is half.
DEEPSEEK_PEAK = {
    "deepseek-flash": (0.006, 0.30, 1.20),
    "deepseek-v4-flash": (0.006, 0.30, 1.20),
    "deepseek-v4-flash-vision-exp": (0.006, 0.30, 1.20),
    "deepseek-v4-pro": (0.044, 1.32, 3.96),
}
PEAK_HOURS_UTC = ((1, 4), (6, 10))
BEIJING = timezone(timedelta(hours=8))

# USD per 1M tokens, standard tier: (input, cached input, 30m cache write, 12h cache write, output), up to
# 272K input tokens and above it.
OPENAI_LONG_CONTEXT = 272_000
OPENAI = {
    "gpt-6-astra": ((10.00, 1.00, 12.50, 25.00, 50.00), (20.00, 2.00, 25.00, 50.00, 75.00)),
    "gpt-6.1-sol": ((2.00, 0.10, 2.50, 5.00, 10.00), (4.00, 0.20, 5.00, 10.00, 15.00)),
    "gpt-6-sol": ((2.00, 0.20, 2.50, 5.00, 10.00), (4.00, 0.40, 5.00, 10.00, 15.00)),
    "gpt-6-luna": ((0.10, 0.01, 0.125, 0.25, 0.50), (0.20, 0.02, 0.25, 0.50, 0.75)),
}

# USD per 1M tokens: (input, 5m cache write, 1h cache write, cache read, output). Haiku 5.5 has a second row for
# prompts over 100K tokens (cache reads and writes count toward the prompt).
ANTHROPIC = {
    "claude-fable-5-1": ((10.00, 12.50, 20.00, 0.25, 50.00),),
    "claude-fable-5": ((10.00, 12.50, 20.00, 1.00, 50.00),),
    "claude-opus-5-5": ((4.00, 5.00, 8.00, 0.20, 20.00),),
    "claude-sonnet-5-5": ((2.00, 2.50, 4.00, 0.10, 10.00),),
    "claude-sonnet-5": ((2.00, 2.50, 4.00, 0.20, 10.00),),
    "claude-haiku-5-5": ((0.10, 0.125, 0.20, 0.01, 0.50), (0.50, 0.625, 1.00, 0.05, 2.50)),
}
HAIKU_LONG_PROMPT = 100_000


def holidays():
    """Chinese public holidays (Beijing dates, YYYY-MM-DD) from DEEPSEEK_HOLIDAYS, billed off-peak all day."""
    return {d.strip() for d in settings.deepseek_holidays.split(",") if d.strip()}


def is_peak(when):
    """Whether a request made at unix time `when` is billed at DeepSeek's peak price."""
    utc = datetime.fromtimestamp(when, timezone.utc)
    if utc.weekday() >= 5 or utc.astimezone(BEIJING).date().isoformat() in holidays():
        return False
    return any(start <= utc.hour < end for start, end in PEAK_HOURS_UTC)


def priced(table, model):
    """The model's prices: by exact name, else the longest name it starts with (a dated snapshot)."""
    if model in table:
        return table[model]
    names = [name for name in table if model and model.startswith(name)]
    return table[max(names, key=len)] if names else None


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def tokens(provider, usage):
    """One request's tokens in the same terms for every provider: {"input" (the whole prompt), "cached" (read
    from cache), "written" (written to cache: {"short", "long"} TTL), "output"}."""
    if provider == "anthropic":
        written = usage.get("cache_creation") or {}
        write = number(usage.get("cache_creation_input_tokens"))
        short = number(written.get("ephemeral_5m_input_tokens"))
        long = number(written.get("ephemeral_1h_input_tokens"))
        if short + long != write:  # no breakdown: Relay's cache markers are all 5-minute
            short, long = write, 0
        cached = number(usage.get("cache_read_input_tokens"))
        return {"input": number(usage.get("input_tokens")) + cached + write, "cached": cached,
                "written": {"short": short, "long": long}, "output": number(usage.get("output_tokens"))}
    if provider == "openai":
        # Responses API names, else chat completions names; reads and writes are inside the input count.
        details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
        write = number(details.get("cache_write_tokens"))
        split = details.get("cache_write_tokens_details") or {}
        short, long = number(split.get("ttl_30m_tokens")), number(split.get("ttl_12h_tokens"))
        if short + long != write:
            short, long = write, 0
        return {"input": number(usage.get("input_tokens", usage.get("prompt_tokens"))),
                "cached": number(details.get("cached_tokens")), "written": {"short": short, "long": long},
                "output": number(usage.get("output_tokens", usage.get("completion_tokens")))}
    hit = number(usage.get("prompt_cache_hit_tokens"))
    return {"input": number(usage.get("prompt_tokens")), "cached": hit, "written": {"short": 0, "long": 0},
            "output": number(usage.get("completion_tokens"))}


def cost(provider, model, usage, when):
    """(credits, peak) for one request; credits is None for a model with no known price. Only DeepSeek has a
    peak price."""
    peak = provider == "deepseek" and is_peak(when)
    t = tokens(provider, usage)
    short, long = t["written"]["short"], t["written"]["long"]
    if provider == "anthropic":
        rows = priced(ANTHROPIC, model)
        if not rows:
            return None, peak
        p = rows[-1] if len(rows) > 1 and t["input"] > HAIKU_LONG_PROMPT else rows[0]
        plain = t["input"] - t["cached"] - short - long
        usd = plain * p[0] + short * p[1] + long * p[2] + t["cached"] * p[3] + t["output"] * p[4]
    elif provider == "openai":
        rows = priced(OPENAI, model)
        if not rows:
            return None, peak
        p = rows[1] if t["input"] > OPENAI_LONG_CONTEXT else rows[0]
        plain = max(t["input"] - t["cached"] - short - long, 0)
        usd = plain * p[0] + t["cached"] * p[1] + short * p[2] + long * p[3] + t["output"] * p[4]
    else:
        p = priced(DEEPSEEK_PEAK, model)
        if not p:
            return None, peak
        miss = usage.get("prompt_cache_miss_tokens")
        miss = number(miss) if miss is not None else max(t["input"] - t["cached"], 0)
        usd = (t["cached"] * p[0] + miss * p[1] + t["output"] * p[2]) * (1 if peak else 0.5)
    return usd / 1_000_000 * 100, peak


def add_up(total, usage):
    """`usage` summed into `total`, field by field, nested objects too (the chat's aggregate usage JSON)."""
    for key, value in usage.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            total[key] = total.get(key, 0) + value
        elif isinstance(value, dict):
            add_up(total.setdefault(key, {}), value)
    return total
