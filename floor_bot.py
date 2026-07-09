import os
import sys
import json
import time
import requests
from dotenv import load_dotenv

# XRPL SDK imports
from xrpl.clients import JsonRpcClient
from xrpl.wallet import Wallet
from xrpl.models.requests import AccountNFTs, NFTSellOffers, AccountObjects, AccountTx
from xrpl.models.transactions import (
    NFTokenAcceptOffer,
    NFTokenCreateOffer,
    NFTokenCreateOfferFlag,
    NFTokenCancelOffer
)
from xrpl.transaction import submit_and_wait

# Load environment variables
load_dotenv()

# Constants
RIPPLE_EPOCH = 946684800

# Configuration variables
XRPL_NODE = os.getenv("XRPL_NODE", "https://s1.ripple.com:51234/")
DRY_RUN = os.getenv("DRY_RUN", "True").lower() == "true"
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "20"))
TARGET_BUY_FLOOR_XRP = float(os.getenv("TARGET_BUY_FLOOR_XRP", "4.5"))
TARGET_SELL_FLOOR_XRP = float(os.getenv("TARGET_SELL_FLOOR_XRP", "5.0"))
XRP_API_KEY = os.getenv("XRP_API_KEY", "").strip()

# NFT Collection Properties
ISSUER = os.getenv("TARGET_ISSUER", "rDropCHANEgmG7FBz1nzPpG27BGzWjnCnn").strip()
TAXON = int(os.getenv("TARGET_TAXON", "0"))

# Broker Fee Multiplier
BROKER_FEE_MULTIPLIER = float(os.getenv("BROKER_FEE_MULTIPLIER", "1.01589"))

# Safety limits & user preferences
MAX_ACTIVE_BUYS = int(os.getenv("MAX_ACTIVE_BUYS", "4"))
BUY_OFFER_EXPIRATION_SEC = int(os.getenv("BUY_OFFER_EXPIRATION_SEC", "600"))
RELIST_MARKUP_DIVISOR = float(os.getenv("RELIST_MARKUP_DIVISOR", "0.9"))

# Convert XRP to drops
TARGET_BUY_FLOOR_DROPS = int(TARGET_BUY_FLOOR_XRP * 1_000_000)
TARGET_SELL_FLOOR_DROPS = int(TARGET_SELL_FLOOR_XRP * 1_000_000)

print("=" * 80)
print("              XRPL NFT FLOOR SWEEPER & RELISTING BOT")
print("=" * 80)
print(f"Node:          {XRPL_NODE}")
print(f"Target Issuer: {ISSUER}")
print(f"Target Taxon:  {TAXON}")
print(f"Max Buy Cap:   {TARGET_BUY_FLOOR_XRP} XRP ({TARGET_BUY_FLOOR_DROPS} drops)")
print(f"Min Sell Floor:{TARGET_SELL_FLOOR_XRP} XRP ({TARGET_SELL_FLOOR_DROPS} drops)")
print(f"Broker Fee Mult:{BROKER_FEE_MULTIPLIER}")
print(f"Max Active Buys:{MAX_ACTIVE_BUYS}")
print(f"Buy Expiration:{BUY_OFFER_EXPIRATION_SEC} seconds")
print(f"Relist Divisor:{RELIST_MARKUP_DIVISOR}")
print(f"Dry Run Mode:  {DRY_RUN}")
print(f"Poll Interval: {POLL_INTERVAL} seconds")

# Wallet Initialization
seed = os.getenv("XRPL_SEED", "").strip()
is_seed_configured = seed and seed != "sYOUR_SECRET_SEED_HERE"

if not is_seed_configured:
    if DRY_RUN:
        print("\n[DRY RUN WARNING] No valid XRPL_SEED configured in .env.")
        print("Generating a temporary random wallet for simulation purposes...")
        wallet = Wallet.create()
        print(f"Simulated Wallet Address: {wallet.classic_address}\n")
    else:
        print("\n[CRITICAL ERROR] Live mode is enabled but no valid XRPL_SEED is configured in .env.")
        sys.exit(1)
else:
    try:
        wallet = Wallet.from_secret(seed)
        print(f"Wallet Address: {wallet.classic_address}\n")
    except Exception as e:
        print(f"\n[CRITICAL ERROR] Failed to initialize wallet from seed: {e}")
        sys.exit(1)

# Establish XRPL client
try:
    client = JsonRpcClient(XRPL_NODE)
except Exception as e:
    print(f"[CRITICAL ERROR] Failed to connect to XRPL node {XRPL_NODE}: {e}")
    sys.exit(1)

def fetch_api_sell_offers():
    """
    Fetch active offers for the collection from XRP Ledger Services.
    """
    url = f"https://api.xrpldata.com/api/v1/xls20-nfts/offers/issuer/{ISSUER}/taxon/{TAXON}"
    headers = {}
    if XRP_API_KEY:
        headers["x-api-key"] = XRP_API_KEY
    
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"[API Error] Failed to fetch collection offers: {e}")
        return None

def get_free_balance():
    """
    Calculate the liquid/free XRP balance of the wallet in drops,
    subtracting base (1.0 XRP) and owner reserves (0.2 XRP per object).
    """
    try:
        from xrpl.models.requests import AccountInfo
        resp = client.request(AccountInfo(account=wallet.classic_address))
        if resp.is_successful():
            data = resp.result.get("account_data", {})
            balance = int(data.get("Balance", 0))
            owner_count = int(data.get("OwnerCount", 0))
            
            # Current XRPL mainnet reserves: 1.0 XRP base, 0.2 XRP per owner object
            base_reserve = 1_000_000  # 1.0 XRP in drops
            owner_reserve = 200_000   # 0.2 XRP in drops
            
            total_reserve = base_reserve + owner_count * owner_reserve
            free_balance = balance - total_reserve
            return free_balance
    except Exception as e:
        print(f"[Warning] Failed to calculate free balance: {e}")
    return 0

def execute_direct_buy(offer_id, nftoken_id, price_drops):
    """
    Purchase an NFT directly from a public sell offer.
    """
    # Hard Safety Limit: Never buy for more than the target buy floor under any circumstances
    if price_drops > TARGET_BUY_FLOOR_DROPS:
        print(f"[CRITICAL SAFETY TRIGGERED] Blocked direct buy attempt of {price_drops / 1_000_000} XRP which exceeds absolute safety limit of {TARGET_BUY_FLOOR_XRP} XRP.")
        return False, 0

    tx = NFTokenAcceptOffer(
        account=wallet.classic_address,
        nftoken_sell_offer=offer_id
    )
    
    if DRY_RUN:
        print(f"[DRY RUN] Would submit NFTokenAcceptOffer for SellOfferID: {offer_id}")
        return True, price_drops
    
    try:
        response = submit_and_wait(tx, client, wallet)
        if response.is_successful() and response.result.get("meta", {}).get("TransactionResult") == "tesSUCCESS":
            print(f"[Success] Successfully purchased NFT {nftoken_id}!")
            return True, price_drops
        else:
            res_code = response.result.get("meta", {}).get("TransactionResult", "Unknown")
            print(f"[Error] Direct buy failed. Result code: {res_code}")
            return False, 0
    except Exception as e:
        print(f"[Error] Transaction submission failed: {e}")
        return False, 0

def execute_brokered_buy(owner_address, nftoken_id, price_drops):
    """
    Create a Buy Offer for a marketplace listing to trigger the broker match.
    """
    # We bid the listing price + the broker fee required by the marketplace broker
    bid_amount = int(price_drops * BROKER_FEE_MULTIPLIER)
    
    # Hard Safety Limit: Never place a buy bid for more than the target buy floor under any circumstances
    if bid_amount > TARGET_BUY_FLOOR_DROPS:
        print(f"[CRITICAL SAFETY TRIGGERED] Blocked brokered buy bid of {bid_amount / 1_000_000} XRP which exceeds absolute safety limit of {TARGET_BUY_FLOOR_XRP} XRP.")
        return False, 0

    print(f"[Buy] Attempting brokered buy of NFT {nftoken_id} from {owner_address}.")
    print(f"      Original listing: {price_drops / 1_000_000} XRP. Submitting Buy Offer for: {bid_amount / 1_000_000} XRP...")
    
    # Set expiration in Ripple Epoch time
    ripple_time = int(time.time()) - RIPPLE_EPOCH
    expiration_time = ripple_time + BUY_OFFER_EXPIRATION_SEC
    
    tx = NFTokenCreateOffer(
        account=wallet.classic_address,
        nftoken_id=nftoken_id,
        amount=str(bid_amount),
        owner=owner_address,
        expiration=expiration_time
        # No tfSellNFToken flag implies this is a Buy Offer
    )
    
    if DRY_RUN:
        print(f"[DRY RUN] Would submit NFTokenCreateOffer (Buy) for NFT {nftoken_id} with amount {bid_amount} drops to owner {owner_address}")
        return True, bid_amount
    
    try:
        response = submit_and_wait(tx, client, wallet)
        if response.is_successful() and response.result.get("meta", {}).get("TransactionResult") == "tesSUCCESS":
            print(f"[Success] Created Buy Offer for NFT {nftoken_id}. Waiting for broker matching...")
            return True, bid_amount
        else:
            res_code = response.result.get("meta", {}).get("TransactionResult", "Unknown")
            print(f"[Error] Failed to create buy offer. Result code: {res_code}")
            return False, 0
    except Exception as e:
        print(f"[Error] Transaction submission failed: {e}")
        return False, 0

def create_sell_offer(nftoken_id, price_drops):
    """
    Create a Sell Offer to list our NFT.
    """
    print(f"[Sell] Creating Sell Offer for NFT {nftoken_id} at {price_drops / 1_000_000} XRP...")
    
    tx = NFTokenCreateOffer(
        account=wallet.classic_address,
        nftoken_id=nftoken_id,
        amount=str(price_drops),
        flags=[NFTokenCreateOfferFlag.TF_SELL_NFTOKEN]
    )
    
    if DRY_RUN:
        print(f"[DRY RUN] Would submit NFTokenCreateOffer (Sell) for NFT {nftoken_id} at {price_drops} drops")
        return "DRY_RUN_OFFER_ID"
    
    try:
        response = submit_and_wait(tx, client, wallet)
        if response.is_successful() and response.result.get("meta", {}).get("TransactionResult") == "tesSUCCESS":
            affected_nodes = response.result.get("meta", {}).get("AffectedNodes", [])
            offer_id = None
            for node in affected_nodes:
                created = node.get("CreatedNode", {})
                if created.get("LedgerEntryType") == "NFTokenOffer":
                    offer_id = created.get("LedgerIndex")
                    break
            
            print(f"[Success] Listed NFT {nftoken_id} for sale! Offer ID: {offer_id}")
            return offer_id
        else:
            res_code = response.result.get("meta", {}).get("TransactionResult", "Unknown")
            print(f"[Error] Failed to create sell offer. Result code: {res_code}")
            return None
    except Exception as e:
        print(f"[Error] Transaction submission failed: {e}")
        return None

def cancel_sell_offer(offer_id, nftoken_id):
    """
    Cancel an existing Sell Offer.
    """
    print(f"[Cancel] Canceling active sell offer {offer_id} for NFT {nftoken_id}...")
    
    tx = NFTokenCancelOffer(
        account=wallet.classic_address,
        nftoken_offers=[offer_id]
    )
    
    if DRY_RUN:
        print(f"[DRY RUN] Would submit NFTokenCancelOffer for Offer ID: {offer_id}")
        return True
    
    try:
        response = submit_and_wait(tx, client, wallet)
        if response.is_successful() and response.result.get("meta", {}).get("TransactionResult") == "tesSUCCESS":
            print(f"[Success] Cancelled sell offer {offer_id}.")
            return True
        else:
            res_code = response.result.get("meta", {}).get("TransactionResult", "Unknown")
            print(f"[Error] Failed to cancel offer. Result code: {res_code}")
            return False
    except Exception as e:
        print(f"[Error] Transaction submission failed: {e}")
        return False

def check_owned_nfts_sell_offers(nft_id):
    """
    Get all active sell offers on the ledger for an NFT.
    """
    try:
        request = NFTSellOffers(nft_id=nft_id)
        response = client.request(request)
        if response.is_successful():
            return response.result.get("offers", [])
    except Exception as e:
        pass
    return []

def validate_and_cleanup_offers(api_data):
    """
    Validate all our active sell and buy offers on-ledger:
    1. Cancel sell offers for NFTs we no longer own (orphans).
    2. Cancel duplicate sell offers for the same NFT (keeping the lowest price/most recent).
    3. Cancel buy offers that are expired, no longer listed under floor, or have incorrect amounts.
    """
    print("[Validation] Validating all active sell and buy offers on-ledger...")
    try:
        # 1. Fetch owned NFTs from target collection
        owned_nfts = []
        marker = None
        while True:
            request = AccountNFTs(account=wallet.classic_address, marker=marker)
            response = client.request(request)
            if response.is_successful():
                owned_nfts.extend(response.result.get("account_nfts", []))
                marker = response.result.get("marker")
                if not marker:
                    break
            else:
                print(f"[Validation Error] Failed to fetch owned NFTs: {response.result}")
                return
        owned_ids = {n.get("NFTokenID") for n in owned_nfts if n.get("Issuer") == ISSUER and n.get("NFTokenTaxon") == TAXON}

        # 2. Fetch all our active offers on-ledger
        objects = []
        marker = None
        while True:
            request = AccountObjects(account=wallet.classic_address, type="nft_offer", marker=marker)
            response = client.request(request)
            if response.is_successful():
                objects.extend(response.result.get("account_objects", []))
                marker = response.result.get("marker")
                if not marker:
                    break
            else:
                print(f"[Validation Error] Failed to fetch active offers: {response.result}")
                return
    except Exception as e:
        print(f"[Validation Error] Failed to query ledger state: {e}")
        return

    # Parse API listings for fast lookup
    api_listings = {}
    if api_data and "data" in api_data and "offers" in api_data["data"]:
        for item in api_data["data"]["offers"]:
            nft_id = item.get("NFTokenID")
            sell_offers = item.get("sell", [])
            best_sell = None
            for sell in sell_offers:
                amount = sell.get("Amount")
                if amount and isinstance(amount, str):
                    price_drops = int(amount)
                    if best_sell is None or price_drops < best_sell["price_drops"]:
                        best_sell = {
                            "price_drops": price_drops,
                            "offer_id": sell.get("OfferID"),
                            "destination": sell.get("Destination"),
                            "owner": sell.get("Owner") or item.get("NFTokenOwner")
                        }
            if best_sell:
                api_listings[nft_id] = best_sell

    ripple_time = int(time.time()) - RIPPLE_EPOCH
    offers_to_cancel = []
    
    # Track sell offers per NFT to detect duplicates
    nft_to_sell_offers = {}
    
    for obj in objects:
        flags = obj.get("Flags", 0)
        is_sell = (flags & 1) == 1
        nft_id = obj.get("NFTokenID")
        offer_index = obj.get("index")
        
        # Check expiration first
        expiration = obj.get("Expiration")
        if expiration is not None and ripple_time > expiration:
            print(f"[Validation] Found expired offer for NFT {nft_id}. Scheduling cancel.")
            offers_to_cancel.append(offer_index)
            continue

        if is_sell:
            # Sell offer check
            if nft_id not in owned_ids:
                print(f"[Validation] Found orphan sell offer for NFT {nft_id} (not owned by us). Scheduling cancel.")
                offers_to_cancel.append(offer_index)
            else:
                nft_to_sell_offers.setdefault(nft_id, []).append(obj)
        else:
            # Buy offer check
            if nft_id in owned_ids:
                print(f"[Validation] Found buy offer for NFT {nft_id} that we already own. Scheduling cancel.")
                offers_to_cancel.append(offer_index)
                continue
                
            listing = api_listings.get(nft_id)
            if not listing:
                print(f"[Validation] Found buy offer for NFT {nft_id} but it is no longer listed for sale. Scheduling cancel.")
                offers_to_cancel.append(offer_index)
                continue
                
            # Check price/amount
            is_brokered = bool(listing["destination"] and listing["destination"] != wallet.classic_address)
            expected_bid = int(listing["price_drops"] * BROKER_FEE_MULTIPLIER) if is_brokered else listing["price_drops"]
            
            # If listing price has changed and no longer matches our bid
            current_bid = int(obj.get("Amount", 0))
            if abs(current_bid - expected_bid) > 5:
                print(f"[Validation] Buy offer for NFT {nft_id} has outdated bid ({current_bid / 1_000_000} XRP vs expected {expected_bid / 1_000_000} XRP). Scheduling cancel.")
                offers_to_cancel.append(offer_index)

    # Detect duplicate sell offers
    for nft_id, sell_list in nft_to_sell_offers.items():
        if len(sell_list) > 1:
            # Sort by price ascending, then index (keep the lowest/best offer, cancel others)
            sell_list.sort(key=lambda x: int(x.get("Amount", 0)))
            for dup in sell_list[1:]:
                print(f"[Validation] Found duplicate sell offer for NFT {nft_id}. Scheduling cancel.")
                offers_to_cancel.append(dup.get("index"))

    # Cancel all flagged offers
    if offers_to_cancel:
        tx = NFTokenCancelOffer(
            account=wallet.classic_address,
            nftoken_offers=offers_to_cancel
        )
        if not DRY_RUN:
            try:
                response = submit_and_wait(tx, client, wallet)
                if response.is_successful() and response.result.get("meta", {}).get("TransactionResult") == "tesSUCCESS":
                    print(f"[Success] Successfully cancelled {len(offers_to_cancel)} invalid/duplicate offers on-ledger.")
                else:
                    res_code = response.result.get("meta", {}).get("TransactionResult", "Unknown")
                    print(f"[Error] Failed to cancel invalid offers on-ledger. Result code: {res_code}")
            except Exception as e:
                print(f"[Error] Failed to submit cancel transaction: {e}")
        else:
            print(f"[DRY RUN] Would submit NFTokenCancelOffer to cancel: {offers_to_cancel}")


def get_purchase_price_from_ledger(nft_id):
    """
    Query Clio's nft_history command for the specific NFT to find the exact drops we paid.
    Raises ValueError if the transaction cannot be found on-ledger.
    """
    try:
        # Submit raw JSON-RPC request to XRPL_NODE for nft_history
        payload = {
            "method": "nft_history",
            "params": [
                {
                    "nft_id": nft_id
                }
            ]
        }
        res = requests.post(XRPL_NODE, json=payload, timeout=15)
        res.raise_for_status()
        data = res.json()
        
        result = data.get("result", {})
        transactions = result.get("transactions", [])
        
        for tx_wrapper in transactions:
            tx = tx_wrapper.get("tx", {})
            meta = tx_wrapper.get("meta", {})
            
            if meta.get("TransactionResult") == "tesSUCCESS":
                # Look at the deleted NFTokenOffer nodes in the metadata
                for node in meta.get("AffectedNodes", []):
                    deleted = node.get("DeletedNode", {})
                    if deleted.get("LedgerEntryType") == "NFTokenOffer":
                        fields = deleted.get("FinalFields", {})
                        
                        # Case 1: Brokered Buy (our buy offer was consumed/deleted)
                        # Owner is us, and it's a buy offer (Flags & 1 == 0)
                        if (fields.get("Owner") == wallet.classic_address and 
                            not (fields.get("Flags", 0) & 1)):
                            amount_val = fields.get("Amount")
                            if isinstance(amount_val, str):
                                return int(amount_val)
                        
                        # Case 2: Direct Buy (we accepted the seller's sell offer)
                        # The transaction was sent by us (Account is us) and we accepted this sell offer
                        if (tx.get("TransactionType") == "NFTokenAcceptOffer" and 
                            tx.get("Account") == wallet.classic_address and 
                            deleted.get("LedgerIndex") == tx.get("NFTokenSellOffer")):
                            amount_val = fields.get("Amount")
                            if isinstance(amount_val, str):
                                return int(amount_val)
                                
    except Exception as e:
        raise ValueError(f"Error fetching NFT history from ledger: {e}")
    
    raise ValueError(f"No successful Buy Offer or Direct Accept transaction found in nft_history for NFT {nft_id}.")

def scan_and_sweep(api_data=None):
    # 1. Fetch offers from XRP Ledger Services NFT API if not provided
    if not api_data:
        api_data = fetch_api_sell_offers()
        
    if not api_data or "data" not in api_data or "offers" not in api_data["data"]:
        print("[API Warning] No valid offer data received from API. Skipping sweep.")
        return
    
    offers_list = api_data["data"]["offers"]
    print(f"[Scan] Scanned {len(offers_list)} NFTs in the collection.")
    
    # 2. Query open Buy Offers on-ledger ONCE at start of cycle
    active_bids_nft_ids = set()
    try:
        req_objs = AccountObjects(account=wallet.classic_address, type="nft_offer")
        res_objs = client.request(req_objs)
        if res_objs.is_successful():
            for obj in res_objs.result.get("account_objects", []):
                # Buy offers do not have TF_SELL_NFTOKEN flag (1)
                if not (obj.get("Flags", 0) & 1):
                    active_bids_nft_ids.add(obj.get("NFTokenID"))
    except Exception as e:
        print(f"[Warning] Failed to check active bids on-ledger: {e}")

    # Enforce strict safety limit of maximum active Buy Offers on-ledger
    if len(active_bids_nft_ids) >= MAX_ACTIVE_BUYS:
        print(f"[Safety] Already have {len(active_bids_nft_ids)} active Buy Offers on-ledger. Skipping sweep to stay within limit of {MAX_ACTIVE_BUYS}.")
        return

    # 3. Collect all valid candidates below target floor
    candidates = []
    for nft_entry in offers_list:
        nftoken_id = nft_entry.get("NFTokenID")
        owner = nft_entry.get("NFTokenOwner")
        
        # Skip if we already own this NFT
        if owner == wallet.classic_address:
            continue
            
        sell_offers = nft_entry.get("sell", [])
        for offer in sell_offers:
            # We only look for XRP listings
            price_str = offer.get("Amount")
            if not isinstance(price_str, str):
                continue
                
            price_drops = int(price_str)
            if price_drops <= 0:
                continue
                
            # Calculate total expected cost (including broker fee if applicable)
            is_brokered = bool(offer.get("Destination") and offer.get("Destination") != wallet.classic_address)
            total_expected_drops = int(price_drops * BROKER_FEE_MULTIPLIER) if is_brokered else price_drops
            
            # Check if total cost is under target buy floor (e.g. 4.5 XRP)
            if total_expected_drops < TARGET_BUY_FLOOR_DROPS:
                candidates.append({
                    "nftoken_id": nftoken_id,
                    "owner": owner,
                    "price_drops": price_drops,
                    "total_expected_drops": total_expected_drops,
                    "offer_id": offer.get("OfferID"),
                    "destination": offer.get("Destination")
                })
                
    # 4. Sort candidates by total expected drops ascending (cheapest first!)
    candidates.sort(key=lambda x: x["total_expected_drops"])
    
    # 5. Get current free balance on-ledger once and track locally
    local_free_bal = get_free_balance()
    
    # 6. Process candidates in order of price
    for item in candidates:
        nftoken_id = item["nftoken_id"]
        owner = item["owner"]
        price_drops = item["price_drops"]
        total_expected_drops = item["total_expected_drops"]
        offer_id = item["offer_id"]
        destination = item["destination"]
        
        print(f"[Match] Found listing below floor! NFT: {nftoken_id}")
        print(f"        Price: {price_drops / 1_000_000} XRP | Total Cost (with fees): {total_expected_drops / 1_000_000} XRP | Owner: {owner}")
        
        # Check if we already have an active bid (using fast local lookup)
        if nftoken_id in active_bids_nft_ids:
            print(f"        Active buy offer already exists on-ledger. Skipping duplicate bid.")
            continue
        
        # Pre-flight reserve and balance check using local free balance tracking
        is_brokered = bool(destination and destination != wallet.classic_address)
        required_drops = (total_expected_drops + 200_000) if is_brokered else (total_expected_drops + 10)
        
        if local_free_bal < required_drops:
            print(f"        Insufficient local free balance/reserve to buy. Free: {local_free_bal / 1_000_000} XRP, Required: {required_drops / 1_000_000} XRP. Skipping.")
            continue
        
        success = False
        paid_drops = 0
        
        # Handle Direct vs. Brokered Buy
        if not is_brokered:
            success, paid_drops = execute_direct_buy(offer_id, nftoken_id, price_drops)
        else:
            success, paid_drops = execute_brokered_buy(owner, nftoken_id, price_drops)
        
        if success:
            # Deduct from local free balance to account for committed funds/reserves
            local_free_bal -= required_drops
            
            relist_price_drops = max(TARGET_SELL_FLOOR_DROPS, int(paid_drops / RELIST_MARKUP_DIVISOR))
            # Proactively list it if direct buy succeeded
            if not destination:
                create_sell_offer(nftoken_id, relist_price_drops)
            
            # Stop sweeping further in this cycle to ensure we buy one at a time
            print("[Safety] Stopping cycle sweep after successful purchase/bid to prioritize cheapest next cycle.")
            break

def manage_inventory():
    """
    Manage owned NFTs: Check if they are listed for sale at the correct price.
    """
    print("[Inventory] Scanning owned NFTs to ensure they are listed at the floor...")
    
    owned_nfts = []
    marker = None
    try:
        while True:
            request = AccountNFTs(
                account=wallet.classic_address,
                marker=marker
            )
            response = client.request(request)
            if not response.is_successful():
                err_code = response.result.get("error")
                if err_code == "actNotFound":
                    print(f"[Inventory] Account {wallet.classic_address} is not active/funded on-ledger. Skipping inventory scan.")
                else:
                    print(f"[Inventory Error] Failed to fetch account NFTs from ledger: {response.result.get('error_message', err_code)}")
                return
            
            owned_nfts.extend(response.result.get("account_nfts", []))
            marker = response.result.get("marker")
            if not marker:
                break
    except Exception as e:
        print(f"[Inventory Error] Failed to fetch account NFTs: {e}")
        return
        
    collection_nfts = []
    for nft in owned_nfts:
        if nft.get("Issuer") == ISSUER and nft.get("NFTokenTaxon") == TAXON:
            collection_nfts.append(nft)
            
    print(f"[Inventory] Found {len(collection_nfts)} NFTs from target collection in wallet.")
    
    # Get current free balance on-ledger once and track locally to avoid reserve errors
    local_free_bal = get_free_balance()
    
    for nft in collection_nfts:
        nft_id = nft.get("NFTokenID")
        
        # Get active sell offers on this NFT on-ledger first
        sell_offers = check_owned_nfts_sell_offers(nft_id)
        
        our_active_offer = None
        for offer in sell_offers:
            if offer.get("owner") == wallet.classic_address:
                our_active_offer = offer
                break
        
        # Get purchase price from ledger transaction history dynamically
        try:
            cost_drops = get_purchase_price_from_ledger(nft_id)
            target_relist_price = max(TARGET_SELL_FLOOR_DROPS, int(cost_drops / RELIST_MARKUP_DIVISOR))
        except Exception as e:
            if our_active_offer:
                amount_val = our_active_offer.get("amount")
                current_price = int(amount_val) / 1_000_000 if isinstance(amount_val, str) else 0.0
                print(f"[Inventory] NFT {nft_id} is already listed at {current_price} XRP. Purchase transaction not found in recent history; keeping active listing.")
                continue
            else:
                print(f"[CRITICAL ERROR] Failed to determine cost for unlisted NFT {nft_id}: {e}")
                print(f"                 Skipping listing to prevent listing at a loss.")
                continue
                
        if our_active_offer:
            amount_val = our_active_offer.get("amount")
            if isinstance(amount_val, str):
                current_price_drops = int(amount_val)
            else:
                current_price_drops = 0
            offer_id = our_active_offer.get("nft_offer_index")
            
            # Verify if listing price is correct
            if current_price_drops != target_relist_price:
                # Pre-flight reserve check before relisting (need 0.2 XRP reserve + 10 drops fee)
                # Note: Cancel will release 0.2 XRP, but the transaction still requires sufficient funds at execution.
                if local_free_bal < 200_010:
                    print(f"[Inventory] Insufficient reserve to relist NFT {nft_id}. Free: {local_free_bal / 1_000_000} XRP, Required: 0.20001 XRP. Skipping.")
                    continue
                
                print(f"[Inventory] NFT {nft_id} is listed at incorrect price: {current_price_drops / 1_000_000} XRP.")
                print(f"            Target price: {target_relist_price / 1_000_000} XRP. Relisting...")
                
                # Cancel old offer and create new one
                if cancel_sell_offer(offer_id, nft_id):
                    # Canceling frees up 200,000 drops reserve (minus 10 drops fee)
                    local_free_bal += 199_990
                    if create_sell_offer(nft_id, target_relist_price):
                        local_free_bal -= 200_010
        else:
            # No active sell offer from us on-ledger. Create one!
            # Requires 200,010 drops (0.2 XRP reserve + 10 drops fee)
            if local_free_bal < 200_010:
                print(f"[Inventory] Insufficient reserve to list NFT {nft_id}. Free: {local_free_bal / 1_000_000} XRP, Required: 0.20001 XRP. Skipping.")
                continue
                
            print(f"[Inventory] NFT {nft_id} is not currently listed. Creating sell listing...")
            if create_sell_offer(nft_id, target_relist_price):
                local_free_bal -= 200_010

def main():
    while True:
        try:
            print("\n" + "-" * 50)
            print(f"Starting Bot Cycle at {time.strftime('%Y-%m-%d %H:%M:%S')}")
            print("-" * 50)
            
            # Step 1: Fetch API offers once for the cycle
            api_data = fetch_api_sell_offers()
            
            # Step 2: Validate all open buy and sell offers, canceling obsolete/invalid/duplicate ones
            validate_and_cleanup_offers(api_data)
            
            # Step 3: Manage inventory and relist at correct floor
            manage_inventory()
            
            # Step 4: Scan collection for deals and buy them
            scan_and_sweep(api_data)
            
            print(f"\nCycle complete. Waiting {POLL_INTERVAL} seconds...")
        except KeyboardInterrupt:
            print("\nShutting down bot.")
            sys.exit(0)
        except Exception as e:
            print(f"[Main Error] Unexpected error in main loop: {e}")
            print("Retrying next cycle...")
            
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
