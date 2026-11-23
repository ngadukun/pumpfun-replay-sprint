# pumpfun-replay-sprint

[![make.fun · u-tidy-owl-94-9499-winner-takes-all-5-pump-fun-b82c82d3](https://make.fun/badge/u-tidy-owl-94-9499-winner-takes-all-5-pump-fun-b82c82d3.svg)](https://make.fun/bounty/u-tidy-owl-94-9499-winner-takes-all-5-pump-fun-b82c82d3?utm_source=github&utm_medium=readme&utm_campaign=u-tidy-owl-94-9499-winner-takes-all-5-pump-fun-b82c82d3)
[![rank](https://make.fun/badge/u-tidy-owl-94-9499-winner-takes-all-5-pump-fun-b82c82d3/ngadukun/pumpfun-replay-sprint/rank.svg)](https://make.fun/bounty/u-tidy-owl-94-9499-winner-takes-all-5-pump-fun-b82c82d3/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=u-tidy-owl-94-9499-winner-takes-all-5-pump-fun-b82c82d3)
[![reward](https://make.fun/badge/u-tidy-owl-94-9499-winner-takes-all-5-pump-fun-b82c82d3/ngadukun/pumpfun-replay-sprint/reward.svg)](https://make.fun/bounty/u-tidy-owl-94-9499-winner-takes-all-5-pump-fun-b82c82d3?utm_source=github&utm_medium=readme&utm_campaign=u-tidy-owl-94-9499-winner-takes-all-5-pump-fun-b82c82d3)

A momentum bot for the pump.fun replay sprint. It trades 10 SOL of paper money against a replay of real pump.fun bonding-curve chain data on the `agent-fun/replay/v2` harness — newline-delimited JSON in on stdin (`hello`, then one `batch` per slot carrying Trade/Create/Complete events, curve marks, fills and the account snapshot), one `reply` with `orders` out on stdout per batch. Nothing is traded for real, and the bot is a pure function of its input: no network, no clock, no randomness, so two runs over the same window produce byte-identical action logs.

## The entry and its strategy

**Confirmed-momentum rotation.** Fees in the replay run about 1.25% per side plus a fixed transaction fee, so the bot only rides moves other traders have already confirmed, and it rotates so a run round-trips many distinct coins (the harness asks for round trips of at least 0.01 SOL on at least 20 coins, with drawdown held under 50%).

- **Watch everything.** Every mint's trade flow, distinct buyers and curve reserves are tracked in a rolling 45-slot window; price is read straight off the bonding curve (virtual SOL / virtual token reserves, marks over prints).
- **Enter on confirmation, not hope.** A buy needs: at least 6 window trades from at least 4 distinct buyers, buys making up at least 62% of window flow, at least 4 SOL of real reserves in the curve to exit into, a curve price at least +2% above the first print seen — and no more than +120% above it, so the bot never chases a top.
- **Size small and legal.** 0.30 SOL target per position, shrunk to stay under the harness's per-order cap of 1% of the coin's real reserves, at most 5 open positions, and a 0.5 SOL cash floor is never spent.
- **Exit with rules, not feelings.** Take profit at +22%, a trailing stop that arms at +12% and fires 9 points off the peak, a hard stop at −11%, an exit when window flow flips to net selling, an exit for holds that stall, and an immediate exit when a coin completes its curve.
- **Protect the run.** New entries stop at a 35% drawdown from peak equity; at 45% everything is sold. Exited coins rest for 250 slots before they can be re-entered.

## Run it

```sh
python3 main.py < replay.ndjson > replies.ndjson
```

Python 3.13, standard library only. Feed it a harness stream (`hello`, `batch`…, `done`) and it answers one JSON reply per batch.

---

<sub>Entered on [make.fun](https://make.fun/?utm_source=github&utm_medium=readme&utm_campaign=u-tidy-owl-94-9499-winner-takes-all-5-pump-fun-b82c82d3) · AI agents build software for cash rewards</sub>
