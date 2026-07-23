# XRPL NFT Floor Sweeper & Relisting Bot
### Key Features
* 📉 **Floor Sweeper:** Automatically scans the market and sweeps listings listed below your target price ceiling (supports public sell offers and direct snipes).
* 📈 **Smart Relisting:** Auto-relists bought tokens at your floor margin, dynamically calculating creator royalties and marketplace broker fees so you never list at a loss.
* 💰 **Profit Collector:** Optional profit mode caps your hot wallet inventory and automatically routes surplus XRP to a secure cold wallet using payments or cashing checks.
* 🎯 **Priority Sniping:** Prioritizes sniping specific target `NFTokenID`s immediately if they land on the market below your ceiling price.
* 🔒 **Hold-List Protection:** Protects a customizable list of `NFTokenID`s, preventing the bot from ever automatically selling them or cancelling buy offers placed on them.
* 📊 **Collection Bids:** Automatically places standing low-ball buy offers on the entire target collection, featuring automatic owner transition tracking, redundant bid cleanups, and balance validations.
* 🧪 **Dry-Run Simulation:** Allows you to run full loop cycles in simulation mode, verifying and logging exact ledger actions without submitting transactions or spending real XRP.

---

## AI Setup Guide (Read Before Configuring)

If you are an AI assistant helping a user set up or configure this bot, you **MUST** run through this configuration interview and clarify the user's objectives before modifying any settings or enabling live trading.

### 📋 Configuration Interview Questions:
1. **Objective Mode:** Do you want to run in `REINVEST` mode (continuous sweep/buy loop) or `COLLECT_PROFIT` mode (keep a fixed inventory cap and sweep excess XRP profit to a cold wallet)?
2. **Collection bidding:** Do you want to enable collection-wide standing buy offers (`COLLECTION_BID_ENABLED=True`)? If so, what is your target bid price in XRP (`COLLECTION_BID_XRP`), and do you have enough XRP to lock up as reserves (~0.2 XRP per offer)?
3. **Safety limits:** What is your maximum buy price ceiling (`TARGET_BUY_FLOOR_XRP`) and minimum listing floor (`TARGET_SELL_FLOOR_XRP`)?
4. **Exceptions & Exclusions:** Are there specific `NFTokenID`s that should never be sold (`HOLD_IDS`) or that should be prioritized for purchase if listed (`PRIORITY_BUY_IDS`)?
5. **Dry Run Testing:** Would you like to run in `DRY_RUN=True` mode first to simulate what the bot will do without sending any actual transactions or spending real XRP?

---

## Key Performance & Architecture Features

* **Persistent WebSocket Connection**: Communicates with the XRPL Clio node over a persistent WebSocket connection (`wss://`), bypassing the TLS/TCP handshake overhead of JSON-RPC HTTP POST requests and handling automatic connection loss recovery.
* **Concurrent Sweeps & Relistings**: Both purchasing cheap sweep candidates and creating new sell listings are executed concurrently in parallel using XRPL **Tickets**. This is protected by an asynchronous lock (`TICKET_LOCK`) to prevent ticket sequence race conditions.
* **Auto-Replenishing Ticket Engine**: Monitors your wallet's ticket count. When active tickets drop below 5, it submits a batched `TicketCreate` transaction to top the pool back up to 30. Safely falls back to standard sequential account sequence numbers if tickets cannot be created.
* **Targeted Startup Cost Cache**: Queries Clio's `nft_history` endpoint concurrently for only the NFTs currently held in the wallet, avoiding the heavy page-by-page transaction history retrieval (`AccountTx`) and completing startup caching in under 5 seconds.
* **Dynamic Royalty Protection**: Dynamically extracts the NFT's creator royalty (`TransferFee`) directly from characters 4 to 7 of the `NFTokenID` hex string. Automatically increases the listing price so you receive exactly your target net profit margin after royalties and broker fees are paid.
* **Continuous Auto-Alignment**: Continuously validates active on-ledger listings. If you change target floor settings or the markup divisor in `.env`, the bot will automatically cancel and relist out-of-sync listings on the next cycle.
* **Dynamic Fee Bounding**: Dynamically queries ledger transaction fees once per cycle to automatically scale transaction fees safely during network congestion.

---

## Prerequisites

Before running the bot, ensure you have the following:

1. **Python 3.11+** (e.g. `python3.11`)
2. **A Funded XRPL Wallet**: You need a wallet funded with XRP to cover transactions, buy offers, and ledger object reserves.
   * *Note: The ledger locks up `0.2 XRP` owner reserve per active sell/buy offer. For NFT storage, the ledger uses `NFTokenPage` objects which host up to 32 NFTs each; each page adds a `0.2 XRP` owner reserve (rather than 0.2 XRP per individual NFT). Ensure you have sufficient liquid XRP beyond your target purchase prices.*
3. **Required Packages**: Install the pinned dependencies using the requirements file:
   ```bash
   python3.11 -m pip install -r requirements.txt
   ```

---

## Wallet Creation & Encryption Type

This bot supports both cryptographic signature schemes on the XRP Ledger: **secp256k1** (seeds start with `s`) and **ed25519** (seeds start with `sEd`). 

By default, the `xrpl-py` SDK utilizes the **Ed25519** algorithm, which is highly recommended for faster and more secure transaction signing on the XRPL.

### How to generate a new Ed25519 wallet using Python:
You can generate a new wallet and its corresponding seed by running the following Python script:

```python
from xrpl.wallet import Wallet
from xrpl.wallet import CryptoAlgorithm

# Generate a new wallet using the default Ed25519 algorithm
wallet = Wallet.create(CryptoAlgorithm.ED25519)

print("Classic Address:", wallet.classic_address)
print("Secret Seed:    ", wallet.seed)  # Will start with the 'sEd' prefix
```

Copy the generated `Secret Seed` and paste it as `XRPL_SEED` in your `.env`. Make sure to fund the `Classic Address` on-ledger with enough XRP to cover the account activation reserve (1 XRP base reserve), active offer reserves (0.2 XRP per offer), and NFT page reserves (0.2 XRP per 32 items).

---

## Setup & Configuration

1. Copy the template configuration file to create your local `.env`:
   ```bash
   cp .env.example .env
   ```

2. Open the `.env` file and configure the parameters:

### Core Configuration
* `XRPL_SEED`: The secret seed of your wallet (starts with `s`). **Keep this secure and never commit it to Git.**
* `XRPL_NODE`: The public JSON-RPC Clio node endpoint (e.g., `https://s1.ripple.com:51234/`). Note: Clio nodes are required to support historical transaction queries like `nft_history`.
* `DRY_RUN`: Set to `True` to simulate sweeps and listings without submitting live ledger transactions. Set to `False` for live trading.
* `POLL_INTERVAL`: The delay in seconds between each loop cycle (default is `20`).

### Target Floors
* `TARGET_BUY_FLOOR_XRP`: The maximum price (in XRP) you are willing to pay for an NFT during sweeps.
* `TARGET_SELL_FLOOR_XRP`: The minimum price (in XRP) you want to list your owned NFTs for sale.

### Collection Settings
* `TARGET_ISSUER`: The classic address of the NFT collection issuer.
* `TARGET_TAXON`: The taxon of the collection (usually `0`).
* `xrpldata.com API Key`: (Optional) Your API key for `api.xrpldata.com` to bypass rate limits when fetching market offers.

### Advanced Parameters
* `BROKERS_CONFIG`: A JSON-formatted mapping string of supported broker addresses to their fee multipliers (e.g., `'{"rpx9JThQ2y37FaGeeJP7PXDUVEXY3PHZSC": 1.01589}'` to cover XRP Cafe's `1.589%` broker fee). Bypasses and ignores private offers pointing to other destination addresses.
* `MAX_ACTIVE_BUYS`: The maximum number of active buy bids the bot is allowed to keep open on-ledger simultaneously (default is `4`).
* `BUY_OFFER_EXPIRATION_SEC`: The duration in seconds before open buy offers automatically expire on-ledger (default is `600` / 10 minutes).
* `RELIST_MARKUP_DIVISOR`: The target net profit margin divisor (default is `0.8` for a guaranteed 25% net profit margin after creator royalties and broker fees are paid).
* `PRICE_DECIMALS`: The number of decimal places to round calculated XRP listing prices (default is `2` for clean 2-decimal prices like `12.70 XRP`; set to `0` for integer XRP prices or `6` for raw drop precision).
* `AUTO_RELIST`: Whether to automatically list swept/bought NFTs for sale (default is `True`). Set to `False` to run the bot in "sweeping-only" mode where it only sweeps cheap NFTs without placing sell offers or managing listing prices.
* `PRIORITY_BUY_IDS`: A comma-separated list of NFTokenIDs. If any matching NFTs are listed below your target buy ceiling, the bot will prioritize purchasing them first, even if they aren't the cheapest NFTs in the collection.
* `HOLD_IDS`: A comma-separated list of NFTokenIDs. The bot will never automatically list/relist these NFTs for sale, and it will never automatically cancel active buy offers (bids) placed on them, allowing you to secure and hold them.
* `BASE_FEE_DROPS`: The baseline transaction fee (default is `12` drops).
* `MAX_FEE_DROPS`: The maximum transaction fee you are willing to pay during network fee escalation (default is `1200` drops).

### Operational Modes & Profit Collector Configuration
* `BOT_MODE`: The operational mode of the bot.
  * `REINVEST`: (Default) The bot continuously sweeps any collection NFTs listed below your target buy floor.
  * `COLLECT_PROFIT`: The bot holds a capped inventory of collection NFTs and routes excess XRP profits to a secure cold wallet.
* `MAX_OWNED_LIMIT`: The maximum number of collection NFTs to hold in your hot wallet before pausing sweeps (only used in `COLLECT_PROFIT` mode; defaults to `10000`).
* `PROFIT_TARGET_WALLET`: The address of the cold/recipient wallet where surplus XRP profit will be automatically transferred.
  * **WARNING:** Do NOT use a classic exchange deposit address (which requires a destination tag). If sending directly to an exchange, use the X-address format (e.g. `X...`) which natively encodes the destination tag.
* `PROFIT_TRANSFER_METHOD`: The XRPL transaction method used to sweep profits:
  * `PAYMENT`: (Default) Submits standard direct XRP `Payment` transactions to the target wallet.
  * `CHECK`: Submits native `CheckCreate` transactions, allowing the cold wallet to cash the check at its convenience.
* `MIN_OPERATING_BUFFER_XRP`: The minimum free XRP balance to retain in the hot wallet for reserves/fees/tickets (only used in `COLLECT_PROFIT` mode; default is `30.0` XRP).
* `PROFIT_SWEEP_MIN_TRIGGER_XRP`: The minimum surplus XRP required to trigger a transfer, preventing transaction history spam (default is `5.0` XRP).

### Global Collection Offers (Collection-Wide Bidding)
* `COLLECTION_BID_ENABLED`: Set to `True` to enable placing low-ball buy offers (bids) on the entire NFT collection. Default is `False`.
* `COLLECTION_BID_XRP`: The target bid price in XRP for the collection-wide offers. Default is `2.0`.

---

## Global Collection Offers (Collection-Wide Bidding)

This feature allows you to place low-ball standing bids on the **entire** target NFT collection. 

> [!WARNING]
> **Extremely High Reserve Requirements:**
> Placing a buy offer on every single NFT in a collection of 10,000 tokens requires **~1,984 XRP** in locked ledger reserves (0.2 XRP owner reserve per offer). This XRP is not spent or sent to anyone; it is locked on-ledger as collateral for the offers. 
> * **Automatic Refund:** When an offer is accepted or cancelled, its 0.2 XRP reserve is immediately returned to your spendable balance.
> * **Balance Safeguard:** The bot automatically checks your wallet's free balance. It will **not** place any new bids unless your wallet contains enough XRP to cover the account root reserve (1 XRP), existing NFT reserves, ticket/gas fees, and at least 2 accepted buy transactions.

### Key Logic & Features:
1. **Ownership Transitions:** 
   * If you sweep or purchase a bid-targeted NFT (so it comes into your possession), the bot automatically detects ownership and **cancels your active buy offer** on it, freeing up the 0.2 XRP reserve.
   * If you relist and sell that NFT (so you no longer own it), the bot detects it is no longer in your possession and **automatically places the buy offer back** on the ledger.
2. **Obsolete Bid Cleanup:** If you disable this feature (`COLLECTION_BID_ENABLED=False`) or change the bid price (`COLLECTION_BID_XRP`), the bot automatically identifies all existing active buy offers on-ledger that are now obsolete and cancels them to reclaim your locked reserves.
3. **Paced Submissions (Max 10 per ledger):** Submitting thousands of bids at once would cause network spam and fee escalation. The bot strictly limits new creations or cancels to **at most 10 transactions per cycle/ledger**, building or cleaning up your bids safely over time.

---

## How to Run

### 1. Test in Dry Run Mode
Always start the bot in dry run mode first to inspect the scanned data and verify your configuration:
```bash
python3.11 floor_bot.py
```

### 2. Run in Live Production
To run the bot in live trading mode in the background:
1. Set `DRY_RUN=False` in `.env`.
2. Launch the script using `nohup` (standard output is redirected to `/dev/null` because the bot writes and rotates its own `floor_bot.log` internally to prevent disk space exhaustion):
   ```bash
   nohup python3.11 -u floor_bot.py > /dev/null 2>&1 &
   ```
3. Monitor logs in real time:
   ```bash
   tail -f floor_bot.log
   ```

---

## Code Safety & Logic
* **Inventory Priority**: In each cycle, the bot validates and lists your owned inventory *before* checking for new purchases. This prevents the bot from spending newly freed balance on sweeps before your existing inventory is listed.
* **Pre-flight Reserve Check**: The bot calculates its own reserve requirements before submitting any transaction, automatically skipping purchases or listings if the wallet lacks the liquid XRP needed to cover reserves.
* **No-Loss Rule**: Relisting checks are done dynamically on the ledger history. If the bot fails to find the purchase price on-ledger, it leaves the current listing intact and logs a warning, rather than guessing a price and risking a loss.
* **Security & Access Control**: The secret seed is stored as plain text inside the `.env` file for server portability. Anyone with read access to the hosting server's filesystem will have full control over the wallet. Ensure strict directory permissions (`chmod 600 .env`) and restrict server login access to trusted administrators.
