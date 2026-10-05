"""Rule-based token safety filters, applied to every copy buy before the AI screen."""
from __future__ import annotations

from datetime import datetime, timezone

from bot.models import BotConfig


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _age_minutes(info: dict) -> float | None:
    created = (info.get("firstPool") or {}).get("createdAt") or info.get("createdAt")
    if not created:
        return None
    try:
        if isinstance(created, (int, float)):
            ts = datetime.fromtimestamp(created / 1000 if created > 1e12 else created, timezone.utc)
        else:
            ts = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts).total_seconds() / 60
    except ValueError:
        return None


def check_token(cfg: BotConfig, mint_info: dict, token_info: dict) -> tuple[bool, str]:
    """Return (ok, reason). mint_info comes from chain (authoritative), token_info from Jupiter.

    Missing Jupiter data never blocks a trade (brand-new tokens often aren't indexed yet);
    missing on-chain data does, because then we can't verify authorities at all.
    """
    if cfg.require_mint_disabled or cfg.require_freeze_disabled:
        if not mint_info:
            return False, "could not read token mint on-chain"
        if cfg.require_mint_disabled and mint_info.get("mintAuthority"):
            return False, "mint authority still enabled (dev can print tokens)"
        if cfg.require_freeze_disabled and mint_info.get("freezeAuthority"):
            return False, "freeze authority enabled (dev can freeze your tokens)"
    notes = []
    liq = _num(token_info.get("liquidity"))
    if cfg.min_liquidity_usd > 0:
        if liq is not None and liq < cfg.min_liquidity_usd:
            return False, f"liquidity ${liq:,.0f} < min ${cfg.min_liquidity_usd:,.0f}"
        if liq is None:
            notes.append("liquidity unknown")
    top = _num((token_info.get("audit") or {}).get("topHoldersPercentage"))
    if cfg.max_top_holders_pct > 0 and top is not None and top > cfg.max_top_holders_pct:
        return False, f"top holders own {top:.0f}% > max {cfg.max_top_holders_pct:g}%"
    age = _age_minutes(token_info)
    if cfg.min_token_age_min > 0 and age is not None and age < cfg.min_token_age_min:
        return False, f"token is {age:.0f} min old < min {cfg.min_token_age_min:g} min"
    return True, "; ".join(notes)
