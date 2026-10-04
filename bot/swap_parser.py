"""DEX-agnostic swap detection from balance changes.

Instead of relying on per-DEX instruction decoding (or Helius `events.swap`,
which is missing for many pump.fun / new-AMM trades) we diff the wallet's
balances before and after the transaction:

* SOL side  = native lamport change (+ fee added back if wallet paid it,
              + rent of token accounts it opened) + wrapped-SOL change
* tokens    = pre/post token balances whose `owner` is the wallet

One token up + SOL/stable down  -> BUY
One token down + SOL/stable up  -> SELL
One token down + another up     -> token-to-token (emitted as SELL + BUY)

Works on the `getTransaction` (jsonParsed) shape and on Helius *raw* webhook
payloads, which share it. The same parser measures our own copy trades.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from bot.config import LAMPORTS_PER_SOL, STABLE_MINTS, WSOL_MINT

TOKEN_ACCOUNT_RENT_LAMPORTS = 2_039_280
MIN_SOL_LEG = 0.0001  # ignore SOL movements smaller than this (fee noise)


@dataclass
class ParsedSwap:
    wallet: str
    signature: str
    side: str  # "buy" | "sell"
    token_mint: str
    token_decimals: int
    token_amount: float  # absolute ui amount bought / sold
    token_amount_raw: int
    quote_mint: str  # WSOL_MINT, a stable mint, or another token (token-to-token)
    quote_amount: float  # absolute ui amount of the quote leg
    sol_amount: float  # SOL value of the trade if the quote was SOL, else 0 (valued later)
    wallet_pre_sol: float  # wallet SOL balance before the tx (for mirror sizing)
    pre_token: float  # wallet balance of token_mint before the tx (ui)
    post_token: float  # ... and after
    block_time: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def sell_fraction(self) -> float:
        """Fraction of the wallet's position sold (sells only)."""
        if self.side != "sell" or self.pre_token <= 0:
            return 0.0
        return max(0.0, min(1.0, (self.pre_token - self.post_token) / self.pre_token))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ParsedSwap":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _account_keys(tx: dict) -> tuple[list[str], set[str]]:
    """Return (all account keys in index order, signer keys)."""
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = msg.get("accountKeys") or []
    if keys and isinstance(keys[0], dict):  # jsonParsed: includes lookup-table keys already
        return [k["pubkey"] for k in keys], {k["pubkey"] for k in keys if k.get("signer")}
    keys = list(keys)
    n_sig = (msg.get("header") or {}).get("numRequiredSignatures", 1)
    signers = set(keys[:n_sig])
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    keys += list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])
    return keys, signers


def _token_balances(entries: list[dict], owner: str) -> dict[str, tuple[int, int, int]]:
    """mint -> (raw_amount, decimals, account_count) for token accounts owned by `owner`."""
    out: dict[str, tuple[int, int, int]] = {}
    for e in entries or []:
        if e.get("owner") != owner:
            continue
        ui = e.get("uiTokenAmount") or {}
        raw = int(ui.get("amount") or 0)
        dec = int(ui.get("decimals") or 0)
        prev = out.get(e["mint"], (0, dec, 0))
        out[e["mint"]] = (prev[0] + raw, dec, prev[2] + 1)
    return out


def signature_of(tx: dict) -> str:
    sigs = (tx.get("transaction") or {}).get("signatures") or []
    return sigs[0] if sigs else ""


def parse_swaps(tx: dict, wallet: str) -> list[ParsedSwap]:
    """Detect swaps performed by `wallet` in a confirmed transaction."""
    if not tx:
        return []
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return []
    keys, signers = _account_keys(tx)
    if wallet not in keys or wallet not in signers:
        return []  # wallet did not sign -> it was not their trade
    idx = keys.index(wallet)
    pre_bal, post_bal = meta.get("preBalances") or [], meta.get("postBalances") or []
    if idx >= len(pre_bal) or idx >= len(post_bal):
        return []

    native_delta = post_bal[idx] - pre_bal[idx]
    if idx == 0:
        native_delta += int(meta.get("fee") or 0)

    pre_tok = _token_balances(meta.get("preTokenBalances"), wallet)
    post_tok = _token_balances(meta.get("postTokenBalances"), wallet)

    # Rent for token accounts opened in this tx isn't trade value; add it back.
    opened = sum(max(0, post_tok[m][2] - pre_tok.get(m, (0, 0, 0))[2]) for m in post_tok if m != WSOL_MINT)
    closed = sum(max(0, pre_tok[m][2] - post_tok.get(m, (0, 0, 0))[2]) for m in pre_tok if m != WSOL_MINT)
    native_delta += (opened - closed) * TOKEN_ACCOUNT_RENT_LAMPORTS

    deltas: dict[str, tuple[int, int]] = {}
    for mint in set(pre_tok) | set(post_tok):
        a = pre_tok.get(mint, (0, 0, 0))
        b = post_tok.get(mint, (0, 0, 0))
        dec = b[1] if mint in post_tok else a[1]
        if b[0] != a[0]:
            deltas[mint] = (b[0] - a[0], dec)

    wsol_raw = deltas.pop(WSOL_MINT, (0, 9))[0]
    sol_delta = (native_delta + wsol_raw) / LAMPORTS_PER_SOL
    stable = {m: deltas.pop(m) for m in list(deltas) if m in STABLE_MINTS}
    ups = {m: d for m, d in deltas.items() if d[0] > 0}
    downs = {m: d for m, d in deltas.items() if d[0] < 0}

    pre_sol = pre_bal[idx] / LAMPORTS_PER_SOL
    if WSOL_MINT in pre_tok:
        pre_sol += pre_tok[WSOL_MINT][0] / LAMPORTS_PER_SOL
    sig = signature_of(tx)
    block_time = int(tx.get("blockTime") or 0)

    def ui(mint: str, raw: int) -> float:
        dec = deltas[mint][1]
        return raw / 10**dec

    def bal(src: dict, mint: str) -> float:
        raw, dec, _ = src.get(mint, (0, deltas[mint][1], 0))
        return raw / 10**dec

    def make(side: str, mint: str, quote_mint: str, quote_amount: float, sol_amount: float) -> ParsedSwap:
        raw = abs(deltas[mint][0])
        return ParsedSwap(
            wallet=wallet, signature=sig, side=side, token_mint=mint, token_decimals=deltas[mint][1],
            token_amount=ui(mint, raw), token_amount_raw=raw, quote_mint=quote_mint,
            quote_amount=quote_amount, sol_amount=sol_amount, wallet_pre_sol=pre_sol,
            pre_token=bal(pre_tok, mint), post_token=bal(post_tok, mint), block_time=block_time,
        )

    def quote_leg(sign: int) -> tuple[str, float, float] | None:
        """Pick the SOL/stable leg moving in direction `sign` (-1 spent, +1 received)."""
        if sign * sol_delta >= MIN_SOL_LEG:
            return WSOL_MINT, abs(sol_delta), abs(sol_delta)
        for m, (raw, dec) in stable.items():
            if sign * raw > 0:
                return m, abs(raw) / 10**dec, 0.0
        return None

    if len(ups) == 1 and not downs:
        mint = next(iter(ups))
        q = quote_leg(-1)
        return [make("buy", mint, *q)] if q else []
    if len(downs) == 1 and not ups:
        mint = next(iter(downs))
        q = quote_leg(+1)
        return [make("sell", mint, *q)] if q else []
    if len(ups) == 1 and len(downs) == 1:
        up, down = next(iter(ups)), next(iter(downs))
        down_amt = ui(down, abs(deltas[down][0]))
        up_amt = ui(up, abs(deltas[up][0]))
        return [make("sell", down, up, up_amt, 0.0), make("buy", up, down, down_amt, 0.0)]
    return []
