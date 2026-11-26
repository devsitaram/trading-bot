# Multi-Asset Trading Research Framework

This is a fail-closed scanner, opportunity ranker, risk calculator, and local
browser dashboard. It includes a read-only MT5 quote bridge, but does **not**
include an account-data adapter or order-sending implementation. The dashboard
is served from this Mac at
`http://127.0.0.1:8765`; it is separate from the JustMarkets website. No
JustMarkets URL is treated as an API, and no login, browser storage, or
credentials are scraped. No live/demo account has been verified from this
workspace.

## Current limits

- For automated scanning, account state and the symbol universe must come from
  a separately authenticated terminal adapter implementing `ReadOnlyTerminal`.
- Instrument metadata must explicitly provide commission, swap, and a quote
  slippage estimate. Missing fee/slippage data makes a symbol ineligible.
- Execution is intentionally unavailable. A successful risk-sized proposal is
  only a proposal, never an order. Do not connect an order sender or enable live
  execution without separate review and verified demo integration.
- `main.py` starts the local dashboard. Until the MT5 quote bridge sends a
  fresh quote, the dashboard shows that the feed is disconnected and market
  data is unavailable. Account mode remains unknown because this quote bridge
  does not access account data.
- The dashboard includes a live midpoint chart that samples real bid/ask quotes
  every five seconds when a configured data feed supplies instruments. The
  chart stays empty while the adapter is unavailable; it does not generate
  sample prices or historical candles.
- For MT5 on this Mac, use the read-only quote publisher at
  `mt5/ReadOnlyQuoteBridge.mq5`. Start `python main.py`, then in MT5 open
  **File → Open Data Folder → MQL5 → Experts** and copy the EA there. Open it
  in MetaEditor and compile it. In MT5 **Tools → Options → Expert Advisors**,
  enable **Allow WebRequest for listed URL** and add exactly
  `http://127.0.0.1:8765`. Attach the EA to each symbol chart you want to
  display and enable Algo Trading so its timer can run. The EA contains no
  order-placement calls and sends quotes only to this Mac's loopback address.
  The dashboard rejects stale quotes after 15 seconds and does not receive
  account details or submit orders. The EA converts MT5 trade-server tick
  times to UTC, so keep the Mac clock synced; ignored stale quotes are logged
  in the MT5 Experts tab.
- This repository has no validated JustMarkets instrument specifications,
  trading sessions, commission/swap schedules, or historical market data. Do
  not use placeholder costs for real decisions.
- The sample trend-breakout backtest is a baseline research harness, not a
  claim of profitability. It uses a chronological 70/30 split and explicit
  next-bar open-to-close returns. Spread, estimated round-trip commission, and
  estimated financing use adapter-supplied values; financing is normalized to
  account-currency cost per lot per day and uses a configurable four-hour
  holding-time estimate. This is not historical tick/fill simulation; slippage,
  financing schedules, and realistic execution still require validation.

## Workflow

1. Authenticate using the broker's supported integration, then verify account
   ID, `DEMO` mode, currency, positions, pending orders, and recent deals.
2. For live MT5 prices on macOS, run the included read-only quote publisher.
   For account state, contract specs, incremental candles, or portfolio state,
   implement a separately reviewed authenticated `ReadOnlyTerminal` adapter.
3. Test discovery, session/quote checks, data-quality filtering, and contract
   math against terminal-sourced fixtures before connecting live market data.
4. Evaluate the strategy chronologically out of sample with all applicable
   costs; unvalidated or insufficiently positive opportunities become HOLD.
5. Review and test account-wide risk checks. The risk manager counts current
   positions and pending orders, and includes emergency stop, daily loss,
   drawdown, margin, cooldown, correlation group, and duplicate-symbol gates.
   Configure a validated correlation group for every tradable and already-open
   symbol; the risk manager rejects proposals if group coverage is missing.
6. Only after independent validation should a separately reviewed demo
   execution adapter be considered. This project has no order submission path.

Start the local dashboard from the project root:

```sh
python main.py
```

Open `http://127.0.0.1:8765` in your browser. The gold chart and paper bot are
at `http://127.0.0.1:8765/chart`: candles and the live bid come from Binance's
public XAUUSDT perpetual futures feed (gold, but not JustMarkets quotes), and
the bot only simulates the `strategy.py` trend-breakout rules on that history.
Every bot rule (entry filters, stop, target, trailing stop, daily limits) lives
in `rules.json`; the page reads it once when it opens and refuses to trade if a
rule is missing or the stop is inside the spread. Edit it and reload the page.
### Trading a JustMarkets account (demo or real)

JustMarkets has no public trading API, so the chart page's bot trades through
MetaTrader 5 with the add-on `mt5/JustMarketsBridge.mq5`. It trades whichever
account MT5 is logged in to. Every order carries its stop loss and take profit
inside MT5. The server (`demo_broker.py`) only accepts orders from the one
chart tab in control and rejects requests from other websites.

**Demo** accounts are traded as soon as they are linked. A **real (money)**
account needs two separate switches, and is refused otherwise:

- in MT5, the add-on's input `AllowRealAccount = true` (off by default);
- on the chart page, **Trade on: Real account**, confirmed by typing `REAL`.
  This resets to Demo only on every page reload, and a red **REAL MONEY**
  warning shows while it is on.

1. Install MetaTrader 5 for Mac and log in to the JustMarkets account to trade
   (e.g. demo MT5 Standard #1300066989, server JustMarkets-Demo). Type the
   password into MT5 only.
2. Start the dashboard: `python main.py`. It reports whether MetaTrader 5 is
   installed (and its version, and whether it is running) and prints the
   download link if not; the chart page's account panel shows the same.
3. In MT5: Tools → Options → Expert Advisors → tick **Allow WebRequest for
   listed URL** and add `http://127.0.0.1:8765`.
4. Open MetaEditor, open `MQL5/Experts/JustMarketsBridge.mq5` (copy it there
   from `mt5/` if missing) and press **Compile**.
5. Open an XAUUSD chart (1-minute is fine), drag JustMarketsBridge onto it
   (for a real account, set `AllowRealAccount` to true in its Inputs), and
   switch on **Algo Trading** in the toolbar.
   Shortcut for steps 3–5 after step 1's account exists in MT5: double-click
   `link_justmarkets.command`. It asks for the account, server and MT5 trading
   password (in macOS dialogs), restarts MT5 with a temporary start-up file
   (deleted after 15 s) that logs in, allows the WebRequest URL and starts
   JustMarketsBridge on XAUUSD M1, then reports whether the link worked. If MT5
   does not know JustMarkets' servers yet, first use File → Open an Account →
   search "JustMarkets" → click Just Global Markets → Cancel.
6. Open `http://127.0.0.1:8765/chart` in **one** browser tab. Until the account
   is linked, the panel lists each step (✓ done, ✗ next to do) read live from
   MT5's logs by `mt5_status.py`. The "JustMarkets
   account" panel turns green (red for real money), prices switch to
   JustMarkets, and the bot's trades, the manual BUY/SELL/Close buttons and
   dragged Stop/Target lines go to the account. Other open tabs only watch.

The news-blackout rule uses the free Forex Factory weekly calendar, fetched by
`main.py` at `/api/news` (cached for an hour, retried every 5 minutes after a
failure). Until it loads, the bot stays stopped rather than trade blind. Stop the local server with
`Ctrl+C`. This does not run inside the JustMarkets website; the terminal link
is only an external link for you to open separately.

Run the offline suite with:

```sh
python3 -m unittest discover -s tests -v
```

The tests use a mock terminal only and never contact a broker. The dashboard
itself reports no account, quote, or instrument data instead of showing
fabricated live information.

# project run
<!-- cd /Users/ing/Desktop/ai
source .venv/bin/activate
python main.py -->
# trading-bot
