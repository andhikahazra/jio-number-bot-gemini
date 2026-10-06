# Grizzly SMS Automator for MyJio (Gemini Promo)

Automated script to request Indian virtual phone numbers for MyJio via [Grizzly SMS](https://grizzlysms.com/docs), filter by required Jio prefixes (`600`, `939`, `700`, `701`, `702`, etc.), and wait for the OTP verification code.

## Key Features

- **Prefix Matching**: Automatically checks incoming numbers for target Jio prefixes (default: `600,939,700,701,702` or choose presets like `tier1`, `popular`, `all`).
- **Simultaneous Parallel Requests & Candidate Queue (2 Requests per Attempt)**:
  - By default, each attempt fires **2 requests simultaneously in parallel** to Grizzly SMS using concurrent worker threads (`--parallel`, default: true).
  - If multiple numbers match the target prefixes, **they are all added to your testing queue**.
  - Number 1 is immediately presented in the terminal and Telegram (with 1-tap copy).
  - If Number 1 is not eligible and skipped, **Number 2 is immediately loaded from the queue** without waiting for new API calls!
  - If Number 1 succeeds with an OTP, Number 2 is safely cancelled/refunded in the background so you never lose balance.
- **Interactive Skip to Next Number**:
  - Whenever a number is found and not working in MyJio, press `[Enter]` on your keyboard or tap `[⏩ Next Number (Skip)]` on Telegram.
  - The script immediately cancels that specific number and requests/loads a fresh number without discarding the whole prefix range (since other numbers under the same prefix can still be eligible!).
- **Telegram 1-Tap Copy Number & Controls**:
  - Direct `📋 Copy Number` button in Telegram chat that copies the 10-digit mobile number to your phone's clipboard.
  - Shows queue position (e.g. `Queue: 1/2`) and remaining candidates.
  - Dedicated quick-copy message bubble with just the number for instant pasting into MyJio.
  - Direct `📋 Copy OTP` button for instant OTP copying when the SMS arrives.
  - Interactive buttons `[✅ Working (Wait OTP)]`, `[⏩ Next Number (Skip)]`, `[⏸️ Pause Automator]`, and `[🛑 Stop Automator]`.
- **Pause & Resume Anytime**:
  - Need to take a break or stop purchasing numbers temporarily? Tap `⏸️ Pause Automator` or send `/pause` (or press `p` in terminal) to pause searches immediately without terminating the process.
  - Active and queued numbers are safely cancelled & refunded so your funds are never lost.
  - Tap `▶️ Resume Automator` or send `/resume` (or `/start` / press `[Enter]` in terminal) whenever you are ready to start searching again!
  - Supports `--start-paused` (or `START_PAUSED=true` in `.env`) to launch the bot in standby mode until you trigger it.
- **2-Minute Background Cleanup & Non-Blocking Search**:
  - Grizzly SMS enforces a 2-minute cooldown before cancellations are accepted (`EARLY_CANCEL_DENIED`).
  - Discarded numbers are automatically queued for background cleanup.
  - A background worker thread cancels them once the 2-minute cooldown passes, restoring your balance without interrupting your ongoing number search!
- **Automatic Balance Waiting & Stale Number Recovery**:
  - If your balance is depleted or tied up (`NO_BALANCE` or balance < max price), the script pauses and waits for background cleanups to finish and refund your funds.
  - Automatically queries and adopts any stale uncancelled activations on your account from previous interrupted runs to ensure no balance is lost.
- **Gentle & Safe ("Do Not Harsh")**:
  - Configurable polite delay (default: 4 seconds) between attempts to prevent API rate limiting or account flags.
  - Unlimited attempts by default (or configure max retries with `MAX_RETRIES` / `--max-retries`) until a working number is found.
  - Graceful interrupt handling: Pressing `Ctrl+C` cleanly cancels active and pending numbers in the queue.
- **Auto OTP Polling**: Once a matching number is assigned, polls for the incoming SMS OTP code and completes the activation (Status `6`).

---

## Setup & Usage

### 1. Requirements

Ensure you have Python 3 and the `requests` library:

```bash
pip install requests
```

### 2. Configuration

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Open `.env` and fill in your Grizzly SMS API Key:

```env
GRIZZLY_API_KEY=your_actual_api_key_here

# (Optional) Telegram bot alerts and interactive controls:
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
TELEGRAM_ALL_MESSAGES=false
```

### 3. Telegram Notifications & Interactive Controls (Optional)

Receive real-time notifications on Telegram with 1-tap copy and interactive buttons:
- 📱 **1-Tap Copy Number**: Direct `📋 Copy Number` button copies the 10-digit number to your phone's clipboard. Plus, a standalone monospace number bubble for instant tap-to-copy.
- 🔘 **Interactive Buttons**: Tap `[✅ Working (Wait OTP)]`, `[⏩ Next Number (Skip)]`, or `[🛑 Stop Automator]` without touching your terminal.
- 💬 **Bot Commands**: Reply `/yes` to wait for OTP, or `/skip` / `/next` / `/no` to get the next number.
- 🔑 **1-Tap Copy OTP**: As soon as the SMS arrives, tap `📋 Copy OTP` to copy the code.

1. **Create Bot**: Message [@BotFather](https://t.me/botfather) on Telegram and type `/newbot` to get your `TELEGRAM_BOT_TOKEN`.
2. **Find Your Chat ID**: Run the built-in detector:
   ```bash
   python3 grizzly_myjio.py --get-chat-id --telegram-bot-token "YOUR_BOT_TOKEN"
   ```
   Open Telegram, search for your bot, and tap **Start** (or send any message). The script will automatically detect and print your `TELEGRAM_CHAT_ID`.
3. **Verify Connection**:
   ```bash
   python3 grizzly_myjio.py --test-telegram
   ```

### 4. Running the Bot

#### Option A: Real-Time Web Dashboard (Next.js + FastAPI)
Launch the modern Web Dashboard and open it on your PC or phone browser:

```bash
# Launch directly via CLI:
python grizzly_myjio.py --web

# Or run the dedicated server runner:
python -m server.run --port 8000
```
Open **`http://localhost:8000`** on your computer, or **`http://<your-ip>:8000`** on your smartphone connected to the same network.
- 📱 **1-Tap Copy Number**: Tap to copy directly to your phone clipboard.
- 🔔 **Audio Chime & OTP Alert**: Instant audible alert and popup when SMS arrives.
- ⚡ **Tactile Controls**: Skip, Pause, Resume, Stop, and adjust prefixes without restarting.
- 📊 **Cooldown Monitor & Logs**: Real-time 120s refund queue counter and live terminal logs.

#### Option B: Terminal CLI / Headless Mode
Run using default settings in your terminal:

```bash
python grizzly_myjio.py
```

Or pass your API key, Telegram settings, and parameters directly via CLI:

```bash
# Run with Telegram notifications enabled:
python3 grizzly_myjio.py --telegram-bot-token "YOUR_TOKEN" --telegram-chat-id "YOUR_CHAT_ID"

# Using strict tier (default):
python3 grizzly_myjio.py --api-key "YOUR_KEY" --preset tier1

# Using popular tier (recommended for faster matching):
python3 grizzly_myjio.py --api-key "YOUR_KEY" --preset popular

# Using all known Jio ranges:
python3 grizzly_myjio.py --api-key "YOUR_KEY" --preset all

# Or provide custom comma-separated prefixes:
python3 grizzly_myjio.py --api-key "YOUR_KEY" --prefixes "600,939,700,701,702,620,630"

# If Grizzly SMS server is slow or experiencing high latency:
python3 grizzly_myjio.py --request-timeout 90

# View all supported prefix tiers and catalog:
python3 grizzly_myjio.py --list-prefixes
```

### 5. Step-by-Step Flow

1. Connect your VPN / Proxy to **India**.
2. Run `python3 grizzly_myjio.py`.
3. The script verifies your balance and finds a number matching one of your active Jio prefixes.
4. Copy the displayed number (or tap **Copy Number** in Telegram) and paste it into the **MyJio** app.
   - *If the number is not eligible or promo already used*: Press `[Enter]` in your terminal or tap `[⏩ Next Number (Skip)]` in Telegram to request a new number.
5. Request OTP in MyJio.
6. The script automatically catches the incoming OTP, displays it on terminal, and sends it to Telegram with a **Copy OTP** button.
7. Enter the OTP in MyJio and claim your offer!

### 5. Supported Jio Prefixes Reference

View directly in terminal at any time via `python3 grizzly_myjio.py --list-prefixes`.

- **Tier 1 (Default / Strict - 5 prefixes)**:
  `600, 939, 700, 701, 702`
  - *Best for*: Guaranteed Jio promo eligibility. Original nationwide Jio allocations.
  - *Activate via*: `--preset tier1` or `PREFIX_PRESET=tier1`.

- **Tier 2 (Popular / Moderate - 35 prefixes total)**:
  Includes all Tier 1 prefixes plus:
  - **6-series**: `620, 626, 628, 629, 630, 635, 636, 637, 638, 639`
  - **7-series**: `703, 704, 705, 706, 707, 708, 709, 790, 797, 798, 799`
  - **93-series**: `930, 931, 932, 933, 934, 935, 936, 937, 938`
  - *Best for*: Finding valid Jio numbers much faster on high-traffic days.
  - *Activate via*: `--preset popular` (or `--preset tier2`) or `PREFIX_PRESET=popular`.

- **Tier 3 (Extended / Comprehensive - 82 prefixes total)**:
  Includes all Tier 1 and Tier 2 prefixes plus:
  - **92-series (Reliance CDMA to Jio LTE migrated blocks)**: `920, 921, 922, 923, 924, 925, 926, 927, 928, 929`
  - **7-series circle allocations**: `740, 741, 742, 750, 751, 752, 760, 761, 762, 763, 764, 765, 766, 767, 786, 787, 788, 789`
  - **8-series Jio LTE allocations**: `821, 822, 824, 827, 828, 829, 843, 844, 846, 847, 848, 849, 877, 878, 879, 896, 897, 898, 899`
  - *Best for*: Maximum availability when Grizzly SMS has limited standard Jio numbers in stock.
  - *Activate via*: `--preset all` (or `--preset tier3`) or `PREFIX_PRESET=all`.

---

## Docker & VPS Deployment

Run the bot 24/7 on your VPS using Docker and Docker Compose with automatic restarts, memory limits, and log rotation.

### 1. Quick Start with Docker Compose

1. Clone or copy this repository to your VPS.
2. Create and configure your `.env` file:
   ```bash
   cp .env.example .env
   nano .env
   ```
3. Start the bot in the background:
   ```bash
   docker compose up -d
   ```

### 2. Live Monitoring & Logs

Monitor the bot in real time with unbuffered output:

```bash
# Stream live logs
docker compose logs -f

# Check last 100 log lines
docker compose logs --tail=100 -f

# View container status and resource usage
docker compose ps
docker stats myjio-bot
```

### 3. Managing the Container

```bash
# Restart the bot
docker compose restart

# Stop the bot (cleanly releases lock and pending numbers)
docker compose stop

# Stop and remove containers
docker compose down

# Rebuild container after code changes
docker compose build --no-cache && docker compose up -d
```

---

## Telegram Controls & VPS Safety

The automator is built specifically for VPS/Docker headless hosting with 100% synchronized Telegram control:

### 1. Dedicated Telegram Sync Engine
- A dedicated background polling thread continuously listens to Telegram events. Button presses and commands take effect **immediately** with zero dropped commands and instant Telegram pop-up feedback.
- **Continuous OTP Loop**: When an OTP arrives, the code is sent to Telegram with 1-tap copy, the activation is completed, and the runner **automatically continues** searching for new numbers without terminating.

### 2. Interactive Buttons & Commands
| Telegram Button | Command | Description |
|---|---|---|
| `📋 Copy Number` | — | Direct 1-tap copy of the phone number to clipboard |
| `✅ Working (Wait OTP)` | `/yes` or `/working` | Confirms number works in MyJio and waits for OTP |
| `⏩ Next Number (Skip)` | `/skip`, `/next`, `/no` | Discards current number and loads/requests the next candidate |
| `⏸️ Pause Automator` | `/pause` | Pauses number searches; halts purchasing without exiting |
| `▶️ Resume Automator` | `/resume` or `/start` | Resumes automated searching whenever you want to start again |
| `🧹 Sweep Uncancelled` | `/sweep` | Scans account & queue, immediately refunds eligible uncancelled numbers |
| `⛔ Kill Cancellations` | `/kill_cancel` | Emergency pause for all background cancellations |
| `▶️ Resume Cancellations`| `/resume_cancel` | Resumes automated background cancellation queue |
| `📊 Status` | `/status` | Shows live status, balance, queue sizes, and OTP count |
| `🛑 Stop Automator` | `/stop` or `/kill` | Cleanly terminates the bot and cancels pending numbers |
| `ℹ️ Help` | `/help` | Displays available commands and quick guide |

### 3. VPS Safety Features
- **PID File Lock (`.grizzly_myjio.pid`)**: Automatically prevents multiple instances from running at the same time and burning balance.
- **Headless Detection**: Auto-detects VPS/Docker environments where terminal stdin is detached, preventing accidental stdin EOF read loops.
- **Thread-Safe Requests & Graceful Shutdown**: Handles `SIGTERM`, `SIGINT`, and `SIGHUP` smoothly to clean up any active activations before exiting.


