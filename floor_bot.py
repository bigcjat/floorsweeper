import os
import sys
import json
import asyncio
import time
import httpx
from dotenv import load_dotenv

# XRPL SDK imports
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.wallet import Wallet
from xrpl.models.requests import AccountNFTs, NFTSellOffers, AccountObjects, Fee, AccountObjectType
from xrpl.models.transactions import (
    NFTokenAcceptOffer,
    NFTokenCreateOffer,
    NFTokenCreateOfferFlag,
    NFTokenCancelOffer,
    TicketCreate
)
from xrpl.asyncio.transaction import submit_and_wait

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

# Brokers configuration: mapping of broker address -> fee multiplier
default_brokers = {"rpx9JThQ2y37FaGeeJP7PXDUVEXY3PHZSC": 1.01589}
brokers_env = os.getenv("BROKERS_CONFIG", "").strip()
if brokers_env:
    try:
        BROKERS = json.loads(brokers_env)
    except Exception as parse_err:
        print(f"[Warning] Failed to parse BROKERS_CONFIG: {parse_err}. Using default XRP Cafe broker.")
        BROKERS = default_brokers
else:
    BROKERS = default_brokers

# Safety limits & user preferences
MAX_ACTIVE_BUYS = int(os.getenv("MAX_ACTIVE_BUYS", "4"))
BUY_OFFER_EXPIRATION_SEC = int(os.getenv("BUY_OFFER_EXPIRATION_SEC", "600"))
RELIST_MARKUP_DIVISOR = float(os.getenv("RELIST_MARKUP_DIVISOR", "0.9"))
AUTO_RELIST = os.getenv("AUTO_RELIST", "True").lower() == "true"

def parse_id_list(env_var_name):
    val = os.getenv(env_var_name, "").strip()
    if not val:
        return set()
    return {x.strip() for x in val.split(",") if x.strip()}

PRIORITY_BUY_IDS = parse_id_list("PRIORITY_BUY_IDS")
HOLD_IDS = parse_id_list("HOLD_IDS")

BASE_FEE_DROPS = int(os.getenv("BASE_FEE_DROPS", "12"))
MAX_FEE_DROPS = int(os.getenv("MAX_FEE_DROPS", "1200"))

# Convert XRP to drops
TARGET_BUY_FLOOR_DROPS = int(TARGET_BUY_FLOOR_XRP * 1_000_000)
TARGET_SELL_FLOOR_DROPS = int(TARGET_SELL_FLOOR_XRP * 1_000_000)

# In-memory and persistent cache to prevent redundant on-ledger history scans
CACHE_FILE = "purchase_price_cache.json"
PURCHASE_PRICE_CACHE = {}

def load_purchase_price_cache():
    global PURCHASE_PRICE_CACHE
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                PURCHASE_PRICE_CACHE = json.load(f)
            print(f"[Cache] Loaded {len(PURCHASE_PRICE_CACHE)} cached purchase prices from '{CACHE_FILE}'.")
        except Exception as e:
            print(f"[Cache Warning] Failed to load cache file: {e}")
            PURCHASE_PRICE_CACHE = {}
    else:
        PURCHASE_PRICE_CACHE = {}

def save_purchase_price_cache():
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(PURCHASE_PRICE_CACHE, f, indent=2)
    except Exception as e:
        print(f"[Cache Warning] Failed to save cache file: {e}")

load_purchase_price_cache()

# Ticket queue state
AVAILABLE_TICKETS = []

print("=" * 80)
print("              XRPL NFT FLOOR SWEEPER & RELISTING BOT")
print("=" * 80)
print(f"Node:          {XRPL_NODE}")
print(f"Target Issuer: {ISSUER}")
print(f"Target Taxon:  {TAXON}")
print(f"Max Buy Cap:   {TARGET_BUY_FLOOR_XRP} XRP ({TARGET_BUY_FLOOR_DROPS} drops)")
print(f"Min Sell Floor:{TARGET_SELL_FLOOR_XRP} XRP ({TARGET_SELL_FLOOR_DROPS} drops)")
print(f"Brokers:       {json.dumps(BROKERS)}")
print(f"Max Active Buys:{MAX_ACTIVE_BUYS}")
print(f"Buy Expiration:{BUY_OFFER_EXPIRATION_SEC} seconds")
print(f"Auto Relist:   {AUTO_RELIST}")
print(f"Priority Buys: {len(PRIORITY_BUY_IDS)} items configured")
print(f"Hold (No-Sell):{len(HOLD_IDS)} items configured")
print(f"Relist Divisor:{RELIST_MARKUP_DIVISOR}")
print(f"Base Fee:      {BASE_FEE_DROPS} drops")
print(f"Max Fee Limit: {MAX_FEE_DROPS} drops")
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

# Ticket pool management
async def get_available_tickets(client_obj):
    """
    Query the ledger for all available tickets owned by our account.
    """
    tickets = []
    marker = None
    try:
        while True:
            req = AccountObjects(
                account=wallet.classic_address,
                type=AccountObjectType.TICKET,
                marker=marker
            )
            resp = await client_obj.request(req)
            if resp.is_successful():
                objects = resp.result.get("account_objects", [])
                for obj in objects:
                    tickets.append(int(obj.get("TicketSequence")))
                marker = resp.result.get("marker")
                if not marker:
                    break
            else:
                break
    except Exception as e:
        print(f"[Tickets Warning] Failed to query tickets: {e}")
    return sorted(tickets)

async def ensure_ticket_pool(client_obj):
    """
    Check if available tickets are low and replenish the pool if necessary.
    """
    global AVAILABLE_TICKETS
    
    # Check liquid balance first. If below 10 XRP (10,000,000 drops), don't use or create tickets.
    free_bal = await get_free_balance(client_obj)
    if free_bal < 10_000_000:
        AVAILABLE_TICKETS = []
        return
        
    AVAILABLE_TICKETS = await get_available_tickets(client_obj)
    
    # If tickets are low, top them up (target pool size: 30 tickets)
    if len(AVAILABLE_TICKETS) < 5:
        tickets_needed = 30 - len(AVAILABLE_TICKETS)
        required_reserve = tickets_needed * 200_000  # 0.2 XRP per ticket
        
        # Verify if free balance can cover the required reserve for the new tickets
        if free_bal < (required_reserve + 2_000_000):  # reserve + 2 XRP safety buffer
            print(f"[Tickets] Skip ticket creation. Balance too low to cover new ticket reserves ({free_bal / 1_000_000} XRP free).")
            return
            
        from xrpl.models.requests import AccountInfo
        try:
            resp = await client_obj.request(AccountInfo(account=wallet.classic_address))
            if resp.is_successful():
                account_data = resp.result.get("account_data", {})
                seq = account_data.get("Sequence")
                tickets_needed = 30 - len(AVAILABLE_TICKETS)
                print(f"[Tickets] Ticket pool low ({len(AVAILABLE_TICKETS)} active). Creating {tickets_needed} new tickets...")
                
                tx = TicketCreate(
                    account=wallet.classic_address,
                    ticket_count=tickets_needed,
                    sequence=seq,
                    fee=calculate_tx_fee()
                )
                
                if DRY_RUN:
                    print(f"[DRY RUN] Would submit TicketCreate for {tickets_needed} tickets.")
                    # Simulate tickets locally
                    start_seq = seq + 1
                    AVAILABLE_TICKETS.extend(range(start_seq, start_seq + tickets_needed))
                    return
                    
                resp_tx = await submit_and_wait(tx, client_obj, wallet)
                if resp_tx.is_successful() and resp_tx.result.get("meta", {}).get("TransactionResult") == "tesSUCCESS":
                    print(f"[Success] Created {tickets_needed} new tickets.")
                    AVAILABLE_TICKETS = await get_available_tickets(client_obj)
                else:
                    print(f"[Tickets Error] TicketCreate failed: {resp_tx.result.get('meta', {}).get('TransactionResult')}")
        except Exception as e:
            print(f"[Tickets Error] Failed to create tickets: {e}")

async def get_next_ticket(client_obj):
    """
    Pop the next ticket from the queue. Top up if empty.
    """
    global AVAILABLE_TICKETS
    
    # Check liquid balance first. If below 10 XRP, do not use tickets.
    free_bal = await get_free_balance(client_obj)
    if free_bal < 10_000_000:
        AVAILABLE_TICKETS = []
        return None

    if not AVAILABLE_TICKETS:
        try:
            await ensure_ticket_pool(client_obj)
        except Exception:
            pass
    if AVAILABLE_TICKETS:
        return AVAILABLE_TICKETS.pop(0)
    return None

async def get_tx_sequence_and_ticket(client_obj):
    """
    Get the next sequence or ticket sequence to use for a transaction.
    Returns (sequence, ticket_sequence) tuple.
    """
    ticket = await get_next_ticket(client_obj)
    if ticket is not None:
        return 0, ticket
        
    # Fallback to standard sequence
    from xrpl.models.requests import AccountInfo
    try:
        resp = await client_obj.request(AccountInfo(account=wallet.classic_address))
        if resp.is_successful():
            seq = resp.result.get("account_data", {}).get("Sequence")
            return seq, None
    except Exception as e:
        print(f"[Warning] Failed to fetch standard sequence: {e}")
    return None, None

CURRENT_TX_FEE_DROPS = str(BASE_FEE_DROPS)

async def update_current_fee(client_obj):
    """
    Fetch the current open ledger transaction fee (in drops) from the node
    and update our global variable.
    """
    global CURRENT_TX_FEE_DROPS
    try:
        response = await client_obj.request(Fee())
        if response.is_successful():
            drops = response.result.get("drops", {})
            open_ledger_fee = int(drops.get("open_ledger_fee", 10))
            fee_to_pay = max(BASE_FEE_DROPS, min(open_ledger_fee, MAX_FEE_DROPS))
            CURRENT_TX_FEE_DROPS = str(fee_to_pay)
            return
    except Exception as e:
        print(f"[Warning] Failed to fetch fee from ledger: {e}")
    CURRENT_TX_FEE_DROPS = str(BASE_FEE_DROPS)

def calculate_tx_fee():
    """
    Return the cached transaction fee.
    """
    return CURRENT_TX_FEE_DROPS

async def fetch_api_sell_offers(http_client):
    """
    Fetch active offers for the collection from XRP Ledger Services.
    """
    url = f"https://api.xrpldata.com/api/v1/xls20-nfts/offers/issuer/{ISSUER}/taxon/{TAXON}"
    headers = {}
    if XRP_API_KEY:
        headers["x-api-key"] = XRP_API_KEY
    
    try:
        response = await http_client.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"[API Error] Failed to fetch collection offers: {e}")
        return None

async def get_free_balance(client_obj):
    """
    Calculate the liquid/free XRP balance of the wallet in drops,
    subtracting base (1.0 XRP) and owner reserves (0.2 XRP per object).
    """
    try:
        from xrpl.models.requests import AccountInfo
        resp = await client_obj.request(AccountInfo(account=wallet.classic_address))
        if resp.is_successful():
            data = resp.result.get("account_data", {})
            balance = int(data.get("Balance", 0))
            owner_count = int(data.get("OwnerCount", 0))
            
            base_reserve = 1_000_000  # 1.0 XRP in drops
            owner_reserve = 200_000   # 0.2 XRP in drops
            
            total_reserve = base_reserve + owner_count * owner_reserve
            free_balance = balance - total_reserve
            return free_balance
    except Exception as e:
        print(f"[Warning] Failed to calculate free balance: {e}")
    return 0

async def execute_direct_buy(client_obj, offer_id, nftoken_id, price_drops):
    """
    Purchase an NFT directly from a public sell offer.
    """
    if price_drops > TARGET_BUY_FLOOR_DROPS:
        print(f"[CRITICAL SAFETY TRIGGERED] Blocked direct buy attempt of {price_drops / 1_000_000} XRP which exceeds absolute safety limit of {TARGET_BUY_FLOOR_XRP} XRP.")
        return False, 0

    seq, ticket = await get_tx_sequence_and_ticket(client_obj)
    tx = NFTokenAcceptOffer(
        account=wallet.classic_address,
        nftoken_sell_offer=offer_id,
        sequence=seq,
        ticket_sequence=ticket,
        fee=calculate_tx_fee()
    )
    
    if DRY_RUN:
        print(f"[DRY RUN] Would submit NFTokenAcceptOffer (Ticket: {ticket}) for SellOfferID: {offer_id}")
        return True, price_drops
    
    try:
        response = await submit_and_wait(tx, client_obj, wallet)
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

async def execute_brokered_buy(client_obj, owner_address, nftoken_id, price_drops, broker_fee_mult):
    """
    Create a Buy Offer for a marketplace listing to trigger the broker match.
    """
    bid_amount = int(price_drops * broker_fee_mult)
    
    if bid_amount > TARGET_BUY_FLOOR_DROPS:
        print(f"[CRITICAL SAFETY TRIGGERED] Blocked brokered buy bid of {bid_amount / 1_000_000} XRP which exceeds absolute safety limit of {TARGET_BUY_FLOOR_XRP} XRP.")
        return False, 0

    print(f"[Buy] Attempting brokered buy of NFT {nftoken_id} from {owner_address}.")
    print(f"      Original listing: {price_drops / 1_000_000} XRP. Submitting Buy Offer for: {bid_amount / 1_000_000} XRP...")
    
    # Set expiration in Ripple Epoch time
    ripple_time = int(time.time()) - RIPPLE_EPOCH
    expiration_time = ripple_time + BUY_OFFER_EXPIRATION_SEC
    
    seq, ticket = await get_tx_sequence_and_ticket(client_obj)
    tx = NFTokenCreateOffer(
        account=wallet.classic_address,
        nftoken_id=nftoken_id,
        amount=str(bid_amount),
        owner=owner_address,
        expiration=expiration_time,
        sequence=seq,
        ticket_sequence=ticket,
        fee=calculate_tx_fee()
    )
    
    if DRY_RUN:
        print(f"[DRY RUN] Would submit NFTokenCreateOffer Buy (Ticket: {ticket}) for NFT {nftoken_id} with amount {bid_amount} drops to owner {owner_address}")
        return True, bid_amount
    
    try:
        response = await submit_and_wait(tx, client_obj, wallet)
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

async def create_sell_offer(client_obj, nftoken_id, price_drops):
    """
    Create a Sell Offer to list our NFT.
    """
    print(f"[Sell] Creating Sell Offer for NFT {nftoken_id} at {price_drops / 1_000_000} XRP...")
    
    seq, ticket = await get_tx_sequence_and_ticket(client_obj)
    tx = NFTokenCreateOffer(
        account=wallet.classic_address,
        nftoken_id=nftoken_id,
        amount=str(price_drops),
        flags=NFTokenCreateOfferFlag.TF_SELL_NFTOKEN,
        sequence=seq,
        ticket_sequence=ticket,
        fee=calculate_tx_fee()
    )
    
    if DRY_RUN:
        print(f"[DRY RUN] Would submit NFTokenCreateOffer Sell (Ticket: {ticket}) for NFT {nftoken_id} at {price_drops} drops")
        return "DRY_RUN_OFFER_ID"
    
    try:
        response = await submit_and_wait(tx, client_obj, wallet)
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

async def cancel_sell_offer(client_obj, offer_id, nftoken_id):
    """
    Cancel an existing Sell Offer.
    """
    print(f"[Cancel] Canceling active sell offer {offer_id} for NFT {nftoken_id}...")
    
    seq, ticket = await get_tx_sequence_and_ticket(client_obj)
    tx = NFTokenCancelOffer(
        account=wallet.classic_address,
        nftoken_offers=[offer_id],
        sequence=seq,
        ticket_sequence=ticket,
        fee=calculate_tx_fee()
    )
    
    if DRY_RUN:
        print(f"[DRY RUN] Would submit NFTokenCancelOffer (Ticket: {ticket}) for Offer ID: {offer_id}")
        return True
    
    try:
        response = await submit_and_wait(tx, client_obj, wallet)
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

async def check_owned_nfts_sell_offers(client_obj, nft_id):
    """
    Get all active sell offers on the ledger for an NFT.
    """
    try:
        request = NFTSellOffers(nft_id=nft_id)
        response = await client_obj.request(request)
        if response.is_successful():
            return response.result.get("offers", [])
    except Exception as e:
        pass
    return []

async def validate_and_cleanup_offers(client_obj, api_data):
    """
    Validate all our active sell and buy offers on-ledger:
    1. Cancel sell offers for NFTs we no longer own (orphans).
    2. Cancel duplicate sell offers for the same NFT (keeping the lowest price/most recent).
    3. Cancel buy offers that are expired, no longer listed under floor, or have incorrect amounts.
    """
    print("[Validation] Validating all active sell and buy offers on-ledger...")
    # 1. Fetch owned NFTs from target collection (resilient with fallback to API data)
    owned_nfts = []
    try:
        marker = None
        while True:
            request = AccountNFTs(account=wallet.classic_address, marker=marker)
            response = await client_obj.request(request)
            if response.is_successful():
                owned_nfts.extend(response.result.get("account_nfts", []))
                marker = response.result.get("marker")
                if not marker:
                    break
            else:
                print(f"[Validation Warning] Failed to fetch owned NFTs from ledger: {response.result}")
                break
    except Exception as e:
        print(f"[Validation Warning] Exception querying owned NFTs: {e}")

    owned_ids = {n.get("NFTokenID") for n in owned_nfts if n.get("Issuer") == ISSUER and n.get("NFTokenTaxon") == TAXON}
    
    # Fallback: if we failed to query owned NFTs from the ledger, populate from xrpldata API
    if not owned_ids and api_data and "data" in api_data and "offers" in api_data["data"]:
        owned_ids = {item.get("NFTokenID") for item in api_data["data"]["offers"] if item.get("NFTokenOwner") == wallet.classic_address}
        print(f"[Validation Fallback] Loaded {len(owned_ids)} owned NFT IDs from XRPData API.")

    # 2. Fetch all our active offers on-ledger (resilient with fallback to API data)
    objects = []
    active_offers_failed = False
    try:
        marker = None
        while True:
            request = AccountObjects(account=wallet.classic_address, type=AccountObjectType.NFT_OFFER, marker=marker)
            response = await client_obj.request(request)
            if response.is_successful():
                objects.extend(response.result.get("account_objects", []))
                marker = response.result.get("marker")
                if not marker:
                    break
            else:
                print(f"[Validation Warning] Failed to fetch active offers from ledger: {response.result}")
                active_offers_failed = True
                break
    except Exception as e:
        print(f"[Validation Warning] Exception querying active offers: {e}")
        active_offers_failed = True

    # Fallback/Supplemental: populate/supplement objects from api_data ONLY if ledger query failed
    if active_offers_failed and api_data and "data" in api_data and "offers" in api_data["data"]:
        existing_offer_indexes = {obj.get("index") for obj in objects if obj.get("index") is not None}
        for item in api_data["data"]["offers"]:
            nft_id = item.get("NFTokenID")
            owner = item.get("NFTokenOwner")
            # If we own the NFT, any active sell offer belongs to us
            if owner == wallet.classic_address:
                sell_offers = item.get("sell", [])
                for sell in sell_offers:
                    offer_id = sell.get("OfferID")
                    if offer_id not in existing_offer_indexes:
                        mock_obj = {
                            "Flags": 1,  # TF_SELL_NFTOKEN
                            "NFTokenID": nft_id,
                            "index": offer_id,
                            "Amount": sell.get("Amount"),
                            "Owner": owner,
                            "Destination": sell.get("Destination"),
                            "Expiration": sell.get("Expiration")
                        }
                        objects.append(mock_obj)
                        existing_offer_indexes.add(offer_id)
        print(f"[Validation] Active offers list compiled using API fallback: {len(objects)} total.")
    else:
        print(f"[Validation] Active offers list compiled from ledger: {len(objects)} total.")


    # Parse API listings for fast lookup
    api_listings = {}
    if api_data and "data" in api_data and "offers" in api_data["data"]:
        for item in api_data["data"]["offers"]:
            nft_id = item.get("NFTokenID")
            sell_offers = item.get("sell", [])
            best_sell = None
            for sell in sell_offers:
                amount = sell.get("Amount")
                if amount and isinstance(amount, (str, int, float)):
                    try:
                        price_drops = int(amount)
                    except ValueError:
                        price_drops = 0
                    if price_drops > 0 and (best_sell is None or price_drops < best_sell["price_drops"]):
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
            # Skip managing active sell offers for hold list NFTs
            if nft_id in HOLD_IDS:
                continue
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
                
            # Skip price check for hold list NFTs to keep our manual/custom bids active
            if nft_id in HOLD_IDS:
                continue
                
            # Check price/amount
            dest = listing.get("destination")
            broker_fee_mult = BROKERS.get(dest, 1.0) if isinstance(dest, str) else 1.0
            expected_bid = int(listing["price_drops"] * broker_fee_mult)
            
            # If listing price has changed and no longer matches our bid
            amount_val = obj.get("Amount")
            current_bid = 0
            if isinstance(amount_val, (str, int, float)):
                try:
                    current_bid = int(amount_val)
                except ValueError:
                    pass
            if abs(current_bid - expected_bid) > 5:
                print(f"[Validation] Buy offer for NFT {nft_id} has outdated bid ({current_bid / 1_000_000} XRP vs expected {expected_bid / 1_000_000} XRP). Scheduling cancel.")
                offers_to_cancel.append(offer_index)

    # Detect duplicate sell offers
    for nft_id, sell_list in nft_to_sell_offers.items():
        if len(sell_list) > 1:
            # Sort by price descending, then index (keep the highest/best offer, cancel others)
            def get_amount(x):
                val = x.get("Amount")
                if isinstance(val, (str, int, float)):
                    try:
                        return int(val)
                    except ValueError:
                        return 0
                return 0
            sell_list.sort(key=get_amount, reverse=True)
            for dup in sell_list[1:]:
                print(f"[Validation] Found duplicate sell offer for NFT {nft_id}. Scheduling cancel.")
                offers_to_cancel.append(dup.get("index"))

    # Cancel all flagged offers
    if offers_to_cancel:
        seq, ticket = await get_tx_sequence_and_ticket(client_obj)
        tx = NFTokenCancelOffer(
            account=wallet.classic_address,
            nftoken_offers=offers_to_cancel,
            sequence=seq,
            ticket_sequence=ticket,
            fee=calculate_tx_fee()
        )
        if not DRY_RUN:
            try:
                response = await submit_and_wait(tx, client_obj, wallet)
                if response.is_successful() and response.result.get("meta", {}).get("TransactionResult") == "tesSUCCESS":
                    print(f"[Success] Successfully cancelled {len(offers_to_cancel)} invalid/duplicate offers on-ledger.")
                else:
                    res_code = response.result.get("meta", {}).get("TransactionResult", "Unknown")
                    print(f"[Error] Failed to cancel invalid offers on-ledger. Result code: {res_code}")
            except Exception as e:
                print(f"[Error] Failed to submit cancel transaction: {e}")
        else:
            print(f"[DRY RUN] Would submit NFTokenCancelOffer (Ticket: {ticket}) to cancel: {offers_to_cancel}")
            
    return objects

async def get_purchase_price_from_ledger(client_obj, http_client, nft_id):
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
        res = await http_client.post(client_obj.url, json=payload, timeout=15)
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
                            if isinstance(amount_val, (str, int, float)):
                                try:
                                    return int(amount_val)
                                except ValueError:
                                    pass
                        
                        # Case 2: Direct Buy (we accepted the seller's sell offer)
                        # The transaction was sent by us (Account is us) and we accepted this sell offer
                        if (tx.get("TransactionType") == "NFTokenAcceptOffer" and 
                            tx.get("Account") == wallet.classic_address and 
                            deleted.get("LedgerIndex") == tx.get("NFTokenSellOffer")):
                            amount_val = fields.get("Amount")
                            if isinstance(amount_val, (str, int, float)):
                                try:
                                    return int(amount_val)
                                except ValueError:
                                    pass
                                
    except Exception as e:
        raise ValueError(f"Error fetching NFT history from ledger: {e}")
    
    raise ValueError(f"No successful Buy Offer or Direct Accept transaction found in nft_history for NFT {nft_id}.")

async def process_single_nft_inventory(client_obj, http_client, nft, our_sell_offers, local_free_bal):
    """
    Process listings and pricing checks for a single owned NFT.
    """
    nft_id = nft.get("NFTokenID")
    if nft_id in HOLD_IDS:
        return local_free_bal

    our_active_offer = our_sell_offers.get(nft_id)
    cost_drops = PURCHASE_PRICE_CACHE.get(nft_id)
    
    if cost_drops is None:
        try:
            cost_drops = await get_purchase_price_from_ledger(client_obj, http_client, nft_id)
            PURCHASE_PRICE_CACHE[nft_id] = cost_drops
        except Exception as e:
            if our_active_offer:
                amount_val = our_active_offer.get("amount")
                current_price = 0.0
                if isinstance(amount_val, (str, int, float)):
                    try:
                        current_price = int(amount_val) / 1_000_000
                    except ValueError:
                        pass
                print(f"[Inventory] NFT {nft_id} is already listed at {current_price} XRP. Purchase transaction not found; keeping active listing.")
                return local_free_bal
            else:
                print(f"[CRITICAL ERROR] Failed to determine cost for unlisted NFT {nft_id}: {e}")
                print(f"                 Skipping listing to prevent listing at a loss.")
                return local_free_bal
                    
    target_relist_price = max(TARGET_SELL_FLOOR_DROPS, int(cost_drops / RELIST_MARKUP_DIVISOR))
    tx_fee = int(calculate_tx_fee())
            
    if our_active_offer:
        amount_val = our_active_offer.get("amount")
        current_price_drops = 0
        if isinstance(amount_val, (str, int, float)):
            try:
                current_price_drops = int(amount_val)
            except ValueError:
                pass
        offer_id = our_active_offer.get("nft_offer_index")
        
        # Verify if listing price is correct
        if current_price_drops != target_relist_price:
            # Pre-flight reserve check before relisting (need 0.2 XRP reserve + fee)
            if local_free_bal < (200_000 + tx_fee):
                print(f"[Inventory] Insufficient reserve to relist NFT {nft_id}. Free: {local_free_bal / 1_000_000} XRP. Skipping.")
                return local_free_bal
            
            print(f"[Inventory] NFT {nft_id} is listed at incorrect price: {current_price_drops / 1_000_000} XRP.")
            print(f"            Target price: {target_relist_price / 1_000_000} XRP. Relisting...")
            
            # Cancel old offer and create new one
            if await cancel_sell_offer(client_obj, offer_id, nft_id):
                local_free_bal += (200_000 - tx_fee)
                if await create_sell_offer(client_obj, nft_id, target_relist_price):
                    local_free_bal -= (200_000 + tx_fee)
                else:
                    print(f"[Inventory] Relisting failed to create new offer on-ledger. Aborting further listing attempts in this cycle to prevent ledger spam.")
                    local_free_bal = 0
            else:
                print(f"[Inventory] Relisting failed to cancel old offer on-ledger. Aborting further listing attempts in this cycle to prevent ledger spam.")
                local_free_bal = 0
    else:
        # No active sell offer from us on-ledger. Create one!
        if local_free_bal < (200_000 + tx_fee):
            print(f"[Inventory] Insufficient reserve to list NFT {nft_id}. Free: {local_free_bal / 1_000_000} XRP. Skipping.")
            return local_free_bal
            
        print(f"[Inventory] NFT {nft_id} is not currently listed. Creating sell listing...")
        if await create_sell_offer(client_obj, nft_id, target_relist_price):
            local_free_bal -= (200_000 + tx_fee)
        else:
            print(f"[Inventory] Listing failed. Aborting further listing attempts in this cycle to prevent ledger spam.")
            local_free_bal = 0
            
    return local_free_bal

async def manage_inventory(client_obj, http_client, active_offers):
    """
    Manage owned NFTs: Check if they are listed for sale at the correct price.
    """
    if not AUTO_RELIST:
        return [], {}
    print("[Inventory] Scanning owned NFTs to ensure they are listed at the floor...")
    global PURCHASE_PRICE_CACHE
    
    owned_nfts = []
    marker = None
    try:
        while True:
            request = AccountNFTs(account=wallet.classic_address, marker=marker)
            response = await client_obj.request(request)
            if not response.is_successful():
                err_code = response.result.get("error")
                if err_code == "actNotFound":
                    print(f"[Inventory] Account {wallet.classic_address} is not active/funded on-ledger. Skipping inventory scan.")
                else:
                    print(f"[Inventory Error] Failed to fetch account NFTs from ledger: {response.result.get('error_message', err_code)}")
                return [], {}
            
            owned_nfts.extend(response.result.get("account_nfts", []))
            marker = response.result.get("marker")
            if not marker:
                break
    except Exception as e:
        print(f"[Inventory Error] Failed to fetch account NFTs: {e}")
        return [], {}
        
    collection_nfts = [n for n in owned_nfts if n.get("Issuer") == ISSUER and n.get("NFTokenTaxon") == TAXON]
    print(f"[Inventory] Found {len(collection_nfts)} NFTs from target collection in wallet.")
    
    # Map active sell offers on-ledger for fast local lookup
    our_sell_offers = {}
    if active_offers:
        for obj in active_offers:
            flags = obj.get("Flags", 0)
            is_sell = (flags & 1) == 1
            if is_sell:
                nft_id = obj.get("NFTokenID")
                our_sell_offers[nft_id] = {
                    "nft_offer_index": obj.get("index"),
                    "amount": obj.get("Amount"),
                    "flags": flags,
                    "owner": wallet.classic_address
                }
    
    local_free_bal = await get_free_balance(client_obj)
    
    # Pre-fetch missing purchase history concurrently to optimize API roundtrips
    nfts_to_fetch = [n for n in collection_nfts if n.get("NFTokenID") not in HOLD_IDS and PURCHASE_PRICE_CACHE.get(n.get("NFTokenID")) is None]
    if nfts_to_fetch:
        print(f"[Inventory] Querying Clio history for {len(nfts_to_fetch)} NFTs (max concurrency: 3)...")
        sem = asyncio.Semaphore(3)
        async def fetch_single_price(nft_item):
            n_id = nft_item.get("NFTokenID")
            print(f"[Inventory Debug] fetch_single_price entered for {n_id}")
            async with sem:
                try:
                    price = await get_purchase_price_from_ledger(client_obj, http_client, n_id)
                    PURCHASE_PRICE_CACHE[n_id] = price
                except Exception as e:
                    print(f"[Inventory Warning] Failed to fetch price for {n_id}: {e}")
        await asyncio.gather(*(fetch_single_price(n) for n in nfts_to_fetch))
        save_purchase_price_cache()
    
    # Process listing transactions sequentially to prevent Ticket Sequence race conditions
    for nft in collection_nfts:
        local_free_bal = await process_single_nft_inventory(client_obj, http_client, nft, our_sell_offers, local_free_bal)

    return collection_nfts, our_sell_offers

async def scan_and_sweep(client_obj, http_client, api_data=None, collection_nfts=None, our_sell_offers=None):
    """
    Scan collection for deals and sweep them, processing multiple items concurrently.
    """
    if collection_nfts is None:
        collection_nfts = []
    if our_sell_offers is None:
        our_sell_offers = {}
        
    if not api_data:
        api_data = await fetch_api_sell_offers(http_client)
        
    if not api_data or "data" not in api_data or "offers" not in api_data["data"]:
        print("[API Warning] No valid offer data received from API. Skipping sweep.")
        return
    
    offers_list = api_data["data"]["offers"]
    print(f"[Scan] Scanned {len(offers_list)} NFTs in the collection.")
    
    # Query open Buy Offers on-ledger ONCE at start of cycle
    active_bids_nft_ids = set()
    try:
        req_objs = AccountObjects(account=wallet.classic_address, type=AccountObjectType.NFT_OFFER)
        res_objs = await client_obj.request(req_objs)
        if res_objs.is_successful():
            for obj in res_objs.result.get("account_objects", []):
                # Buy offers do not have TF_SELL_NFTOKEN flag (1)
                if not (obj.get("Flags", 0) & 1):
                    active_bids_nft_ids.add(obj.get("NFTokenID"))
    except Exception as e:
        print(f"[Warning] Failed to check active bids on-ledger: {e}")

    # Collect all valid candidates below target floor
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
            price_val = offer.get("Amount")
            if isinstance(price_val, (str, int, float)):
                try:
                    price_drops = int(price_val)
                except ValueError:
                    continue
            else:
                continue
                
            if price_drops <= 0:
                continue
                
            # Check destination to filter out private sales/transfers
            dest = offer.get("Destination")
            if dest:
                if dest == wallet.classic_address:
                    is_brokered = False
                    broker_fee_mult = 1.0
                elif dest in BROKERS:
                    is_brokered = True
                    broker_fee_mult = BROKERS[dest]
                else:
                    # Destination is set to an unsupported address (private sale) -> Skip it
                    continue
            else:
                is_brokered = False
                broker_fee_mult = 1.0

            # Calculate total expected cost (including broker fee if applicable)
            total_expected_drops = int(price_drops * broker_fee_mult)
            
            # Check if total cost is under target buy floor (e.g. 4.5 XRP)
            if total_expected_drops < TARGET_BUY_FLOOR_DROPS:
                candidates.append({
                    "nftoken_id": nftoken_id,
                    "owner": owner,
                    "price_drops": price_drops,
                    "total_expected_drops": total_expected_drops,
                    "offer_id": offer.get("OfferID"),
                    "destination": dest,
                    "broker_fee_mult": broker_fee_mult
                })
                
    # Sort candidates by priority list (first) and then total expected drops ascending
    candidates.sort(key=lambda x: (x["nftoken_id"] not in PRIORITY_BUY_IDS, x["total_expected_drops"]))
    
    # Get current free balance on-ledger
    local_free_bal = await get_free_balance(client_obj)

    # 1. Determine how many owned collection NFTs do not have active sell offers
    unlisted_count = 0
    if collection_nfts:
        unlisted_count = sum(1 for n in collection_nfts if n.get("NFTokenID") not in HOLD_IDS and n.get("NFTokenID") not in our_sell_offers)
    
    # 2. Reserve 0.2 XRP (200,000 drops) + transaction fee per unlisted NFT
    tx_fee = int(calculate_tx_fee())
    listing_reserve_drops = unlisted_count * (200_000 + tx_fee)
    if listing_reserve_drops > 0:
        print(f"[Sweep Reserve] Reserving {listing_reserve_drops / 1_000_000} XRP of free balance to list {unlisted_count} unlisted owned NFTs first.")
        local_free_bal = max(0, local_free_bal - listing_reserve_drops)
    
    tasks = []
    # Process candidates in order of price, scheduling parallel sweeps up to limits
    for item in candidates:
        total_active_buys = len(active_bids_nft_ids) + len(tasks)
        if total_active_buys >= MAX_ACTIVE_BUYS:
            print(f"[Safety] Already have {total_active_buys} active Buy Offers/pending tasks. Skipping further sweeps in this cycle.")
            break
            
        nftoken_id = item["nftoken_id"]
        owner = item["owner"]
        price_drops = item["price_drops"]
        total_expected_drops = item["total_expected_drops"]
        offer_id = item["offer_id"]
        destination = item["destination"]
        
        # Check if we already have an active bid (or planned task)
        if nftoken_id in active_bids_nft_ids or any(t[0] == nftoken_id for t in tasks):
            continue
        
        # Pre-flight reserve and balance check
        is_brokered = destination in BROKERS
        tx_fee = int(calculate_tx_fee())
        required_drops = (total_expected_drops + 200_000 + tx_fee) if is_brokered else (total_expected_drops + tx_fee)
        
        if local_free_bal < required_drops:
            print(f"[Match] Found listing below floor! NFT: {nftoken_id}")
            print(f"        Price: {price_drops / 1_000_000} XRP | Total Cost (with fees): {total_expected_drops / 1_000_000} XRP | Owner: {owner}")
            print(f"        Insufficient balance to buy. Free: {local_free_bal / 1_000_000} XRP, Required: {required_drops / 1_000_000} XRP. Skipping.")
            continue
        
        # Deduct from local balance tracking for current cycle
        local_free_bal -= required_drops
        
        async def run_buy_task(nft_id, own_addr, off_id, pr_drops, dest_addr, broker_mult):
            print(f"[Match] Found listing below floor! NFT: {nft_id}")
            print(f"        Price: {pr_drops / 1_000_000} XRP | Owner: {own_addr}")
            
            success = False
            paid_drops = 0
            is_brok = dest_addr in BROKERS
            
            if not is_brok:
                success, paid_drops = await execute_direct_buy(client_obj, off_id, nft_id, pr_drops)
            else:
                success, paid_drops = await execute_brokered_buy(client_obj, own_addr, nft_id, pr_drops, broker_mult)
            
            if success:
                relist_price_drops = max(TARGET_SELL_FLOOR_DROPS, int(paid_drops / RELIST_MARKUP_DIVISOR))
                if AUTO_RELIST and nft_id not in HOLD_IDS and not dest_addr:
                    await create_sell_offer(client_obj, nft_id, relist_price_drops)

        tasks.append((nftoken_id, run_buy_task(nftoken_id, owner, offer_id, price_drops, destination, item["broker_fee_mult"])))

    if tasks:
        print(f"[Sweep] Executing {len(tasks)} sweeps concurrently in this cycle using Ticket sequences...")
        await asyncio.gather(*(t[1] for t in tasks))

async def async_main():
    """
    Main asynchronous loop of the bot.
    """
    # Establish XRPL client
    try:
        client_obj = AsyncJsonRpcClient(XRPL_NODE)
        print("Connected to XRPL Node.")
    except Exception as e:
        print(f"[CRITICAL ERROR] Failed to connect to XRPL node {XRPL_NODE}: {e}")
        sys.exit(1)

    async with httpx.AsyncClient() as http_client:
        while True:
            try:
                print("\n" + "-" * 50)
                print(f"Starting Bot Cycle at {time.strftime('%Y-%m-%d %H:%M:%S')}")
                print("-" * 50)
                
                # Step 0: Update cached transaction fee for this cycle
                await update_current_fee(client_obj)
                
                # Step 1: Ensure ticket pool is loaded/topped up on-ledger
                await ensure_ticket_pool(client_obj)
                
                # Step 2: Fetch API offers once for the cycle
                api_data = await fetch_api_sell_offers(http_client)
                
                # Step 3: Validate all open buy and sell offers, canceling obsolete ones
                active_offers = await validate_and_cleanup_offers(client_obj, api_data)
                
                # Step 4: Manage inventory and relist at correct floor
                collection_nfts = []
                our_sell_offers = {}
                if AUTO_RELIST:
                    collection_nfts, our_sell_offers = await manage_inventory(client_obj, http_client, active_offers)
                
                # Step 5: Scan collection for deals and buy them
                await scan_and_sweep(client_obj, http_client, api_data, collection_nfts, our_sell_offers)
                
                print(f"\nCycle complete. Waiting {POLL_INTERVAL} seconds...")
            except KeyboardInterrupt:
                print("\nShutting down bot.")
                sys.exit(0)
            except Exception as e:
                print(f"[Main Error] Unexpected error in main loop: {e}")
                import traceback
                traceback.print_exc()
                print("Retrying next cycle...")
                
            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\nShutting down bot.")
        sys.exit(0)
