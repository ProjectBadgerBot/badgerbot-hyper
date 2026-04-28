# BadgerBot Hyper

Signal-driven perpetual trading bot for [Hyperliquid](https://hyperliquid.xyz). Receives live signals from [BadgerBot](https://badgerbot.io) via WebSocket, executes trades automatically, and lets you monitor and control everything through Telegram.

---

## ⚠️ Disclaimer

> **This project is for educational purposes only.**

- **Not financial advice.** Nothing in this repository constitutes financial, investment, or trading advice of any kind.
- **Risk of total loss.** Cryptocurrency trading is highly speculative. You may lose some or all of your funds. Never trade with money you cannot afford to lose.
- **High volatility.** Crypto markets are extremely volatile and unpredictable. Automated bots do not eliminate or reduce this risk.
- **No performance guarantees.** Past results, backtests, or examples shown do not guarantee future performance.
- **Your responsibility.** By using this software, you accept full responsibility for any financial outcomes. The author(s) are not liable for any losses incurred.
- **Consult a professional.** Before engaging in any trading activity, consider seeking advice from a licensed financial advisor.

*By installing or using this bot, you acknowledge that you have read and understood this disclaimer.*

---

## YouTube Video guide series:
Within the readme, six video's are added for reference and cover the basics on installing the Badger Bot. The video's can be found under the following links:
1. [Choose Your Path](#choose-your-path)
2. [Before You Start](#before-you-start)
3. [Hyperliquid API Wallet](#1-hyperliquid-api-wallet)
4. [BadgerBot API Key & Telegram Bot & User ID](#21-badgerbot-api-key)
5. [Path B: VPS / Ubuntu Server](#path-b-vps--ubuntu-server)
6. [Updating The bot](#updating-the-bot)

## Choose Your Path

#### YouTube Video Guide: [Install Badger Bot #1](https://youtu.be/ZSRQBvNP8No)

Before starting the installment, determine what type of server you would like to use. In general, a Render service  is easier to install and use yet limits customizability. A VPS / server running on the operating system Ubuntu, has more installment steps yet a larger degree of customizability and more secure (if setup with SSH).

| | Path A: Render | Path B: VPS / Server |
|---|---|---|
| Experience needed | None | Basic terminal commands |
| Setup time | ~15 min | ~20 min |
| Monthly cost | ~$8 | ~$4+ |
| Server management | None (browser only) | SSH access |
| Best for | Getting started fast | Lower cost, full control |

→ Jump to [Path A: Render (Cloud)](#path-a-render-cloud)\
→ Jump to [Path B: VPS / Ubuntu Server](#path-b-vps--ubuntu-server)

---

## Before You Start

#### YouTube Video Guide: [Install Badger Bot #2](https://www.youtube.com/watch?v=q-bco4QEkMk)

Both paths need the same three things. Get these ready before you begin.

**Prerequisites:**
- A crypto wallet with funds (e.g. [Rabby](https://rabby.io)) to deposit USDC into Hyperliquid
- A Telegram account
- An active [BadgerBot](https://badgerbot.io) subscription and API key
- A Render service or VPS / Server running the operating system: Ubuntu. More information on this in: [Path B: VPS / Ubuntu Server](#path-b-vps--ubuntu-server)

### 1. Hyperliquid API Wallet

#### YouTube Video Guide: [Install Badger Bot #3](https://www.youtube.com/watch?v=zPDts-7mVE4)

To run the bot on Hyperliquid, an API key is required. The API key is only used for placing orders — it has no withdrawal rights. 
Before requesting an API key, it is required to deposit funds on the platform which can be done by pressing the 'Deposit' button on [app.hyperliquid.xyz/portfolio](https://app.hyperliquid.xyz/portfolio).

1. Go to [app.hyperliquid.xyz/API](https://app.hyperliquid.xyz/API)
2. Click **Generate** to create an API wallet
3. Copy the **private key** shown (starts with `0x`) and temporary store it
4. Your main wallet address (the one you log in with)

### 2.1 BadgerBot API Key

#### YouTube Video Guide: [Install Badger Bot #4](https://www.youtube.com/watch?v=VPXF2LAxaSI)

Log in to your BadgerBot dashboard and copy your API key → this is `BADGERBOT_API_KEY`.

### 2.2 Telegram Bot + User ID

To control and monitor the trading bot, you'll connect it to a Telegram bot you own.

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`, pick a name and username
3. BotFather gives you a token like `110201543:AAHdqTcvCH1vGWJxfSeofSs4tDXtoAg` → this is `TELEGRAM_BOT_TOKEN`
4. To find your numeric user ID, message [@userinfobot](https://t.me/userinfobot) and send `test` → the number shown is `TELEGRAM_AUTHORIZED_USER_ID`

---

## Path A: Render (Cloud)

Deploy the bot on Render — no server setup, managed entirely from your browser.

**Cost:** ~$7/month (Background Worker) + ~$1/month (Persistent Disk for trade history)

### Step 1: Fork the Repository

1. Log in to [GitHub](https://github.com)
2. Open the repository page and click **Fork → Create fork**

### Step 2: Create a Render Account

1. Go to [render.com](https://render.com) and sign up
2. In **Account Settings → Git**, connect your GitHub account

### Step 3: Create a Background Worker

1. In the Render dashboard, click **New → Background Worker**
2. Select your forked repository and click **Connect**
3. Fill in the service settings:
   - **Name:** `badgerbot-hyper`
   - **Branch:** `main`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python main.py`
   - **Instance Type:** Starter ($7/month)
4. Click **Advanced** and then **Add Disk**:
   - **Name:** `data`
   - **Mount Path:** `/data`
   - **Size:** 1 GB
5. Click **Create Background Worker**

The first deploy will fail — that's expected until you add your credentials in the next step.

### Step 4: Add Environment Variables

In your Render service, go to **Environment** and add each variable.

> **These are mandatory** — the bot will not start without them.

| Variable | Value |
|---|---|
| `HYPERBOT_DB_PATH` | `/data/hyperbot.db` |
| `HL_ACCOUNT_ADDRESS` | Your main wallet address |
| `HL_API_PRIVATE_KEY` | Your API wallet private key |
| `BADGERBOT_API_KEY` | Your BadgerBot API key |
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token |
| `TELEGRAM_AUTHORIZED_USER_ID` | Your numeric Telegram user ID |
| `POSITION_SIZE_PCT` | `0.05` |

### Step 5: Deploy

1. Click **Manual Deploy → Deploy latest commit**
2. Open the deploy log and wait for: `Starting signal consumer, position monitor, and Telegram bot...`
3. Send `/status` to your Telegram bot — it should respond with your account balance

Your bot is now live. It restarts automatically on crash and on new code deploys.

---

## Path B: VPS / Ubuntu Server

#### YouTube Video Guide: [Install Badger Bot #5](https://www.youtube.com/watch?v=5EwYJNIYPpw) This video does NOT cover server purchase and setup. For this, watching [Purchase & Setup Hetzner Server](https://www.youtube.com/watch?v=mnFQ2mGJnXI) is reccomended.

Run the bot on any Ubuntu 24.04+ machine — a VPS, a home server, or your local machine.

### Step 1: Get a Server (skip for local)

Any Ubuntu 24.04+ VPS works. A few options:

| Provider | Plan | Cost |
|---|---|---|
| [Hetzner](https://hetzner.com) | CX11 | ~€4/month |
| [DigitalOcean](https://digitalocean.com) | Basic Droplet | ~$6/month |
| [Contabo](https://contabo.com) | VPS S | ~€5/month |

For vps / server purchasing and setup, the video: [Purchase & Setup Hetzner Server](https://www.youtube.com/watch?v=mnFQ2mGJnXI) is of good use.
> **Optional** Increase vps / server security using SSH keys, view article: [Set up SSH Key Login](https://www.ricmedia.com/tutorials/how-to-set-up-ssh-keys-on-ubuntu-22-04)

SSH into your server:
```bash
ssh root@your-server-ip
```

### Step 2: Clone and Install

Install Python 3.12, clone the repo, and run the setup script:

```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv git

git clone https://github.com/ProjectBadgerBot/badgerbot-hyper.git
cd badgerbot-hyper
git config --global --add safe.directory "$(pwd)"

bash setup.sh
```

`setup.sh` creates a `.venv`, installs all four dependencies, and copies `.env.example` to `.env`.

> **Updating later:** run `git pull` inside the `badgerbot-hyper` folder to pull in new code. Your `.env` settings file is never touched by updates.

### Step 3: Configure .env

```bash
nano .env
```

> **These credentials are mandatory** — the bot will not start if any are missing. To disable an optional variable, put a `#` in front of it.

Fill in your credentials:

```env (mandatory credentials)
# Hyperliquid
HL_ACCOUNT_ADDRESS=0xYourMainWalletAddress
HL_API_PRIVATE_KEY=0xYourApiPrivateKey

# Signal source
BADGERBOT_API_KEY=your-badgerbot-api-key

# Telegram
TELEGRAM_BOT_TOKEN=110201543:AAHdqTcvCH1vGWJxfSeofSs4tDXtoAg
TELEGRAM_AUTHORIZED_USER_ID=123456789
```

Save with `Ctrl+O`, exit with `Ctrl+X`.

### Step 4: Test a Trade

Verify the bot can place and protect orders before leaving it running unattended:

```bash
.venv/bin/python simulate_signals.py
```

This opens one ETH LONG and one ETH SHORT at minimum size (~$11 notional each), then automatically closes both and cancels their TP/SL orders. You'll receive a Telegram notification for each trade and a summary when cleanup is done.

```bash
# Test only one direction if needed:
.venv/bin/python simulate_signals.py --mode long
.venv/bin/python simulate_signals.py --mode short
```

> **This places real orders on your mainnet account.** Make sure your account has at least $25 USDC before running.

### Step 5: Start the Bot

```bash
.venv/bin/python main.py
```

Wait until you see this line in the output:

```
Starting signal consumer, position monitor, and Telegram bot...
```

On Telegram, send `/status` to your Telegram bot — it should reply with your account balance. Once you've confirmed it's working, move on to Step 6 to keep it running after you close the terminal.

If you see `Missing required environment variables` or `Failed to connect to Hyperliquid`, stop here and check the [Common Issues](#common-issues) section before continuing.

### Step 6: Keep It Running

First, check which user you're logged in as — run `whoami`. If you SSHed as `root`, your username is `root`. If you're unsure, the terminal prompt also shows your username at the bottom left (e.g. `root@hostname` or `yourname@hostname`).

Run this once to keep the bot running after you disconnect or reboot:

```bash
sudo loginctl enable-linger $USER
```

Then install and start the service:

```bash
bash install-service.sh
```

> **Custom service name (optional):** the service name defaults to `badgerbot`. To use a different name (e.g. when running multiple bot instances on the same machine), pass it as the first argument:
>
> ```bash
> bash install-service.sh <your-name>
> ```
>
> Replace `<your-name>` with any tag you like. All `systemctl` and `journalctl` commands below then use that name instead of `badgerbot` (e.g. `systemctl --user status <your-name>`).

**Checking status and logs:**

```bash
# If you are NOT root (non-root user):
systemctl --user status badgerbot     # check if running
journalctl --user -u badgerbot -f     # live log stream
journalctl --user -u badgerbot -n 100 # last 100 lines
systemctl --user restart badgerbot    # restart
systemctl --user stop badgerbot       # stop
# Don't replace --user with your username.

# If you ARE root:
systemctl status badgerbot            # check if running
journalctl -u badgerbot -f            # live log stream
journalctl -u badgerbot -n 100        # last 100 lines
systemctl restart badgerbot           # restart
systemctl stop badgerbot              # stop
```

---

## Updating the Bot

#### YouTube Video Guide: [Install Badger Bot #6](https://www.youtube.com/watch?v=QroSAsK4bqs)

From inside your `badgerbot-hyper` folder:

```bash
git pull
```

Your `.env` settings file is never touched by updates. If the bot is running as a service, restart it after:

```bash
systemctl --user restart badgerbot   # non-root
systemctl restart badgerbot          # root
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `HL_ACCOUNT_ADDRESS` | Yes | — | Main wallet address (`0x...`) |
| `HL_API_PRIVATE_KEY` | Yes | — | API wallet private key — no withdrawal access |
| `BADGERBOT_API_KEY` | Yes | — | Signal stream key from BadgerBot dashboard |
| `TELEGRAM_BOT_TOKEN` | Yes | — | Token from @BotFather |
| `TELEGRAM_AUTHORIZED_USER_ID` | Yes | — | Your numeric Telegram user ID |
| `POSITION_SIZE_PCT` | No | `0.05` | Trade notional as fraction of equity |
| `POSITION_SIZE_USD` | No | — | Fixed margin per trade in USD — overrides PCT |
| `RISK_PCT` | No | — | Max loss per trade at SL as fraction of equity — overrides both above |
| `MAX_SIGNAL_AGE_SECONDS` | No | `60` | Drop signals older than this |
| `MAX_PRICE_DEVIATION_PCT` | No | `0.01` | Drop signal if mark price moved more than 1% |
| `POSITION_POLL_INTERVAL_SECONDS` | No | `15` | How often to check for filled TP/SL orders |
| `HYPERBOT_DB_PATH` | No | `./hyperbot.db` | Path to the trade database (set to `/data/hyperbot.db` on Render) |
| `ALGORITHMS` | No | `Ethereum Main` | Comma-separated list of algorithms this bot should act on — see [Algorithms](#algorithms) |

---

## Position Sizing

Three modes, in priority order. Set only one at a time.

### Risk-Based (Default)

Size each trade so a stop-loss hit costs exactly X% of your portfolio:

```env
RISK_PCT=0.01
```

With $1,000 equity and `RISK_PCT=0.01`, every SL hit costs at most $10 regardless of TP/SL distance. When multiple signals arrive at the same price within 3 seconds, the risk budget is split evenly.

### Fixed USD Margin

Same dollar margin every trade:

```env
POSITION_SIZE_USD=20
```

$20 margin at 10x leverage = $200 notional.

### Percentage of Equity

Notional as a fraction of total equity:

```env
POSITION_SIZE_PCT=0.05
```

With $500 equity, each trade opens a $25 notional position. To disable a mode, comment it out or remove the line — do not set it to `false`.

---

## Algorithms

A BadgerBot subscription may grant access to several signal algorithms. Your server-side subscription exposes an opt-in list roughly shaped like:

```json
[
  {"algorithm": "ETH - Calibration - COMBO", "display_name": "Ethereum Main", "subscriber_field": "get_ethereum_main", "telegram": true},
  {"algorithm": "ETH - RSI", "display_name": "Ethereum Custom RSI", "subscriber_field": "get_custom_rsi", "telegram": false}
]
```

Use `ALGORITHMS` in your `.env` to tell this bot which of those algorithms it should act on, matched against the signal's `display_name`. Every other signal that arrives over the WebSocket is logged and skipped. Multiple algorithms are comma-separated.

```env
# Default — acts only on Ethereum Main signals
ALGORITHMS=Ethereum Main

# Multiple algorithms
ALGORITHMS=Ethereum Main, Ethereum Custom RSI
```

If `ALGORITHMS` is omitted, the bot defaults to `Ethereum Main`. A signal that carries no algorithm metadata at all is passed through unchanged (backward compatible with older signal formats).

---

## Telegram Commands

| Command | Description |
|---|---|
| `/status` | Open positions (size, entry, leverage, uPnL) or available balance if flat |
| `/position` | Individual trade records with TP/SL prices, funding, and liquidation price |
| `/pause` | Stop processing incoming signals (open positions are unaffected) |
| `/resume` | Resume signal processing |
| `/history` | Last 10 closed trades with net PnL after fees |
| `/close <N\|all>` | Close trade number N or all open positions |
| `/stats` | Performance dashboard: win rate, avg win/loss, best/worst trade, avg hold time |
| `/stats week` | Same, filtered to last 7 days |
| `/stats month` | Same, filtered to last 30 days |
| `/signal` | Recent signal log: filled, rejected, and errored entries |
| `/help` | List all commands |

---

## Checking Logs & Status

### Is the bot healthy?

```bash
systemctl --user status badgerbot   # non-root
systemctl status badgerbot          # root
```

| What you see | Meaning |
|---|---|
| `Active: active (running)` | Bot is running normally |
| `Active: activating (auto-restart)` | Crashed and is restarting — check logs |
| `Active: inactive (dead)` | Stopped — not running |
| `status=203/EXEC` | Python or path not found — check `ExecStart` path in the service file |
| `status=1` | Bot started but exited with an error — check logs |

### Reading the logs

```bash
journalctl --user -u badgerbot -f        # live log stream (non-root)
journalctl --user -u badgerbot -n 100    # last 100 lines (non-root)
journalctl -u badgerbot -f               # live log stream (root)
journalctl -u badgerbot -n 100           # last 100 lines (root)
```

| Log line | Meaning |
|---|---|
| `Starting signal consumer, position monitor, and Telegram bot...` | All good |
| `Listening for signals on wss://...` | Connected to BadgerBot signal feed |
| `WebSocket disconnected` | Signal feed dropped — bot will reconnect automatically |
| `Signal feed offline` | No message received for 5+ minutes — check your API key |
| `Entry filled @ ...` | Trade opened successfully |
| `POSITION UNPROTECTED` | Trade opened but TP/SL placement failed — close it manually |
| `Market order failed` | Order rejected by Hyperliquid — check account balance and API key |
| `Missing required environment variables` | `.env` is incomplete — check all required vars are set |
| `Failed to connect to Hyperliquid` | Network issue or wrong API URL |

### Common issues

**Bot restarts every 10 seconds (`status=203/EXEC`)**
The Python path in the service file is wrong. Re-run `bash install-service.sh` from inside the `badgerbot-hyper` directory.

**"User or API Wallet does not exist"**
The API wallet isn't approved. Go to [app.hyperliquid.xyz/API](https://app.hyperliquid.xyz/API) and approve it.

**No signals appearing**
Check `/signal` in Telegram. If it shows nothing since restart, verify `BADGERBOT_API_KEY` in `.env` and that your BadgerBot subscription is active.

**Telegram bot not responding**
Confirm `TELEGRAM_BOT_TOKEN` and `TELEGRAM_AUTHORIZED_USER_ID` are correct. The bot only responds to the authorized user ID — messages from other accounts are silently ignored.

---

## Leverage Configuration

Edit `config/coin_leverage.json` to set leverage per coin:

```json
{
  "BTC": 10,
  "ETH": 10,
  "SOL": 10,
  "BNB": 3,
  "AVAX": 3,
  "DEFAULT": 3
}
```

Any coin not listed uses the `DEFAULT` value. Restart the bot after making changes.

---

## Changelog

**v0.6 — April 20, 2026**

- Batched close notifications: when multiple TP/SL orders fill in a single poll cycle, PositionMonitor now sends one aggregate Telegram message per coin — showing trade count, total size, entry range, avg exit, and summed realized PnL — instead of one message per trade. Per-trade messages were misleading because HL computes `closedPnl` against the position-average entry, so the fill's PnL attributed to a single DB trade didn't reconcile with that trade's own `entry → exit`. Aggregate PnL now matches what you see on the Hyperliquid dashboard.
- Stop-loss erosion check: signals are dropped if the mark price has moved toward the SL such that remaining stop room is below `MAX_PRICE_DEVIATION_PCT` of the original SL distance — mirroring the existing TP erosion check. Prevents entering a trade that's already too close to being stopped out.

**v0.5 — April 14, 2026**

- Switched from market order entry with separately placed TP/SL to an atomic entry limit order + TP/SL via `normalTpsl` grouping. Entry and both trigger orders are submitted in a single request, giving each lot its own independent OCO pair. This replaces the old flow where HL rejected standalone trigger orders placed after entry with "Main order cannot be trigger order."
- Entry limit slippage reduced from 2% to 0.1%. The limit price is purely a ceiling to guarantee immediate fill — actual fill happens at the best available ask.
- Re-introduced `MAX_SIGNAL_AGE_SECONDS` (default: 60s) to guard against bot-side processing lag causing stale signal entries.
- Fixed: signal is now dropped if the mark price is already at or past the TP at validation time (LONG: mark ≥ TP, SHORT: mark ≤ TP). Prevents entering a position where the entry price has slipped past the take profit target.

**v0.4 — April 11, 2026**

- Adjusted MAX_PRICE_DEVIATION_PCT functionality to ensure not entering with a to low profit potential. If distance between original take profit and entry price decreases by 50% (0.5), no entry is executed.
- Removed old MAX_PRICE_DEVIATION_PCT system which prohibited entry if entry price deviates 1% from original entry price. Danger is when the original take profit is below 1%.
- Added custom take profit & stop loss logging for every single trade per candle, not each group of trades.
- Added Telegram commands to close / check trades with deviated or no TP / SL's. Use /unprotected & /unprotected_close for this.

**v0.3 — March 30–31, 2026**

- Net PnL tracking: trading fees (entry + close) stored per trade. All PnL values in `/stats`, `/history`, `/close`, and close notifications show net PnL after fees.
- Existing TP/SL trades backfilled with estimated 0.025% taker fee. Entry fees captured automatically on new trades.
- Close fee captured from HL fill data on both auto-close (TP/SL) and manual close paths.
- Fixed fill-to-trade matching: now matches by fill size before TP/SL price proximity, preventing cross-assigned PnL when multiple trades close simultaneously.

**v0.2.3 — March 30, 2026**

- Sweep-cancel orphaned trigger orders after closing a residual position. Prevents stale SL orders from flipping the position.

**v0.2.2 — March 28, 2026**

- Auto-close residual positions: when all TP/SL orders fill but a small remainder exists due to size rounding, PositionMonitor closes it automatically.

**v0.2.1 — March 28, 2026**

- Auto-cancel counterpart TP/SL orders when one side fills.
- Exponential backoff on PositionMonitor fill retries.
- GitHub Actions CI: syntax and import checks on every push.

**v0.2 — March 2026**

- Risk-based position sizing (`RISK_PCT`) with 3-second signal batching.
- Fixed margin mode (`POSITION_SIZE_USD`).
- $10 minimum notional bump.
- PnL percentages based on account equity.
- Telegram command menu with autocomplete.
