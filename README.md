# Vibhu Price Sniper

Monitors the public Flipkart deal feed exposed by PriceHistory and sends Telegram alerts when a deal meets:

- Flipkart
- current price <= ₹5,000
- current price <= 50% of the displayed historical/typical price baseline
- duplicate alerts suppressed

It does NOT place orders automatically. The Telegram alert contains the Flipkart link for manual checkout.

## Setup

1. Create a GitHub repository and upload this project.
2. In GitHub: Settings → Secrets and variables → Actions → New repository secret.
3. Create:
   - `TELEGRAM_BOT_TOKEN` = the token from @BotFather for `@Vibhuprice_bot`
   - `TELEGRAM_CHAT_ID` = your personal Telegram chat ID
4. Enable Actions.
5. Run the workflow manually once from Actions → Vibhu Price Sniper → Run workflow.

The workflow is scheduled every 15 minutes. GitHub scheduled jobs can occasionally be delayed.

## Important

This project uses the public PriceHistory Flipkart deal page rather than a Buyhatke account/API. It does not bypass authentication or CAPTCHA and does not automatically submit Flipkart orders.

The public page structure can change. If PriceHistory changes its HTML, the parser may need updating.
