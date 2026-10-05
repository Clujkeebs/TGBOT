"""AI manager powered by Claude.

Three jobs:

1. **Buy screen**: before a copy buy executes, Claude looks at the token
   (liquidity, holders, mint/freeze authority, organic score, age), the
   leader's track record and our exposure, and returns approve / reduce /
   reject. Fails open: if the AI is slow or down, the rule-based decision stands.
2. **Periodic review**: every N hours Claude reviews every leader's results and
   our open positions. It can re-weight, pause or reset leaders and tune
   settings. With autonomy `advise` the changes are sent to you with an
   "Apply" button; with `manage` they are applied and reported.
3. **Chat**: any plain-text message to the bot goes to Claude, which can read
   the bot's state and change settings for you ("cut Whale A's size in half").

Hard guardrails live in code, not in the prompt: the AI cannot withdraw,
export keys, place trades, switch to auto mode, or raise the max trade size
above `ai_max_trade_sol` (only you can change that, with /ai cap).
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from bot.config import WSOL_MINT
from bot.models import Leader, Position, Trade, utcnow
from bot.notifier import esc
from bot.swap_parser import ParsedSwap

log = logging.getLogger("copybot.ai")

FALLBACK_BETA = "server-side-fallback-2026-07-01"
SCREEN_TIMEOUT_S = 25.0
CHAT_IDLE_RESET_S = 30 * 60
CHAT_MAX_MESSAGES = 40
MAX_TOOL_ROUNDS = 12

SYSTEM = """You manage a Solana memecoin copy-trading bot for its owner, who talks to you through Telegram.

How the bot works: it follows several "leader" wallets at once. When a leader buys a token, the bot buys too (size set by the sizing mode, the leader's weight %, the leader's max SOL and the global caps). When the leader who opened a position sells X% of their tokens, the bot sells X% of ours. Each leader can override the global mode (auto / confirm / notify). Amounts are in SOL.

Your priorities, in order: protect capital, cut leaders that lose money, give more size to leaders with a real edge, keep the owner informed. Memecoins are extremely risky; most copied trades lose. A leader with few trades has no proven edge yet, so judge small samples cautiously. Prefer small, reversible changes (weights, per-leader max SOL, pausing a leader) over large global ones.

You cannot withdraw funds, reveal keys, place trades, or switch the bot to auto mode. Changes you make are bounded by code-enforced limits; if a tool reports a limit, explain it rather than retrying.

Write for a phone screen: plain text, no markdown, no tables. Be concise and concrete: name leaders, numbers and the reason for each change."""

SCREEN_SYSTEM = """You are the risk screen for a Solana memecoin copy-trading bot. A followed leader wallet just bought a token and the bot is about to copy the buy. Decide quickly:

- approve: normal size.
- reduce: copy with a smaller size (size_multiplier between 0.1 and 0.9).
- reject: do not copy.

Red flags: mint or freeze authority still enabled, very low liquidity relative to our size, a handful of wallets holding most of the supply, a token minutes old with no organic activity, a leader with a losing record, or our size being a large share of the pool. Missing data is a mild negative, not an automatic reject. A strong leader record and healthy token stats justify approval. Keep the reason under 20 words."""

SCREEN_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["approve", "reduce", "reject"]},
        "size_multiplier": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["decision", "size_multiplier", "reason"],
    "additionalProperties": False,
}


def _nullable(t: str, **extra) -> dict:
    return {"anyOf": [{"type": t, **extra}, {"type": "null"}]}


def _tool(name: str, description: str, props: dict | None = None) -> dict:
    props = props or {}
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {"type": "object", "properties": props, "required": list(props),
                         "additionalProperties": False},
    }


READ_TOOLS = [
    _tool("get_overview", "Bot settings, wallet balance, portfolio value and activity counters."),
    _tool("get_leaders", "Every followed leader with weight, mode, max SOL, trade counts and realized P/L of positions they opened."),
    _tool("get_positions", "Open token positions with cost basis, current value, unrealized P/L and which leader opened each."),
    _tool("get_recent_trades", "Recent detected leader trades and what the bot did.", {
        "limit": {"type": "integer", "description": "1-50"},
        "leader": _nullable("string", description="Leader address or label to filter by, or null for all"),
    }),
    _tool("get_token_info", "Liquidity, holders, audit flags, organic score and price stats for a token mint.", {
        "mint": {"type": "string"},
    }),
]

WRITE_TOOLS = [
    _tool("update_leader", "Change one leader's settings. Pass null for anything you don't want to change.", {
        "leader": {"type": "string", "description": "Leader address or label"},
        "weight_pct": _nullable("number", description="Size multiplier %, 0-200 (100 = normal)"),
        "active": _nullable("boolean", description="false stops following this leader"),
        "mode": _nullable("string", enum=["default", "confirm", "notify"],
                          description="default = use the global mode; notify = alerts only for this leader"),
        "max_sol": _nullable("number", description="Max SOL per copied buy for this leader, 0 = no per-leader cap"),
        "note": _nullable("string", description="Short note on why (stored and shown in /leaders)"),
    }),
    _tool("update_settings", "Change global trading settings. Pass null for anything you don't want to change.", {
        "sizing_mode": _nullable("string", enum=["fixed", "percent", "mirror"]),
        "sizing_value": _nullable("number", description="fixed: SOL per buy; percent: % of SOL balance; mirror: multiplier %"),
        "min_trade_sol": _nullable("number"),
        "max_trade_sol": _nullable("number"),
        "slippage_pct": _nullable("number", description="0.5-30"),
        "tp_pct": _nullable("number", description="take-profit %, 0 disables"),
        "sl_pct": _nullable("number", description="stop-loss %, 0 disables"),
        "daily_loss_limit_pct": _nullable("number", description="1-50"),
        "copy_sells": _nullable("boolean"),
    }),
    _tool("pause_copying", "Pause all copying immediately (alerts continue). Use when something looks wrong.", {
        "reason": {"type": "string"},
    }),
]

CHAT_ONLY_TOOLS = [
    _tool("resume_copying", "Resume copying after a pause. Only when the owner asks."),
    _tool("add_leader", "Start following a new wallet. Only when the owner gives the address.", {
        "address": {"type": "string"},
        "label": {"type": "string"},
    }),
]


@dataclass
class Verdict:
    decision: str
    size_multiplier: float
    reason: str


@dataclass
class Proposal:
    created: float
    actions: list[tuple[str, dict]] = field(default_factory=list)


class AIManager:
    def __init__(self, client: Any, model: str, engine: Any):
        self.client = client
        self.model = model
        self.engine = engine
        self.db = engine.db
        self.notify = engine.notify
        self._chat: list = []
        self._chat_last = 0.0
        self._chat_lock = asyncio.Lock()
        self._review_lock = asyncio.Lock()
        self._proposals: dict[str, Proposal] = {}
        self._last_review = time.time()
        self.stats = {"screens": 0, "rejects": 0, "reduces": 0, "reviews": 0, "errors": 0}

    # ------------------------------------------------------------------ #
    # Claude call
    # ------------------------------------------------------------------ #
    async def _create(self, *, system: str, messages: list, effort: str, max_tokens: int = 16000,
                      tools: list | None = None, output_format: dict | None = None):
        output_config: dict = {"effort": effort}
        if output_format:
            output_config["format"] = output_format
        kwargs: dict = dict(model=self.model, max_tokens=max_tokens, system=system, messages=messages,
                            output_config=output_config, betas=[FALLBACK_BETA], fallbacks="default")
        if tools:
            kwargs["tools"] = tools
        return await self.client.beta.messages.create(**kwargs)

    @staticmethod
    def _text(resp) -> str:
        return "\n".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()

    async def _agent(self, system: str, messages: list, tools: list, effort: str, ctx: str,
                     proposal: Proposal | None = None) -> str:
        """Run the tool loop, appending to `messages` in place. Returns the final text."""
        for _ in range(MAX_TOOL_ROUNDS):
            resp = await self._create(system=system, messages=messages, tools=tools, effort=effort)
            if resp.stop_reason == "refusal":
                messages.append({"role": "assistant", "content": resp.content})
                return "The AI declined to answer that."
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "pause_turn":
                continue
            calls = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
            if resp.stop_reason != "tool_use" or not calls:
                return self._text(resp) or "(no reply)"
            results = []
            for call in calls:
                try:
                    out = await self._run_tool(call.name, dict(call.input or {}), ctx, proposal)
                    results.append({"type": "tool_result", "tool_use_id": call.id,
                                    "content": json.dumps(out, default=str)})
                except Exception as e:  # noqa: BLE001
                    results.append({"type": "tool_result", "tool_use_id": call.id,
                                    "content": f"Error: {e}", "is_error": True})
            messages.append({"role": "user", "content": results})
        return "Stopped: too many tool steps."

    # ------------------------------------------------------------------ #
    # 1. Buy screen
    # ------------------------------------------------------------------ #
    async def screen_buy(self, swap: ParsedSwap, leader: Leader | None, sol_amount: float,
                         leader_sol_value: float, token_info: dict | None = None) -> Verdict | None:
        try:
            return await asyncio.wait_for(self._screen(swap, leader, sol_amount, leader_sol_value, token_info),
                                          SCREEN_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            self.stats["errors"] += 1
            log.warning("AI screen failed (%s) - falling back to rules", e)
            return None

    async def _screen(self, swap, leader, sol_amount, leader_sol_value, token_info=None) -> Verdict | None:
        info = token_info if token_info is not None else await self.engine.jup.token_info(swap.token_mint)
        exposure = await self._exposure()
        ctx = {
            "token_mint": swap.token_mint,
            "token": _trim(info),
            "leader": _leader_dict(leader) if leader else {"address": swap.wallet, "note": "not in leader list"},
            "leader_trade": {"sol_value": round(leader_sol_value, 4),
                             "pct_of_leader_sol": round(leader_sol_value / swap.wallet_pre_sol * 100, 2)
                             if swap.wallet_pre_sol else None,
                             "already_held_tokens_before": swap.pre_token},
            "our_planned_buy_sol": round(sol_amount, 4),
            "our_exposure": exposure,
        }
        resp = await self._create(system=SCREEN_SYSTEM, effort="low", max_tokens=2000,
                                  messages=[{"role": "user", "content": json.dumps(ctx, default=str)}],
                                  output_format={"type": "json_schema", "schema": SCREEN_SCHEMA})
        if resp.stop_reason in ("refusal", "max_tokens"):
            return None
        data = json.loads(self._text(resp))
        v = Verdict(data["decision"], float(data.get("size_multiplier") or 1.0), str(data.get("reason", ""))[:200])
        if v.decision == "reduce":
            v.size_multiplier = max(0.1, min(0.9, v.size_multiplier))
        self.stats["screens"] += 1
        if v.decision == "reject":
            self.stats["rejects"] += 1
        elif v.decision == "reduce":
            self.stats["reduces"] += 1
        return v

    # ------------------------------------------------------------------ #
    # 2. Periodic review
    # ------------------------------------------------------------------ #
    async def review_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                cfg = await self.db.get_config()
                due = cfg.ai_review_hours > 0 and time.time() - self._last_review >= cfg.ai_review_hours * 3600
                if due and cfg.ai_autonomy != "off" and await self.db.active_leaders():
                    await self.review()
            except Exception:  # noqa: BLE001
                self.stats["errors"] += 1
                log.exception("AI review failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=300)
            except asyncio.TimeoutError:
                pass

    async def review(self) -> None:
        async with self._review_lock:
            self._last_review = time.time()
            cfg = await self.db.get_config()
            manage = cfg.ai_autonomy == "manage"
            proposal = None if manage else Proposal(time.time())
            msg = ("Run your scheduled review. Look at every leader and our open positions, then decide what to "
                   "change. " + ("Apply changes with the tools; they take effect immediately." if manage else
                                 "Your changes are proposals: the owner will see them and decide whether to apply.")
                   + " Finish with a short report: what you changed or propose and why, and anything the owner "
                   "should watch. If nothing needs changing, say so in one or two lines.")
            text = await self._agent(SYSTEM, [{"role": "user", "content": msg}], READ_TOOLS + WRITE_TOOLS,
                                     "high", "manage" if manage else "advise", proposal)
            self.stats["reviews"] += 1
            title = "🤖 <b>AI review</b>"
            if proposal and proposal.actions:
                token = secrets.token_hex(6)
                self._proposals[token] = proposal
                from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text=f"✅ Apply {len(proposal.actions)} change(s)", callback_data=f"ai:y:{token}"),
                    InlineKeyboardButton(text="❌ Ignore", callback_data=f"ai:n:{token}"),
                ]])
                lines = "\n".join(f"• {esc(_describe(n, a))}" for n, a in proposal.actions)
                await self.notify.send(f"{title}\n{esc(text)}\n\n<b>Proposed:</b>\n{lines}", reply_markup=kb)
            else:
                await self.notify.send(f"{title}\n{esc(text)}")

    async def apply_proposal(self, token: str, approve: bool) -> str:
        prop = self._proposals.pop(token, None)
        if prop is None or time.time() - prop.created > 24 * 3600:
            return "This proposal expired."
        if not approve:
            return "Ignored."
        done = []
        for name, args in prop.actions:
            try:
                res = await self._run_tool(name, args, "manage", None)
                done.append(f"✅ {_describe(name, args)}" + (f" ({res.get('note')})" if res.get("note") else ""))
            except Exception as e:  # noqa: BLE001
                done.append(f"❌ {_describe(name, args)}: {e}")
        await self.notify.send("Applied AI changes:\n" + esc("\n".join(done)))
        return "Applied."

    # ------------------------------------------------------------------ #
    # 3. Chat
    # ------------------------------------------------------------------ #
    async def chat(self, text: str) -> str:
        async with self._chat_lock:
            now = time.time()
            # Start fresh instead of trimming: history is append-only within a conversation.
            if now - self._chat_last > CHAT_IDLE_RESET_S or len(self._chat) > CHAT_MAX_MESSAGES:
                self._chat = []
            self._chat_last = now
            self._chat.append({"role": "user", "content": text})
            try:
                return await self._agent(SYSTEM, self._chat, READ_TOOLS + WRITE_TOOLS + CHAT_ONLY_TOOLS,
                                         "medium", "chat")
            except Exception:
                self._chat = []
                raise

    def reset_chat(self) -> None:
        self._chat = []

    # ------------------------------------------------------------------ #
    # Tools
    # ------------------------------------------------------------------ #
    async def _run_tool(self, name: str, args: dict, ctx: str, proposal: Proposal | None) -> dict:
        reads = {"get_overview": self._t_overview, "get_leaders": self._t_leaders,
                 "get_positions": self._t_positions, "get_recent_trades": self._t_trades,
                 "get_token_info": self._t_token}
        if name in reads:
            return await reads[name](**args)
        writes = {"update_leader": self._t_update_leader, "update_settings": self._t_update_settings,
                  "pause_copying": self._t_pause, "resume_copying": self._t_resume, "add_leader": self._t_add_leader}
        if name not in writes:
            raise ValueError(f"unknown tool {name}")
        if name in ("resume_copying", "add_leader") and ctx != "chat":
            raise PermissionError("only available when the owner asks in chat")
        if ctx == "advise" and proposal is not None and name != "pause_copying":
            await self._validate(name, args)
            proposal.actions.append((name, args))
            return {"ok": True, "note": "recorded as a proposal for the owner"}
        return await writes[name](**args)

    async def _validate(self, name: str, args: dict) -> None:
        if name == "update_leader":
            async with self.db.session() as s:
                if await _find_leader(s, args["leader"]) is None:
                    raise ValueError("leader not found")

    async def _t_overview(self) -> dict:
        cfg = await self.db.get_config()
        try:
            sol = await self.engine.wallet.sol_balance()
            portfolio = await self.engine.portfolio_sol()
        except Exception as e:  # noqa: BLE001
            sol = portfolio = f"unavailable: {e}"
        keys = ["mode", "paused", "sizing_mode", "sizing_value", "min_trade_sol", "max_trade_sol", "slippage_bps",
                "max_price_impact_pct", "tp_pct", "sl_pct", "daily_loss_limit_pct", "copy_sells", "ai_autonomy",
                "ai_max_trade_sol"]
        return {"settings": {k: getattr(cfg, k) for k in keys}, "sol_balance": sol, "portfolio_sol": portfolio,
                "counters": self.engine.stats}

    async def _t_leaders(self) -> dict:
        async with self.db.session() as s:
            rows = list((await s.execute(select(Leader).order_by(Leader.id))).scalars().all())
        return {"leaders": [_leader_dict(ld) for ld in rows]}

    async def _t_positions(self) -> dict:
        async with self.db.session() as s:
            rows = list((await s.execute(select(Position).where(Position.qty > 0))).scalars().all())
            labels = {ld.address: ld.label for ld in (await s.execute(select(Leader))).scalars().all()}
        if not rows:
            return {"positions": []}
        prices = await self.engine.jup.prices_usd([WSOL_MINT] + [p.token_mint for p in rows])
        sol_usd = prices.get(WSOL_MINT, 0.0)
        out = []
        for p in rows:
            value = p.qty * prices.get(p.token_mint, 0.0) / sol_usd if sol_usd else None
            out.append({"mint": p.token_mint, "qty": p.qty, "cost_sol": round(p.cost_basis_sol, 4),
                        "value_sol": round(value, 4) if value is not None else None,
                        "unrealized_pct": round((value / p.cost_basis_sol - 1) * 100, 1)
                        if value and p.cost_basis_sol else None,
                        "opened_by": labels.get(p.leader) or p.leader, "opened_at": p.opened_at})
        return {"positions": out}

    async def _t_trades(self, limit: int = 20, leader: str | None = None) -> dict:
        limit = max(1, min(50, int(limit)))
        async with self.db.session() as s:
            q = select(Trade).order_by(Trade.id.desc()).limit(limit)
            if leader:
                ld = await _find_leader(s, leader)
                if ld:
                    q = q.where(Trade.leader == ld.address)
            rows = list((await s.execute(q)).scalars().all())
        return {"trades": [{"at": t.created_at, "leader": t.leader, "side": t.side, "mint": t.token_mint,
                            "leader_sol": round(t.leader_sol, 4), "status": t.status, "origin": t.origin,
                            "our_sol": round(t.our_sol, 4), "note": t.note[:120]} for t in rows]}

    async def _t_token(self, mint: str) -> dict:
        return _trim(await self.engine.jup.token_info(mint)) or {"error": "no data"}

    async def _t_update_leader(self, leader: str, weight_pct=None, active=None, mode=None, max_sol=None,
                               note=None) -> dict:
        async with self.db.session() as s:
            ld = await _find_leader(s, leader)
            if ld is None:
                raise ValueError("leader not found")
            if weight_pct is not None:
                ld.weight_pct = max(0.0, min(200.0, float(weight_pct)))
            if active is not None:
                ld.is_active = bool(active)
            if mode is not None:
                ld.mode = "" if mode == "default" else mode
            if max_sol is not None:
                ld.max_sol = max(0.0, float(max_sol))
            if note:
                ld.ai_note = str(note)[:300]
            result = _leader_dict(ld)
        if active is not None and self.on_leaders_changed:
            await self.on_leaders_changed()
        return {"ok": True, "leader": result}

    async def _t_update_settings(self, sizing_mode=None, sizing_value=None, min_trade_sol=None, max_trade_sol=None,
                                 slippage_pct=None, tp_pct=None, sl_pct=None, daily_loss_limit_pct=None,
                                 copy_sells=None) -> dict:
        cfg = await self.db.get_config()
        ceiling = cfg.ai_max_trade_sol
        upd: dict = {}
        notes = []
        if max_trade_sol is not None:
            v = max(0.001, float(max_trade_sol))
            if v > ceiling:
                notes.append(f"max_trade_sol limited to the owner's AI ceiling {ceiling:g}")
                v = ceiling
            upd["max_trade_sol"] = v
        if min_trade_sol is not None:
            upd["min_trade_sol"] = max(0.001, float(min_trade_sol))
        if sizing_mode is not None:
            upd["sizing_mode"] = sizing_mode
        if sizing_value is not None:
            mode = upd.get("sizing_mode", cfg.sizing_mode)
            v = float(sizing_value)
            limits = {"fixed": ceiling, "percent": 50.0, "mirror": 300.0}
            if v <= 0:
                raise ValueError("sizing_value must be positive")
            if v > limits[mode]:
                notes.append(f"sizing_value limited to {limits[mode]:g} for {mode}")
                v = limits[mode]
            upd["sizing_value"] = v
        if slippage_pct is not None:
            upd["slippage_bps"] = int(round(max(0.5, min(30.0, float(slippage_pct))) * 100))
        if tp_pct is not None:
            upd["tp_pct"] = max(0.0, float(tp_pct))
        if sl_pct is not None:
            upd["sl_pct"] = max(0.0, min(95.0, float(sl_pct)))
        if daily_loss_limit_pct is not None:
            upd["daily_loss_limit_pct"] = max(1.0, min(50.0, float(daily_loss_limit_pct)))
        if copy_sells is not None:
            upd["copy_sells"] = bool(copy_sells)
        lo = upd.get("min_trade_sol", cfg.min_trade_sol)
        hi = upd.get("max_trade_sol", cfg.max_trade_sol)
        if lo > hi:
            raise ValueError(f"min_trade_sol {lo:g} would exceed max_trade_sol {hi:g}")
        if upd:
            await self.db.update_config(**upd)
        return {"ok": True, "changed": upd, "note": "; ".join(notes)}

    async def _t_pause(self, reason: str) -> dict:
        await self.db.update_config(paused=True)
        await self.notify.send(f"⏸ <b>AI paused copying</b>: {esc(reason)}\n/resume to continue.")
        return {"ok": True}

    async def _t_resume(self) -> dict:
        await self.db.update_config(paused=False)
        return {"ok": True}

    async def _t_add_leader(self, address: str, label: str) -> dict:
        from bot.wallet import is_valid_pubkey
        if not is_valid_pubkey(address):
            raise ValueError("invalid address")
        async with self.db.session() as s:
            ld = (await s.execute(select(Leader).where(Leader.address == address))).scalar_one_or_none()
            if ld:
                ld.is_active = True
                ld.label = label[:64] or ld.label
            else:
                s.add(Leader(address=address, label=label[:64]))
        if self.on_leaders_changed:
            await self.on_leaders_changed()
        return {"ok": True}

    on_leaders_changed = None

    async def _exposure(self) -> dict:
        async with self.db.session() as s:
            rows = list((await s.execute(select(Position).where(Position.qty > 0))).scalars().all())
            since = utcnow() - timedelta(hours=24)
            recent = list((await s.execute(select(Trade).where(Trade.created_at >= since,
                                                                Trade.status == "executed"))).scalars().all())
        return {"open_positions": len(rows), "open_cost_sol": round(sum(p.cost_basis_sol for p in rows), 4),
                "buys_24h": sum(1 for t in recent if t.side == "buy")}


# ---------------------------------------------------------------------- #
async def _find_leader(s, ref: str) -> Leader | None:
    res = await s.execute(select(Leader).where((Leader.address == ref) | (Leader.label == ref)))
    return res.scalars().first()


def _leader_dict(ld: Leader) -> dict:
    closed = ld.wins + ld.losses
    return {"address": ld.address, "label": ld.label, "active": ld.is_active, "weight_pct": ld.weight_pct,
            "mode": ld.mode or "default", "max_sol": ld.max_sol, "trades_seen": ld.trades_seen,
            "buys": ld.buys, "sells": ld.sells, "copied_sol": round(ld.copied_sol, 4),
            "realized_pnl_sol": round(ld.realized_pnl_sol, 4),
            "win_rate": round(ld.wins / closed, 2) if closed else None, "closed_sells": closed,
            "last_trade_at": ld.last_trade_at, "ai_note": ld.ai_note}


_TOKEN_KEYS = ("name", "symbol", "decimals", "liquidity", "mcap", "fdv", "usdPrice", "holderCount",
               "organicScore", "organicScoreLabel", "isVerified", "audit", "firstPool", "createdAt", "tags",
               "stats5m", "stats1h", "stats24h", "launchpad")


def _trim(info: dict) -> dict:
    return {k: info[k] for k in _TOKEN_KEYS if k in info} if info else {}


def _describe(name: str, args: dict) -> str:
    changed = ", ".join(f"{k}={v}" for k, v in args.items() if v is not None and k not in ("leader", "note"))
    if name == "update_leader":
        return f"{args.get('leader')}: {changed}" + (f" ({args['note']})" if args.get("note") else "")
    if name == "update_settings":
        return f"settings: {changed}"
    return f"{name} {changed}".strip()
