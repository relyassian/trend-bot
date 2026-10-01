# Trend Bot — setup guide

**What it does:** every weekday morning it checks whether the S&P 500 is above its 200-day average.
- Above → whole account in **SSO** (2x S&P 500).
- Below → whole account in **BIL** (T-bills).
- Every run, any new cash (deposits, dividends) gets invested into whatever it's holding.
- Messages you on Telegram for every trade, a daily note, and a Friday weekly summary.

It starts in **paper mode** (fake money). Nothing real is traded until you change one setting.

Setup takes about 30–45 minutes, on a computer. Do the steps in order.

---

## 1. Alpaca paper account (10 min)
1. **Sign up at alpaca.markets.** Paper trading is free and needs no deposit.
2. **Open the Paper dashboard** (toggle at the top of the dashboard).
3. **Optional: create a paper account with a realistic balance** (for example $1,000) so the Telegram numbers feel real.
4. **Generate API keys** on the Paper dashboard home page. Copy the **Key** and **Secret** somewhere safe. The secret is only shown once.

## 2. Telegram bot (5 min)
1. **In Telegram, message `@BotFather` and send `/newbot`.** Pick any name. It replies with a **token** like `123456:ABC...`.
2. **Open your new bot and send it any message** (e.g. "hi").
3. **In a browser, open** `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` (replace `<YOUR_TOKEN>`).
4. **Find `"chat":{"id":` and copy the number after it.** That's your **chat ID**.

## 3. GitHub (15 min)
GitHub runs the bot on a schedule for free. You don't need any server.
1. **Create a free account at github.com, then a new repository. Set it to Private.**
2. **Upload all the files from this folder,** keeping the `.github/workflows/bot.yml` path exactly as is. (On the repo page: "Add file → Upload files", drag the whole folder in.)
3. **Go to Settings → Secrets and variables → Actions → New repository secret,** and add these four:
   - `ALPACA_KEY`: your Alpaca paper key
   - `ALPACA_SECRET`: your Alpaca paper secret
   - `TELEGRAM_TOKEN`: the BotFather token
   - `TELEGRAM_CHAT_ID`: the number from step 2.4
4. **Go to the Actions tab and enable workflows** if GitHub asks.

## 4. Test it (5 min)
1. **In the Actions tab, click "trend-bot" → "Run workflow", leave "Dry run" checked, and run it.**
   - It must be during market hours (9:30am–4pm ET, weekdays), or it'll just say the market is closed.
2. **You should get a Telegram message** saying what it *would* do. If you see that, every connection works.
3. **Run it once more with "Dry run" unchecked** to place the first paper trade, or just wait. It runs automatically every weekday around 10am ET.

---

## Going live later (after ~3 months of paper trading)
1. Open an IRA at Alpaca (Roth or Traditional) and fund it.
2. Generate **live** API keys and replace `ALPACA_KEY` / `ALPACA_SECRET` with them.
3. In **Settings → Secrets and variables → Actions → Variables**, add `ALPACA_PAPER` = `false`.
4. Set up an automatic monthly deposit. The bot invests it on its next run.

**Things to confirm when you open the IRA:** any minimum opening deposit, and that SSO can be bought in fractional shares (the bot handles either case and will tell you on Telegram if cash is waiting).

## Good to know
- **Expect about 5 switches a year,** more in choppy markets. Most days it just confirms it's holding.
- **It only touches SSO, BIL, and cash.** Anything else in the account is left alone.
- **If something fails, you get a ❌ message on Telegram,** and it tries again the next weekday.
- **Don't trade in the account yourself.** That'll confuse it.
- To change the rule (e.g. 250-day average), add a repository variable later. Ask Claude.

## Files
- `bot.py`: the bot
- `test_bot.py`: offline tests (`pip install -r requirements.txt pytest && pytest`)
- `.github/workflows/bot.yml`: the weekday schedule
- `requirements.txt`: dependencies
