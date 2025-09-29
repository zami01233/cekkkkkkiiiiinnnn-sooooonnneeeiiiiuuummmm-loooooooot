import os
import time
import datetime
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account

load_dotenv()

RPC_URL = os.getenv("RPC_URL", "https://rpc.soneium.org")
CONTRACT_ADDRESS = Web3.to_checksum_address(os.getenv("CONTRACT_ADDRESS", "0x21Be1D69A77eA5882aCcD5c5319Feb7AC3854751"))
PRIVATE_KEYS_RAW = os.getenv("PRIVATE_KEYS", "")
ZERO_REFERRER = Web3.to_checksum_address(os.getenv("ZERO_REFERRER", "0x0000000000000000000000000000000000000000"))

DESIRED_PRIORITY_GWEI = float(os.getenv("DESIRED_PRIORITY_GWEI", "0.000145106"))
DESIRED_MAX_GWEI = float(os.getenv("DESIRED_MAX_GWEI", "0.001349"))

LOG_FILE = os.getenv("CHECKIN_LOG", "checkin_log.txt")

if not PRIVATE_KEYS_RAW:
    raise SystemExit("ENV ERROR: set PRIVATE_KEYS in .env (comma-separated).")

PRIVATE_KEYS = [k.strip() for k in PRIVATE_KEYS_RAW.split(",") if k.strip()]

w3 = Web3(Web3.HTTPProvider(RPC_URL))
chain_id = w3.eth.chain_id

CONTRACT_ABI = [
    {"inputs":[{"internalType":"address","name":"referrer","type":"address"}],"name":"checkIn","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"internalType":"address","name":"player","type":"address"}],"name":"hasCheckedInToday","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"view","type":"function"}
]

contract = w3.eth.contract(address=CONTRACT_ADDRESS, abi=CONTRACT_ABI)

def gwei_to_wei(x_gwei):
    return int(w3.to_wei(x_gwei, "gwei"))

def compute_base_and_defaults():
    try:
        pending = w3.eth.get_block('pending')
        base_fee = int(pending.get('baseFeePerGas', 0) or 0)
    except Exception:
        base_fee = 0
    priority_fee = gwei_to_wei(DESIRED_PRIORITY_GWEI)
    desired_max_from_env = gwei_to_wei(DESIRED_MAX_GWEI)
    if base_fee > 0:
        computed_max = max(base_fee + 2 * priority_fee, priority_fee)
        max_fee = min(computed_max, desired_max_from_env if desired_max_from_env > 0 else computed_max)
    else:
        max_fee = desired_max_from_env if desired_max_from_env > 0 else priority_fee * 3
    return base_fee, priority_fee, int(max_fee)

def adjust_max_fee_to_balance(max_fee_per_gas, priority_fee_per_gas, gas_limit, balance_wei):
    est_cost = gas_limit * max_fee_per_gas
    if est_cost <= balance_wei:
        return max_fee_per_gas, est_cost
    max_fee_fit = balance_wei // gas_limit
    if max_fee_fit < priority_fee_per_gas:
        return None, est_cost
    est_cost2 = gas_limit * max_fee_fit
    return int(max_fee_fit), int(est_cost2)

def wei_to_eth_str(x):
    return f"{w3.from_wei(int(x), 'ether'):.12f} ETH"

def send_tx(signed_raw):
    tx_hash = w3.eth.send_raw_transaction(signed_raw)
    print(f"  → tx sent: {tx_hash.hex()}")
    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        print(f"  → receipt status: {receipt.status} (gasUsed: {receipt.gasUsed})")
        return receipt
    except Exception as e:
        print("  → wait_for_receipt failed / timed out:", e)
        return None

def write_log(line: str):
    ts = datetime.datetime.now().astimezone().isoformat()
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{ts}  {line}\n")

def do_checkin(acct, pk):
    addr = Web3.to_checksum_address(acct.address)
    print(f"\n[Wallet] {addr}")
    try:
        already = contract.functions.hasCheckedInToday(addr).call()
    except Exception as e:
        print("  ❗ Error calling hasCheckedInToday:", e)
        return

    if already:
        print("  ✅ Sudah check-in hari ini, skip.")
        return

    base_fee, priority_fee, max_fee = compute_base_and_defaults()
    try:
        est_gas = contract.functions.checkIn(ZERO_REFERRER).estimate_gas({'from': addr})
        gas_limit = int(est_gas * 1.05)
    except Exception as e:
        print("  ⚠️ estimate_gas failed, fallback gas limit 150000. Err:", e)
        gas_limit = 150000

    balance = w3.eth.get_balance(addr)
    print(f"  Saldo: {wei_to_eth_str(balance)} (wei: {balance})")
    print(f"  Est gas limit: {gas_limit}")

    adjusted = adjust_max_fee_to_balance(max_fee, priority_fee, gas_limit, balance)
    if adjusted[0] is None:
        print("  ❌ Saldo tidak cukup untuk membayar fee minimal (priority). Coba topup atau turunkan gas hints.")
        return
    max_fee_adj, est_cost = adjusted
    print(f"  Menggunakan maxFeePerGas (wei): {max_fee_adj} (≈ {w3.from_wei(max_fee_adj,'gwei')} gwei)")
    print(f"  Est biaya tx = gas_limit * maxFee = {wei_to_eth_str(est_cost)}")

    nonce = w3.eth.get_transaction_count(addr)
    tx_dict = {
        "from": addr,
        "nonce": nonce,
        "gas": gas_limit,
        "maxPriorityFeePerGas": int(priority_fee),
        "maxFeePerGas": int(max_fee_adj),
        "chainId": chain_id,
        "value": 0
    }
    try:
        tx = contract.functions.checkIn(ZERO_REFERRER).build_transaction(tx_dict)
        signed = w3.eth.account.sign_transaction(tx, private_key=pk)
        print("  ▶ Mengirim checkIn...")
        receipt = send_tx(signed.raw_transaction)
        if receipt and receipt.status == 1:
            print("  ✅ Check-in sukses.")
            # tulis log + print pesan spesial user
            msg = "ulag terimkasih banyak"
            summary = f"{addr} | tx:{receipt.transactionHash.hex()} | {msg}"
            print(f"  ✉️  {msg}")
            write_log(summary)
        else:
            print("  ❌ Check-in gagal atau tidak terkonfirmasi.")
    except Exception as e:
        print("  ❗ Error saat membuat/mengirim transaksi:", e)

def main_loop():
    accounts = []
    for pk in PRIVATE_KEYS:
        try:
            acct = Account.from_key(pk)
            accounts.append((acct, pk))
        except Exception as e:
            print("Invalid private key skipped:", e)

    if not accounts:
        raise SystemExit("No valid private keys found.")

    print(f"Loaded {len(accounts)} wallet(s). RPC: {RPC_URL} ChainId: {chain_id}")
    while True:
        start_time = time.time()
        for idx, (acct, pk) in enumerate(accounts, start=1):
            try:
                do_checkin(acct, pk)
            except Exception as e:
                print("Unhandled error for wallet:", e)

            if idx < len(accounts):
                print("  ⏱ Menunggu 10 detik sebelum wallet berikutnya...")
                time.sleep(10)

        elapsed = time.time() - start_time
        sleep_seconds = 86400 + 60  # 24 jam + 1 menit
        print(f"\nSelesai 1 putaran. Waktu ditempuh: {int(elapsed)}s. Tidur {sleep_seconds}s (24 jam + 1 menit)...\n")
        time.sleep(sleep_seconds)

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        print("Dihentikan oleh user.")
