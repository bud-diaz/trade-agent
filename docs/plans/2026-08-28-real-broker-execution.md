# Real Broker Execution Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add a controlled live execution path for Alpaca stock orders and ccxt crypto orders, backed by broker-side order reconciliation, Discord alerts, systemd deployment, exposure/correlation caps, market-hours checks, and a real Alpaca market-data source.

**Architecture:** Keep strategy and risk code broker-agnostic: `live_loop.py` should produce approved order intents, a new execution layer should submit/reconcile broker orders, and portfolio state should be rebuilt from broker-confirmed fills only. Treat broker state as authoritative for order status/fills, while SQLite remains the local audit log and restart cache.

**Tech Stack:** Python, SQLite, alpaca-py, ccxt, pandas, pytest, python-dotenv, Discord webhooks, systemd user service.

---

## Current Codebase Facts

- `live_loop.py` currently simulates every approved order with `fills.simulate_fill()` and writes `broker='paper'`, `status='filled'` directly to `orders`.
- `datasources.py` has real `YFinanceDataSource` and `CcxtDataSource`; `AlpacaDataSource` is a deliberate stub that raises `NotImplementedError`.
- `risk_gate.py` already enforces per-order position size, order value, cash buffer, open positions, daily loss, daily trade count, data freshness, and price sanity.
- `live_state.py` reconstructs positions only from local rows where `orders.status = 'filled'`.
- `db.py` already has an `orders` table but it is missing fields needed for robust reconciliation: client order id, broker timestamps, average fill price, last status sync, error message, and raw broker payload.
- `requirements.txt` already includes `ccxt` and `python-dotenv`, but not `alpaca-py`.

## Non-Negotiable Safety Rules

1. Default mode must stay paper/simulated unless explicitly configured for live broker execution.
2. First real broker implementation must support broker paper accounts before live capital.
3. Portfolio state must only apply real fills after broker-side status says filled/partially filled.
4. Every external side effect must be idempotent across process crashes/restarts.
5. Execution must refuse stock trades outside market hours unless explicitly configured to allow extended hours.
6. Alerts must fire for fills, execution errors, startup, shutdown, and daily summaries.
7. Secrets stay in `.env.local`; docs/examples only list variable names.

---

## Phase 0: Baseline Verification

### Task 0.1: Confirm current tests pass before touching execution

**Objective:** Establish a clean baseline so broker changes are not hiding old failures.

**Files:** none

**Steps:**

1. Run:
   ```bash
   cd /home/bud/trade-agent
   source .venv/bin/activate
   python -m pytest -q
   ```
2. Expected: all existing tests pass.
3. If failures exist, stop and fix baseline first.

**Commit:** none.

---

## Phase 1: Configuration and Dependencies

### Task 1.1: Add broker/data dependencies

**Objective:** Install and pin the libraries needed for Alpaca data/execution and test mocking.

**Files:**
- Modify: `requirements.txt`
- Modify: `requirements-dev.txt`

**Implementation:**

Add to `requirements.txt`:
```txt
alpaca-py>=0.42,<1
requests>=2.32,<3
```

If not already present, ensure `requirements-dev.txt` contains:
```txt
pytest
```

**Verification:**

```bash
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python - <<'PY'
import alpaca
import ccxt
import requests
print('broker deps ok')
PY
```

Expected: `broker deps ok`.

**Commit:**
```bash
git add requirements.txt requirements-dev.txt
git commit -m "chore: add broker execution dependencies"
```

### Task 1.2: Centralize runtime configuration

**Objective:** Replace scattered constants/env reads with one typed config object.

**Files:**
- Create: `config.py`
- Test: `test_config.py`
- Modify: `.env.local.example`

**Implementation sketch:**

```python
# config.py
from dataclasses import dataclass
import os

TRUE_VALUES = {'1', 'true', 'yes', 'on'}

@dataclass(frozen=True)
class AppConfig:
    execution_mode: str
    alpaca_api_key: str | None
    alpaca_secret_key: str | None
    alpaca_paper: bool
    crypto_exchange_id: str
    discord_webhook_url: str | None
    allow_extended_hours: bool
    max_correlated_exposure_pct: float
    market_timezone: str

    @property
    def broker_execution_enabled(self) -> bool:
        return self.execution_mode in {'paper_broker', 'live'}


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in TRUE_VALUES


def load_config() -> AppConfig:
    return AppConfig(
        execution_mode=os.getenv('EXECUTION_MODE', 'simulated'),
        alpaca_api_key=os.getenv('ALPACA_API_KEY'),
        alpaca_secret_key=os.getenv('ALPACA_SECRET_KEY'),
        alpaca_paper=_bool('ALPACA_PAPER', True),
        crypto_exchange_id=os.getenv('CRYPTO_EXCHANGE_ID', 'coinbaseexchange'),
        discord_webhook_url=os.getenv('DISCORD_WEBHOOK_URL'),
        allow_extended_hours=_bool('ALLOW_EXTENDED_HOURS', False),
        max_correlated_exposure_pct=float(os.getenv('MAX_CORRELATED_EXPOSURE_PCT', '0.35')),
        market_timezone=os.getenv('MARKET_TIMEZONE', 'America/New_York'),
    )
```

Add `.env.local.example` entries:
```env
EXECUTION_MODE=simulated
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_PAPER=true
CRYPTO_EXCHANGE_ID=coinbaseexchange
DISCORD_WEBHOOK_URL=
ALLOW_EXTENDED_HOURS=false
MAX_CORRELATED_EXPOSURE_PCT=0.35
MARKET_TIMEZONE=America/New_York
```

**Tests:**

```python
# test_config.py
from config import load_config


def test_config_defaults_to_simulated(monkeypatch):
    monkeypatch.delenv('EXECUTION_MODE', raising=False)
    cfg = load_config()
    assert cfg.execution_mode == 'simulated'
    assert not cfg.broker_execution_enabled


def test_config_enables_broker_execution(monkeypatch):
    monkeypatch.setenv('EXECUTION_MODE', 'paper_broker')
    cfg = load_config()
    assert cfg.broker_execution_enabled
```

**Verification:**
```bash
python -m pytest test_config.py -q
```

**Commit:**
```bash
git add config.py test_config.py .env.local.example
git commit -m "feat: add runtime configuration"
```

---

## Phase 2: Database Schema for Real Orders

### Task 2.1: Extend `orders` schema for idempotent reconciliation

**Objective:** Store enough broker metadata to safely resume after crashes.

**Files:**
- Modify: `db.py`
- Test: `test_db.py`

**Schema additions:**

Add columns to `orders` creation and migrations:

```sql
client_order_id TEXT UNIQUE,
broker_status TEXT,
submitted_broker_at INTEGER,
updated_broker_at INTEGER,
last_reconciled_at INTEGER,
avg_fill_price REAL,
remaining_qty REAL,
error_message TEXT,
raw_broker_json TEXT
```

Keep existing fields for backwards compatibility:
- `broker_order_id`
- `status`
- `filled_price`
- `filled_qty`
- `fee`

**Expected status model:**

Local `orders.status` should be one of:
- `pending_submit`
- `submitted`
- `partially_filled`
- `filled`
- `cancelled`
- `rejected`
- `error`

**Tests:**

Add assertions in `test_db.py` that fresh and migrated DBs include every new column.

**Verification:**
```bash
python -m pytest test_db.py -q
```

**Commit:**
```bash
git add db.py test_db.py
git commit -m "feat: extend order schema for broker reconciliation"
```

### Task 2.2: Add order repository helpers

**Objective:** Avoid raw SQL duplication between live loop, execution clients, and reconciliation.

**Files:**
- Create: `orders.py`
- Test: `test_orders.py`

**Core functions:**

```python
def create_pending_order(conn, *, signal_id, broker, symbol, side, qty, order_type, client_order_id, submitted_at):
    ...


def mark_order_submitted(conn, *, client_order_id, broker_order_id, broker_status, raw_broker_json, now_ts):
    ...


def update_order_from_broker(conn, *, broker, broker_order_id, broker_status, filled_qty, avg_fill_price, remaining_qty, raw_broker_json, now_ts):
    ...


def list_open_orders(conn, broker: str | None = None) -> list[dict]:
    ...
```

**Tests:**

- Creating the same `client_order_id` twice is idempotent or fails cleanly without duplicate rows.
- Submitted order can move to `filled`.
- `list_open_orders()` excludes terminal statuses.

**Verification:**
```bash
python -m pytest test_orders.py -q
```

**Commit:**
```bash
git add orders.py test_orders.py
git commit -m "feat: add order repository helpers"
```

---

## Phase 3: AlpacaDataSource

### Task 3.1: Implement Alpaca historical bars

**Objective:** Replace the Alpaca stub with a real `alpaca-py` stock data source.

**Files:**
- Modify: `datasources.py`
- Modify: `requirements.txt`
- Test: `test_datasources.py`

**Implementation notes:**

- Use `alpaca.data.historical.StockHistoricalDataClient`.
- Use `alpaca.data.requests.StockBarsRequest`.
- Use `alpaca.data.timeframe.TimeFrame` / `TimeFrameUnit` mapping.
- Read credentials from `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` if not passed to constructor.
- Normalize to existing contract: columns `[timestamp, open, high, low, close, volume]`, timestamp as UTC epoch seconds, sorted ascending.

**Timeframe map:**

```python
_ALPACA_TIMEFRAMES = {
    '1d': TimeFrame.Day,
    '1h': TimeFrame.Hour,
    '5m': TimeFrame(5, TimeFrameUnit.Minute),
    '1m': TimeFrame.Minute,
}
```

**Failure behavior:**

- Missing keys: raise `DataSourceError('alpaca: missing ALPACA_API_KEY/ALPACA_SECRET_KEY')`.
- Empty bars: raise `DataSourceError`.
- API exception: wrap in `DataSourceError`.

**Tests:**

Mock `StockHistoricalDataClient.get_stock_bars()` and verify:
- Missing env raises `DataSourceError`.
- Unsupported timeframe raises `DataSourceError`.
- Returned bars normalize timestamps to seconds.
- `get_data_source('alpaca')` returns a real `AlpacaDataSource` instance without contacting network.

**Verification:**
```bash
python -m pytest test_datasources.py -q
```

**Commit:**
```bash
git add datasources.py test_datasources.py requirements.txt
git commit -m "feat: implement Alpaca market data source"
```

---

## Phase 4: Broker Execution Layer

### Task 4.1: Define broker interface and order intent model

**Objective:** Create a broker-neutral contract for submitting and reconciling orders.

**Files:**
- Create: `brokers.py`
- Test: `test_brokers.py`

**Implementation sketch:**

```python
from dataclasses import dataclass
from typing import Protocol, Literal

Side = Literal['buy', 'sell']
OrderStatus = Literal['submitted', 'partially_filled', 'filled', 'cancelled', 'rejected', 'error']

@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    asset_type: str
    side: Side
    qty: float
    order_type: str
    client_order_id: str

@dataclass(frozen=True)
class BrokerOrderState:
    broker: str
    broker_order_id: str | None
    client_order_id: str
    symbol: str
    status: OrderStatus
    filled_qty: float
    avg_fill_price: float | None
    remaining_qty: float | None
    raw: dict

class BrokerClient(Protocol):
    broker_name: str
    def submit_order(self, intent: OrderIntent) -> BrokerOrderState: ...
    def get_order(self, broker_order_id: str | None = None, client_order_id: str | None = None) -> BrokerOrderState: ...
    def list_open_orders(self) -> list[BrokerOrderState]: ...
```

**Tests:**

- Dataclass construction.
- Protocol-compatible fake client.

**Verification:**
```bash
python -m pytest test_brokers.py -q
```

**Commit:**
```bash
git add brokers.py test_brokers.py
git commit -m "feat: define broker execution interface"
```

### Task 4.2: Implement Alpaca stock broker client

**Objective:** Submit/reconcile stock market orders through Alpaca paper/live endpoint.

**Files:**
- Create: `alpaca_broker.py`
- Test: `test_alpaca_broker.py`

**Implementation notes:**

- Use `alpaca.trading.client.TradingClient`.
- Use `MarketOrderRequest` for now; do not add options/limit/bracket orders yet.
- Use `client_order_id` so retries do not duplicate orders.
- Paper mode default: `TradingClient(api_key, secret_key, paper=True)`.
- Map Alpaca statuses to local statuses.

**Test cases:**

- Missing keys raises clear config error.
- `submit_order()` passes symbol/qty/side/time_in_force/client_order_id.
- `get_order()` maps filled, partially_filled, canceled, rejected.
- Duplicate submit should call `get_order_by_client_order_id` when Alpaca reports duplicate client id.

**Verification:**
```bash
python -m pytest test_alpaca_broker.py -q
```

**Commit:**
```bash
git add alpaca_broker.py test_alpaca_broker.py
git commit -m "feat: add Alpaca stock broker client"
```

### Task 4.3: Implement ccxt crypto broker client

**Objective:** Submit/reconcile crypto market orders through ccxt with exchange-specific credentials.

**Files:**
- Create: `ccxt_broker.py`
- Test: `test_ccxt_broker.py`
- Modify: `.env.local.example`

**Config additions:**

```env
CCXT_API_KEY=
CCXT_SECRET=
CCXT_PASSWORD=
```

**Implementation notes:**

- Use `CRYPTO_EXCHANGE_ID` to construct ccxt exchange class.
- Require API keys only when execution is enabled.
- Use `create_order(symbol, 'market', side, qty, params={...})`.
- Put `client_order_id` into params where supported; preserve it locally even if exchange does not support it.
- Use `fetch_order()` for reconciliation.

**Test cases:**

- Exchange created with `enableRateLimit=True`.
- Missing credentials fail before submit.
- `submit_order()` maps ccxt response into `BrokerOrderState`.
- `get_order()` maps closed/open/canceled/rejected.

**Verification:**
```bash
python -m pytest test_ccxt_broker.py -q
```

**Commit:**
```bash
git add ccxt_broker.py test_ccxt_broker.py .env.local.example
git commit -m "feat: add ccxt crypto broker client"
```

### Task 4.4: Add broker router/factory

**Objective:** Route stock intents to Alpaca and crypto intents to ccxt.

**Files:**
- Create: `broker_factory.py`
- Test: `test_broker_factory.py`

**Behavior:**

```python
def make_broker_for_asset(asset_type: str, cfg: AppConfig) -> BrokerClient:
    if asset_type == 'stock': return AlpacaBrokerClient(...)
    if asset_type == 'crypto': return CcxtBrokerClient(...)
    raise ValueError(...)
```

**Verification:**
```bash
python -m pytest test_broker_factory.py -q
```

**Commit:**
```bash
git add broker_factory.py test_broker_factory.py
git commit -m "feat: route orders to broker by asset type"
```

---

## Phase 5: Broker-Side Reconciliation

### Task 5.1: Build reconciliation service

**Objective:** Poll broker-side order state and update local DB before strategy decisions run.

**Files:**
- Create: `reconcile.py`
- Test: `test_reconcile.py`

**Behavior:**

1. Load local open orders via `orders.list_open_orders()`.
2. Group by broker.
3. Fetch each broker order by `broker_order_id` or `client_order_id`.
4. Update local order status/filled_qty/avg_fill_price/remaining_qty/raw payload.
5. Return a list of state changes for alerting.

**Critical rule:** only after reconciliation marks an order `filled` or increases `filled_qty` should portfolio state apply fills.

**Tests:**

- Submitted -> filled updates DB.
- Submitted -> rejected updates error/status.
- Partial fill update is idempotent: same broker fill quantity processed twice does not double-count portfolio.

**Verification:**
```bash
python -m pytest test_reconcile.py -q
```

**Commit:**
```bash
git add reconcile.py test_reconcile.py
git commit -m "feat: reconcile local orders with broker state"
```

### Task 5.2: Change portfolio reconstruction to handle partial fills safely

**Objective:** Ensure `live_state.py` rebuilds positions from real filled quantities without double-counting.

**Files:**
- Modify: `live_state.py`
- Test: `test_live_state.py`

**Approach:**

- Continue replaying terminal filled rows.
- For partially-filled rows, replay the current confirmed `filled_qty` once.
- Prefer `avg_fill_price` when present, fallback to existing `filled_price`.
- Ignore `submitted`, `cancelled`, `rejected`, `error` orders unless they have a confirmed `filled_qty > 0`.

**Verification:**
```bash
python -m pytest test_live_state.py -q
```

**Commit:**
```bash
git add live_state.py test_live_state.py
git commit -m "fix: reconstruct portfolio from broker-confirmed fills"
```

---

## Phase 6: Market Hours and Risk Expansion

### Task 6.1: Add market-hours checks for stocks

**Objective:** Prevent stock orders outside market hours by default.

**Files:**
- Create: `market_hours.py`
- Test: `test_market_hours.py`
- Modify: `risk_gate.py`
- Modify: `test_risk_gate.py`

**Implementation:**

- Use `zoneinfo.ZoneInfo('America/New_York')`.
- Basic initial rule: weekdays, 09:30 <= local time < 16:00.
- Do not try to model holidays in this task unless using Alpaca clock endpoint later.
- Crypto should return open 24/7.

**Risk integration:**

Add `RiskConfig.enforce_market_hours: bool = True` and check stock signals before order execution.

**Tests:**

- Stock buy Monday 10:00 ET passes.
- Stock buy Monday 08:00 ET fails.
- Stock buy Saturday fails.
- Crypto buy Saturday passes.
- `ALLOW_EXTENDED_HOURS=true` can bypass this only if explicitly wired in config.

**Verification:**
```bash
python -m pytest test_market_hours.py test_risk_gate.py -q
```

**Commit:**
```bash
git add market_hours.py test_market_hours.py risk_gate.py test_risk_gate.py
git commit -m "feat: add market-hours risk checks"
```

### Task 6.2: Add correlation/exposure cap

**Objective:** Block new orders that over-concentrate the portfolio in correlated assets.

**Files:**
- Create: `exposure.py`
- Test: `test_exposure.py`
- Modify: `risk_gate.py`
- Modify: `test_risk_gate.py`

**YAGNI initial model:**

Use configured correlation buckets instead of calculating rolling correlations live.

```python
CORRELATION_BUCKETS = {
    'mega_cap_tech': {'AAPL', 'MSFT', 'NVDA', 'GOOGL', 'META', 'TSLA'},
    'crypto_major': {'BTC/USD', 'ETH/USD'},
}
```

Add to `RiskConfig`:

```python
max_correlated_exposure_pct: float = 0.35
correlation_buckets: dict[str, set[str]] = field(default_factory=lambda: CORRELATION_BUCKETS.copy())
```

Check projected bucket exposure after the proposed order:

```python
projected_bucket_value / total_equity <= max_correlated_exposure_pct
```

**Tests:**

- Buying AAPL when current mega-cap bucket is 34% and order pushes to 36% fails.
- Buying unrelated symbol does not count against AAPL bucket.
- Selling always passes exposure-cap check.

**Verification:**
```bash
python -m pytest test_exposure.py test_risk_gate.py -q
```

**Commit:**
```bash
git add exposure.py test_exposure.py risk_gate.py test_risk_gate.py
git commit -m "feat: add correlated exposure cap"
```

---

## Phase 7: Discord Alerting

### Task 7.1: Add Discord webhook client

**Objective:** Send structured alerts without coupling the trading loop to `requests` details.

**Files:**
- Create: `alerts.py`
- Test: `test_alerts.py`

**Implementation sketch:**

```python
import requests

class DiscordAlertClient:
    def __init__(self, webhook_url: str | None, timeout_seconds: float = 5.0):
        self.webhook_url = webhook_url
        self.timeout_seconds = timeout_seconds

    def send(self, content: str, embeds: list[dict] | None = None) -> bool:
        if not self.webhook_url:
            return False
        resp = requests.post(
            self.webhook_url,
            json={'content': content, 'embeds': embeds or []},
            timeout=self.timeout_seconds,
        )
        resp.raise_for_status()
        return True
```

**Alert helpers:**

- `alert_fill(order_state, portfolio_snapshot)`
- `alert_error(symbol, exc)`
- `alert_daily_summary(summary)`
- `alert_startup(config)`
- `alert_shutdown(reason)`

**Tests:**

Mock `requests.post()`:
- no webhook returns `False` and does not call network.
- fill alert posts expected content.
- HTTP error raises or logs according to final policy.

**Verification:**
```bash
python -m pytest test_alerts.py -q
```

**Commit:**
```bash
git add alerts.py test_alerts.py
git commit -m "feat: add Discord webhook alerts"
```

### Task 7.2: Add daily summary generation

**Objective:** Produce one daily status payload that can be logged and sent to Discord.

**Files:**
- Create: `summaries.py`
- Test: `test_summaries.py`

**Summary contents:**

- Date.
- Total equity.
- Cash.
- Realized P/L today.
- Unrealized P/L.
- Filled order count.
- Rejected/error order count.
- Trading halted state/reason.

**Verification:**
```bash
python -m pytest test_summaries.py -q
```

**Commit:**
```bash
git add summaries.py test_summaries.py
git commit -m "feat: add daily trading summaries"
```

---

## Phase 8: Wire Execution into `live_loop.py`

### Task 8.1: Extract order-intent creation from simulated fill path

**Objective:** Make `live_loop.py` able to choose simulated vs broker execution without duplicating strategy/risk logic.

**Files:**
- Modify: `live_loop.py`
- Test: `test_live_loop.py`

**Approach:**

- Create `_make_order_intent(signal, cfg, now_ts)`.
- Generate deterministic `client_order_id`, e.g. `ta-{symbol_slug}-{bar_ts}-{signal.action}`.
- Keep existing simulated mode unchanged.

**Verification:**
```bash
python -m pytest test_live_loop.py -q
```

**Commit:**
```bash
git add live_loop.py test_live_loop.py
git commit -m "refactor: extract live order intent creation"
```

### Task 8.2: Submit broker orders when execution mode enables it

**Objective:** Replace simulated immediate fills with broker submission in `paper_broker`/`live` mode.

**Files:**
- Modify: `live_loop.py`
- Test: `test_live_loop.py`

**Behavior:**

- On each loop cycle, reconcile open broker orders first.
- Fetch latest market data.
- Generate signal.
- Run risk gate including market-hours/exposure checks.
- If approved and `EXECUTION_MODE=simulated`: current simulated fill behavior.
- If approved and `EXECUTION_MODE in {'paper_broker', 'live'}`:
  1. Insert local `pending_submit` order with `client_order_id`.
  2. Submit through broker client.
  3. Mark local order `submitted` / `filled` / `rejected` based on immediate broker response.
  4. Alert on submit/fill/error.

**Crash rule:** If process dies after local `pending_submit` insert but before submit, next boot should reconcile/inspect and either submit once or mark error. Do not blindly resubmit without checking `client_order_id`.

**Verification:**
```bash
python -m pytest test_live_loop.py test_orders.py test_reconcile.py -q
python live_loop.py --once
```

Expected: in default `EXECUTION_MODE=simulated`, behavior remains local/simulated and no broker credentials are needed.

**Commit:**
```bash
git add live_loop.py test_live_loop.py
git commit -m "feat: wire broker execution into live loop"
```

---

## Phase 9: systemd Deployment

### Task 9.1: Add installable systemd unit template

**Objective:** Make the live loop restart on crash and boot under the user service manager.

**Files:**
- Create: `deploy/trade-agent.service`
- Create: `deploy/install-systemd-user-service.sh`
- Create: `deploy/README.md`

**Service template:**

```ini
[Unit]
Description=Trade Agent live execution loop
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/bud/trade-agent
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/bud/trade-agent/.venv/bin/python /home/bud/trade-agent/live_loop.py
Restart=on-failure
RestartSec=15
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=default.target
```

**Install script:**

```bash
#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$HOME/.config/systemd/user"
cp deploy/trade-agent.service "$HOME/.config/systemd/user/trade-agent.service"
systemctl --user daemon-reload
systemctl --user enable trade-agent.service
systemctl --user restart trade-agent.service
systemctl --user status trade-agent.service --no-pager
```

**Verification commands:**

```bash
systemctl --user daemon-reload
systemd-analyze --user verify deploy/trade-agent.service
```

**Commit:**
```bash
git add deploy/trade-agent.service deploy/install-systemd-user-service.sh deploy/README.md
git commit -m "deploy: add systemd user service"
```

### Task 9.2: Add service runbook

**Objective:** Document operational commands and failure modes.

**Files:**
- Modify: `README.md`
- Create or modify: `deploy/README.md`

**Include:**

```bash
systemctl --user start trade-agent.service
systemctl --user stop trade-agent.service
systemctl --user restart trade-agent.service
systemctl --user status trade-agent.service --no-pager
journalctl --user -u trade-agent.service -f
```

Also document:
- how to set `.env.local`;
- how to stay in `EXECUTION_MODE=simulated`;
- how to switch to `paper_broker`;
- never switch to `live` until paper broker mode has run cleanly for multiple sessions.

**Verification:** docs review.

**Commit:**
```bash
git add README.md deploy/README.md
git commit -m "docs: add trade-agent service runbook"
```

---

## Phase 10: End-to-End Validation

### Task 10.1: Run full local test suite

**Objective:** Confirm unit/integration tests pass.

**Command:**

```bash
source .venv/bin/activate
python -m pytest -q
```

Expected: all tests pass.

### Task 10.2: Run simulated once-mode smoke test

**Objective:** Prove default mode still does not require broker credentials.

**Command:**

```bash
unset ALPACA_API_KEY ALPACA_SECRET_KEY DISCORD_WEBHOOK_URL
EXECUTION_MODE=simulated python live_loop.py --once
```

Expected:
- exits successfully;
- no broker API call required;
- local simulated mode still writes rows as before.

### Task 10.3: Run broker paper-mode dry run with real credentials

**Objective:** Validate broker connectivity without live capital.

**Preconditions:**

`.env.local` contains paper credentials:

```env
EXECUTION_MODE=paper_broker
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_PAPER=true
DISCORD_WEBHOOK_URL=...
```

**Command:**

```bash
source .venv/bin/activate
python live_loop.py --once
```

Expected:
- Alpaca data fetch succeeds for stock symbols configured to use `alpaca`;
- approved stock orders submit to Alpaca paper only;
- crypto orders submit only if ccxt credentials are present and asset is enabled;
- Discord receives startup/error/fill alerts as applicable;
- `orders` table includes broker ids and reconciled statuses.

### Task 10.4: Validate service restart behavior

**Objective:** Confirm systemd restarts the loop on crash.

**Commands:**

```bash
systemctl --user restart trade-agent.service
systemctl --user status trade-agent.service --no-pager
journalctl --user -u trade-agent.service -n 100 --no-pager
```

Then intentionally stop/restart, not kill during a real order window:

```bash
systemctl --user restart trade-agent.service
```

Expected:
- process restarts cleanly;
- open orders reconcile on next cycle;
- no duplicate broker orders with same `client_order_id`.

---

## Acceptance Criteria

- [ ] `EXECUTION_MODE=simulated` preserves current no-credentials behavior.
- [ ] `AlpacaDataSource` fetches normalized OHLCV with Alpaca API keys and raises clear `DataSourceError` without them.
- [ ] Stock orders route through Alpaca broker client in `paper_broker`/`live` mode.
- [ ] Crypto orders route through ccxt broker client in `paper_broker`/`live` mode.
- [ ] Orders use deterministic `client_order_id` and do not duplicate after crash/retry.
- [ ] Reconciliation polls broker status and updates local DB before each strategy cycle.
- [ ] Portfolio reconstruction uses broker-confirmed fill quantities only.
- [ ] Discord webhook alerts fire for fills, execution errors, startup/shutdown, and daily summaries.
- [ ] Stock execution is blocked outside regular market hours unless extended-hours mode is explicitly enabled.
- [ ] Correlation/exposure cap rejects orders that exceed configured bucket exposure.
- [ ] `deploy/trade-agent.service` passes `systemd-analyze --user verify` and restarts on crash.
- [ ] Full test suite passes with no broker credentials.

## Suggested Implementation Order

1. Config/dependencies.
2. DB/order repository.
3. AlpacaDataSource.
4. Broker interface + Alpaca/ccxt clients.
5. Reconciliation.
6. Market-hours + exposure risk checks.
7. Alerts + summaries.
8. Wire live loop.
9. systemd deployment.
10. Paper broker validation.

## Explicit Deferrals

- Options, bracket orders, trailing stops, margin, shorting, futures, and multi-leg orders.
- Automatic live-mode promotion.
- Dynamic statistical correlation calculation.
- Holiday-accurate market calendar unless Alpaca clock/calendar endpoint is added in a later pass.
- More strategies. This plan hardens execution plumbing, not alpha.
