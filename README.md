# XRPL NFT Floor Sweeper & Relisting Bot

> **Note**: This bot is designed for NFT projects to maintain a price floor on their project, or for "whales" looking to manage and protect their collection floors on the XRP Ledger.

This automated bot runs in the background to monitor a specific XLS-20 NFT collection on the XRP Ledger. It performs two key operations:
1. **Sweeping**: Scans the market and automatically accepts public sell offers (or submits bids for brokered offers) for any NFTs listed below your target buy ceiling.
2. **Relisting**: Scans your owned inventory, dynamically retrieves the historical purchase price of each NFT from the ledger, and ensures they are listed for sale at your target sell floor (or higher, using a customizable profit margin to prevent selling at a loss).

---

## Prerequisites

Before running the bot, ensure you have the following:

1. **Python 3.7+**
2. **A Funded XRPL Wallet**: You need a wallet funded with XRP to cover transactions, buy offers, and ledger object reserves.
   * *Note: The ledger locks up `0.2 XRP` owner reserve per active sell/buy offer. For NFT storage, the ledger uses `NFTokenPage` objects which host up to 32 NFTs each; each page adds a `0.2 XRP` owner reserve (rather than 0.2 XRP per individual NFT). Ensure you have sufficient liquid XRP beyond your target purchase prices.*
3. **Required Packages**: Install the XRPL Python SDK and environment variables loader:
   ```bash
   pip install xrpl-py python-dotenv requests
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
* `RELIST_MARKUP_DIVISOR`: The margin divisor to protect listings from selling at a loss (default is `0.9` for a ~11% profit margin check).

---

## How to Run

### 1. Test in Dry Run Mode
Always start the bot in dry run mode first to inspect the scanned data and verify your configuration:
```bash
python3 floor_bot.py
```

### 2. Run in Live Production
To run the bot in live trading mode in the background:
1. Set `DRY_RUN=False` in `.env`.
2. Launch the script using `nohup` so it continues running when you close your terminal:
   ```bash
   nohup python3 -u floor_bot.py > floor_bot.log 2>&1 &
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
