import pytest

from bot.config import USDC_MINT, WSOL_MINT
from bot.swap_parser import parse_swaps
from tests.helpers import LEADER, OTHER, POOL, TOKEN, TOKEN2, buy_tx, make_tx, sell_tx, tb


def test_buy_with_new_token_account_excludes_rent_and_fee():
    [s] = parse_swaps(buy_tx(sol_spent=1.0, tokens_raw=5_000_000_000), LEADER)
    assert s.side == "buy"
    assert s.token_mint == TOKEN
    assert s.quote_mint == WSOL_MINT
    assert s.sol_amount == pytest.approx(1.0, abs=1e-6)
    assert s.token_amount == pytest.approx(5000.0)
    assert s.wallet_pre_sol == pytest.approx(10.0)
    assert s.pre_token == 0 and s.post_token == pytest.approx(5000.0)


def test_partial_sell_fraction():
    [s] = parse_swaps(sell_tx(pre_raw=4_000_000, sold_raw=1_000_000, sol_got=0.5), LEADER)
    assert s.side == "sell"
    assert s.sol_amount == pytest.approx(0.5, abs=1e-6)
    assert s.sell_fraction == pytest.approx(0.25)


def test_full_sell_closing_account_subtracts_rent_refund():
    tx = make_tx(pre_sol=1.0, post_sol=1.0 + 0.3 + 0.00203928,
                 pre_tokens=[tb(2, TOKEN, LEADER, 1_000_000)], post_tokens=[])
    [s] = parse_swaps(tx, LEADER)
    assert s.side == "sell"
    assert s.sol_amount == pytest.approx(0.3, abs=1e-6)
    assert s.sell_fraction == 1.0


def test_wsol_leg_counts_as_sol():
    tx = make_tx(pre_sol=1.0, post_sol=1.0,
                 pre_tokens=[tb(1, WSOL_MINT, LEADER, 2_000_000_000, 9), tb(2, TOKEN, LEADER, 0)],
                 post_tokens=[tb(1, WSOL_MINT, LEADER, 1_500_000_000, 9), tb(2, TOKEN, LEADER, 7_000_000)])
    [s] = parse_swaps(tx, LEADER)
    assert s.side == "buy" and s.sol_amount == pytest.approx(0.5)
    assert s.wallet_pre_sol == pytest.approx(3.0)


def test_usdc_quote_buy():
    tx = make_tx(pre_sol=1.0, post_sol=1.0,
                 pre_tokens=[tb(1, USDC_MINT, LEADER, 100_000_000), tb(2, TOKEN, LEADER, 0)],
                 post_tokens=[tb(1, USDC_MINT, LEADER, 50_000_000), tb(2, TOKEN, LEADER, 9_000_000)])
    [s] = parse_swaps(tx, LEADER)
    assert s.side == "buy" and s.quote_mint == USDC_MINT and s.quote_amount == pytest.approx(50.0)
    assert s.sol_amount == 0.0


def test_token_to_token_emits_sell_and_buy():
    tx = make_tx(pre_sol=1.0, post_sol=1.0,
                 pre_tokens=[tb(1, TOKEN, LEADER, 10_000_000), tb(2, TOKEN2, LEADER, 0)],
                 post_tokens=[tb(1, TOKEN, LEADER, 0), tb(2, TOKEN2, LEADER, 3_000_000)])
    swaps = parse_swaps(tx, LEADER)
    assert [(s.side, s.token_mint, s.quote_mint) for s in swaps] == [("sell", TOKEN, TOKEN2), ("buy", TOKEN2, TOKEN)]


def test_failed_tx_ignored():
    assert parse_swaps(buy_tx() | {"meta": {**buy_tx()["meta"], "err": {"InstructionError": [0, "x"]}}}, LEADER) == []


def test_non_signer_ignored():
    # LEADER receives tokens in a tx signed by someone else -> not their trade
    tx = make_tx(signer=OTHER, extra_keys=(LEADER,), pre_tokens=[], post_tokens=[tb(2, TOKEN, LEADER, 5)])
    assert parse_swaps(tx, LEADER) == []


def test_plain_transfer_is_not_a_swap():
    tx = make_tx(pre_sol=1.0, post_sol=1.0, pre_tokens=[tb(2, TOKEN, LEADER, 10)], post_tokens=[tb(2, TOKEN, LEADER, 5)])
    assert parse_swaps(tx, LEADER) == []


def test_raw_format_with_string_keys():
    tx = buy_tx()
    tx["transaction"]["message"]["accountKeys"] = [LEADER, POOL]
    [s] = parse_swaps(tx, LEADER)
    assert s.side == "buy"


def test_roundtrip_dict():
    from bot.swap_parser import ParsedSwap
    [s] = parse_swaps(buy_tx(), LEADER)
    assert ParsedSwap.from_dict(s.to_dict()) == s
