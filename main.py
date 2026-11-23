#!/usr/bin/env python3
"""Pump.fun replay momentum bot for the agent-fun/replay/v2 harness.

Protocol (newline-delimited JSON):
  harness -> bot on stdin : hello, then batch messages, then done
  bot -> harness on stdout: exactly one reply per batch, echoing its seq,
                            carrying the orders chosen at that slot

Design notes
------------
* Paper trading only. The bot never touches a wallet, the network, the
  filesystem or the clock; it is a pure function of the lines it reads,
  so two runs over the same replay produce byte-identical replies.
* Prices come from the bonding curve itself: a mint's spot price is
  virtual_sol_reserves / virtual_token_reserves, and a position is marked
  the same way from the latest reserves the batch carried (marks win over
  trade prints, because marks are the curve state at the batch slot).
* Every amount is an integer number of lamports / token base units.
  Ratios are compared by cross-multiplication wherever a threshold is a
  hard rule, so nothing depends on float rounding.
* Fees are real in this harness (about 1.25% per side plus a fixed
  transaction fee), so the strategy only rides moves that are already
  confirmed by other buyers and cuts anything that stalls.

Strategy in one paragraph: watch every new coin's trade flow; when a coin
shows sustained net buying from several distinct wallets, a rising curve
price and enough real SOL in the curve to exit into, buy a small fixed
slice (never more than the harness's per-order reserve cap). Sell on a
take-profit, a trailing stop once a run is underway, a hard stop-loss, a
flow reversal, a stale hold, or the coin completing its curve. Rotate out
and into fresh coins so the run round-trips many distinct mints.
"""

import json
import sys
from collections import deque

# ---------------------------------------------------------------- tunables

LAMPORTS_PER_SOL = 1_000_000_000

# Entry sizing: target slice per position, before caps are applied.
TARGET_BUY_LAMPORTS = 300_000_000          # 0.30 SOL
MIN_BUY_LAMPORTS = 20_000_000              # 0.02 SOL (round trips need >= 0.01)
CASH_FLOOR_LAMPORTS = 500_000_000          # never spend the last 0.5 SOL

# Entry confirmation window, in slots, and its thresholds.
FLOW_WINDOW_SLOTS = 45
MIN_TRADES_IN_WINDOW = 6
MIN_DISTINCT_BUYERS = 4
MIN_BUY_SHARE_BPS = 6_200                  # >= 62% of window flow is buys
MIN_REAL_RESERVE_LAMPORTS = 4_000_000_000  # curve holds >= 4 SOL real
MAX_EXTENSION_BPS = 12_000                 # price <= 2.2x first print (no tops)
MIN_AGE_SLOTS = 12                         # let the first blocks settle
MIN_RISE_BPS = 200                         # price >= +2% vs first print
FORGET_AFTER_SLOTS = 20_000                # drop dead mints from memory

# Exits, in basis points of mark-to-market gain versus position cost.
TAKE_PROFIT_BPS = 2_200                    # +22% : bank it
TRAIL_ARM_BPS = 1_200                      # trailing stop arms at +12%
TRAIL_GIVEBACK_BPS = 900                   # ... and fires 9 points off peak
STOP_LOSS_BPS = -1_100                     # -11% : out, no debate
STALL_SLOTS = 700                          # held this long with < +3% : out
STALL_MAX_GAIN_BPS = 300
REVERSAL_MIN_NET_SELL_BPS = 3_500          # window flow flipped to selling

# Portfolio brakes.
DRAWDOWN_HALT_BPS = 3_500                  # at -35% from peak: stop entering
DRAWDOWN_FLATTEN_BPS = 4_500               # at -45% from peak: sell everything
REENTRY_COOLDOWN_SLOTS = 250               # rest a mint after exiting it

BUY_SLIPPAGE_BPS = 500
SELL_SLIPPAGE_BPS = 900

# ------------------------------------------------------------------- state


class MintState:
    """Rolling view of one coin: curve state plus its recent trade flow."""

    __slots__ = (
        "mint", "created_slot", "complete", "v_sol", "v_tok",
        "real_sol", "first_price_num", "first_price_den",
        "flow", "buyers", "round_tripped",
    )

    def __init__(self, mint, slot):
        self.mint = mint
        self.created_slot = slot
        self.complete = False
        self.v_sol = 0
        self.v_tok = 0
        self.real_sol = 0
        self.first_price_num = 0   # price as the fraction v_sol / v_tok
        self.first_price_den = 1
        self.flow = deque()        # (slot, is_buy, sol_amount, user)
        self.buyers = {}           # user -> last buy slot inside the window
        self.round_tripped = False

    def note_reserves(self, v_sol, v_tok, real_sol):
        if v_tok > 0:
            if self.first_price_num == 0:
                self.first_price_num = v_sol
                self.first_price_den = v_tok
            self.v_sol = v_sol
            self.v_tok = v_tok
        if real_sol >= 0:
            self.real_sol = real_sol

    def note_trade(self, slot, is_buy, sol_amount, user):
        self.flow.append((slot, is_buy, sol_amount, user))
        if is_buy:
            self.buyers[user] = slot

    def prune(self, slot):
        horizon = slot - FLOW_WINDOW_SLOTS
        while self.flow and self.flow[0][0] < horizon:
            self.flow.popleft()
        stale = [u for u, s in self.buyers.items() if s < horizon]
        for user in stale:
            del self.buyers[user]

    def window_stats(self):
        """(buy_lamports, sell_lamports, trade_count) inside the window."""
        buy = 0
        sell = 0
        for _slot, is_buy, amount, _user in self.flow:
            if is_buy:
                buy += amount
            else:
                sell += amount
        return buy, sell, len(self.flow)

    def price(self):
        """Spot price as a fraction (num, den); (0, 1) while unknown."""
        if self.v_tok > 0:
            return self.v_sol, self.v_tok
        return 0, 1


class Position:
    """A holding, synced from the harness account snapshot each batch."""

    __slots__ = ("mint", "tokens", "cost", "opened_slot", "peak_gain_bps")

    def __init__(self, mint, tokens, cost, opened_slot):
        self.mint = mint
        self.tokens = tokens
        self.cost = cost
        self.opened_slot = opened_slot
        self.peak_gain_bps = 0


class Bot:
    def __init__(self):
        self.capital = 0
        self.max_open = 5
        self.max_order_bps = 100
        self.max_orders_per_batch = 16
        self.mints = {}
        self.positions = {}
        self.pending_buys = set()    # mints with a buy order in flight
        self.pending_sells = set()   # mints with a sell order in flight
        self.cooldown_until = {}     # mint -> slot when it may be re-entered
        self.order_counter = 0
        self.cash = 0
        self.peak_equity = 0
        self.tripped_mints = set()   # distinct mints fully round-tripped

    # ------------------------------------------------------------- helpers

    def _client_id(self, tag):
        self.order_counter += 1
        return "%s-%d" % (tag, self.order_counter)

    def mint_state(self, mint, slot):
        state = self.mints.get(mint)
        if state is None:
            state = MintState(mint, slot)
            self.mints[mint] = state
        return state

    def position_value(self, pos):
        """Mark-to-market lamports of a position at the curve's spot."""
        state = self.mints.get(pos.mint)
        if state is None or state.v_tok <= 0:
            return pos.cost
        return pos.tokens * state.v_sol // state.v_tok

    def gain_bps(self, pos):
        if pos.cost <= 0:
            return 0
        value = self.position_value(pos)
        return (value - pos.cost) * 10_000 // pos.cost

    def equity(self):
        total = self.cash
        for pos in self.positions.values():
            total += self.position_value(pos)
        return total

    # ------------------------------------------------------------ protocol

    def on_hello(self, msg):
        self.capital = int(msg["capital"]["lamports"])
        self.cash = self.capital
        self.peak_equity = self.capital
        caps = msg["caps"]
        self.max_open = int(caps["max_open_positions"])
        self.max_order_bps = int(caps["max_order_bps_of_real_reserve"])
        self.max_orders_per_batch = int(caps["max_orders_per_batch"])

    def on_fills(self, fills):
        for fill in fills:
            mint = fill["mint"]
            if fill["side"] == "buy":
                self.pending_buys.discard(mint)
            else:
                self.pending_sells.discard(mint)
                if fill["status"] == "filled":
                    state = self.mints.get(mint)
                    if state is not None:
                        state.round_tripped = True
                    self.tripped_mints.add(mint)

    def on_events(self, events, slot):
        for event in events:
            kind = event["event"]
            mint = event["mint"]
            state = self.mint_state(mint, slot)
            if kind == "CreateEvent":
                # A fresh curve holds no real SOL yet; trades and marks
                # report the real reserves as they actually build up.
                state.note_reserves(
                    int(event["virtual_sol_reserves"]),
                    int(event["virtual_token_reserves"]),
                    0,
                )
            elif kind == "TradeEvent":
                state.note_reserves(
                    int(event["virtual_sol_reserves"]),
                    int(event["virtual_token_reserves"]),
                    int(event["real_sol_reserves"]),
                )
                state.note_trade(
                    slot,
                    bool(event["is_buy"]),
                    int(event["sol_amount"]),
                    event["user"],
                )
            elif kind == "CompleteEvent":
                state.complete = True

    def on_marks(self, marks):
        for mark in marks:
            state = self.mint_state(mark["mint"], mark["slot"])
            state.note_reserves(
                int(mark["virtual_quote_reserves"]),
                int(mark["virtual_token_reserves"]),
                int(mark["real_quote_reserves"]),
            )

    def on_account(self, account, slot):
        self.cash = int(account["cash_lamports"])
        seen = set()
        for entry in account["positions"]:
            mint = entry["mint"]
            tokens = int(entry["token_amount"])
            cost = int(entry["cost_lamports"])
            seen.add(mint)
            pos = self.positions.get(mint)
            if pos is None:
                self.positions[mint] = Position(mint, tokens, cost, slot)
            else:
                pos.tokens = tokens
                pos.cost = cost
        for mint in list(self.positions):
            if mint not in seen:
                del self.positions[mint]
                self.cooldown_until[mint] = slot + REENTRY_COOLDOWN_SLOTS
        equity = self.equity()
        if equity > self.peak_equity:
            self.peak_equity = equity

    # ------------------------------------------------------------ strategy

    def drawdown_bps(self):
        if self.peak_equity <= 0:
            return 0
        return (self.peak_equity - self.equity()) * 10_000 // self.peak_equity

    def exit_reason(self, pos, slot):
        state = self.mints.get(pos.mint)
        if state is not None and state.complete:
            return "complete"
        gain = self.gain_bps(pos)
        if gain > pos.peak_gain_bps:
            pos.peak_gain_bps = gain
        if self.drawdown_bps() >= DRAWDOWN_FLATTEN_BPS:
            return "flatten"
        if gain <= STOP_LOSS_BPS:
            return "stop"
        if gain >= TAKE_PROFIT_BPS:
            return "target"
        if (pos.peak_gain_bps >= TRAIL_ARM_BPS
                and gain <= pos.peak_gain_bps - TRAIL_GIVEBACK_BPS):
            return "trail"
        if slot - pos.opened_slot >= STALL_SLOTS and gain < STALL_MAX_GAIN_BPS:
            return "stall"
        if state is not None:
            buy, sell, _n = state.window_stats()
            total = buy + sell
            if total > 0 and gain > STOP_LOSS_BPS // 2:
                net_sell_bps = (sell - buy) * 10_000 // total
                if net_sell_bps >= REVERSAL_MIN_NET_SELL_BPS:
                    return "reversal"
        return None

    def entry_score(self, state, slot):
        """Score a candidate mint, or None when it fails a hard filter."""
        if state.complete or state.v_tok <= 0:
            return None
        if slot - state.created_slot < MIN_AGE_SLOTS:
            return None
        if state.mint in self.positions or state.mint in self.pending_buys:
            return None
        if self.cooldown_until.get(state.mint, -1) > slot:
            return None
        if state.real_sol < MIN_REAL_RESERVE_LAMPORTS:
            return None
        buy, sell, trades = state.window_stats()
        if trades < MIN_TRADES_IN_WINDOW:
            return None
        if len(state.buyers) < MIN_DISTINCT_BUYERS:
            return None
        total = buy + sell
        if total <= 0 or buy * 10_000 < total * MIN_BUY_SHARE_BPS:
            return None
        # Momentum must be real, not just loud: the curve price has to be
        # at least MIN_RISE_BPS above the first print this bot saw, and no
        # more than MAX_EXTENSION_BPS above it (never chase a top).
        num, den = state.price()
        if state.first_price_num > 0:
            lhs = num * state.first_price_den * 10_000
            rhs_top = den * state.first_price_num * (10_000 + MAX_EXTENSION_BPS)
            if lhs > rhs_top:
                return None
            rhs_floor = den * state.first_price_num * (10_000 + MIN_RISE_BPS)
            if lhs < rhs_floor:
                return None
        # Score: net inflow share, breadth of buyers, fresh flow first.
        net_bps = (buy - sell) * 10_000 // total
        fresh_bonus = 0 if state.round_tripped else 250
        return net_bps + 40 * len(state.buyers) + fresh_bonus

    def buy_size(self, state):
        """Lamports to spend: target slice, capped by the reserve rule,
        the cash on hand and the cash floor the bot refuses to cross."""
        size = TARGET_BUY_LAMPORTS
        reserve_cap = state.real_sol * self.max_order_bps // 10_000
        reserve_cap = reserve_cap * 9 // 10  # stay clear of the cap's edge
        if reserve_cap < size:
            size = reserve_cap
        spendable = self.cash - CASH_FLOOR_LAMPORTS
        if spendable < size:
            size = spendable
        if size < MIN_BUY_LAMPORTS:
            return 0
        return size

    def decide(self, slot):
        orders = []

        # Exits first: they free slots and cash for this same batch.
        for mint in sorted(self.positions):
            if len(orders) >= self.max_orders_per_batch:
                break
            if mint in self.pending_sells:
                continue
            pos = self.positions[mint]
            if self.exit_reason(pos, slot) is None:
                continue
            orders.append({
                "side": "sell",
                "client_id": self._client_id("s"),
                "mint": mint,
                "token_amount": str(pos.tokens),
                "max_slippage_bps": SELL_SLIPPAGE_BPS,
            })
            self.pending_sells.add(mint)

        # Entries, best score first, mint name as the deterministic tiebreak.
        open_slots = (self.max_open - len(self.positions)
                      - len(self.pending_buys))
        if open_slots > 0 and self.drawdown_bps() < DRAWDOWN_HALT_BPS:
            ranked = []
            for mint in sorted(self.mints):
                state = self.mints[mint]
                state.prune(slot)
                score = self.entry_score(state, slot)
                if score is not None:
                    ranked.append((-score, mint))
            ranked.sort()
            for _neg_score, mint in ranked:
                if len(orders) >= self.max_orders_per_batch:
                    break
                if open_slots <= 0:
                    break
                state = self.mints[mint]
                size = self.buy_size(state)
                if size <= 0:
                    continue
                orders.append({
                    "side": "buy",
                    "client_id": self._client_id("b"),
                    "mint": mint,
                    "quote_lamports": str(size),
                    "max_slippage_bps": BUY_SLIPPAGE_BPS,
                })
                self.pending_buys.add(mint)
                self.cash -= size  # fills confirm later; plan conservatively
                open_slots -= 1

        return orders

    def forget_dead_mints(self, slot):
        """Bound memory on long replays: a mint with no flow for a long
        stretch, no open position and no pending order is dropped; if it
        ever trades again it is simply rediscovered as a fresh mint."""
        horizon = slot - FORGET_AFTER_SLOTS
        for mint in list(self.mints):
            if mint in self.positions or mint in self.pending_buys:
                continue
            state = self.mints[mint]
            last_seen = state.flow[-1][0] if state.flow else state.created_slot
            if last_seen < horizon:
                del self.mints[mint]

    def on_batch(self, msg):
        slot = int(msg["slot"])
        self.on_fills(msg["fills"])
        self.on_events(msg["events"], slot)
        self.on_marks(msg["marks"])
        self.on_account(msg["account"], slot)
        self.forget_dead_mints(slot)
        return {"type": "reply", "seq": msg["seq"], "orders": self.decide(slot)}


def main():
    bot = Bot()
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        kind = msg.get("type")
        if kind == "hello":
            bot.on_hello(msg)
        elif kind == "batch":
            reply = bot.on_batch(msg)
            out.write(json.dumps(reply, separators=(",", ":")) + "\n")
            out.flush()
        elif kind == "done":
            break


if __name__ == "__main__":
    main()
