"""
Set USDC + CTF allowances for Polymarket exchange contracts.
Needs a tiny amount of MATIC on the wallet for gas.
"""
from web3 import Web3
from web3.constants import MAX_INT

rpc_url = "https://rpc.ankr.com/polygon"
import os
from dotenv import load_dotenv
load_dotenv()
priv_key = os.environ["POLYMARKET_PRIVATE_KEY"]
pub_key = os.environ["POLYMARKET_FUNDER"]
chain_id = 137

erc20_approve = '[{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"}]'
erc1155_set_approval = '[{"inputs":[{"internalType":"address","name":"operator","type":"address"},{"internalType":"bool","name":"approved","type":"bool"}],"name":"setApprovalForAll","outputs":[],"stateMutability":"nonpayable","type":"function"}]'

usdc_address = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
ctf_address = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Exchange contracts to approve
EXCHANGES = {
    "CTF Exchange": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "Neg Risk CTF Exchange": "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    "Neg Risk Adapter": "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
}

web3 = Web3(Web3.HTTPProvider(rpc_url))

# Check connection + balance
print(f"Connected: {web3.is_connected()}")
balance = web3.eth.get_balance(pub_key)
print(f"MATIC balance: {web3.from_wei(balance, 'ether')} MATIC")

if balance == 0:
    print("\n⚠️  No MATIC for gas. Send ~0.1 MATIC to:")
    print(f"   {pub_key}")
    print("   on Polygon network")
    exit(1)

usdc = web3.eth.contract(address=usdc_address, abi=erc20_approve)
ctf = web3.eth.contract(address=ctf_address, abi=erc1155_set_approval)

for name, exchange in EXCHANGES.items():
    print(f"\n--- {name} ({exchange[:10]}...) ---")

    # USDC approve
    nonce = web3.eth.get_transaction_count(pub_key)
    tx = usdc.functions.approve(exchange, int(MAX_INT, 0)).build_transaction({
        "chainId": chain_id, "from": pub_key, "nonce": nonce,
    })
    signed = web3.eth.account.sign_transaction(tx, private_key=priv_key)
    tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = web3.eth.wait_for_transaction_receipt(tx_hash, 600)
    print(f"  USDC approve: {'✓' if receipt.status == 1 else '✗'}  tx={tx_hash.hex()[:16]}...")

    # CTF setApprovalForAll
    nonce = web3.eth.get_transaction_count(pub_key)
    tx = ctf.functions.setApprovalForAll(exchange, True).build_transaction({
        "chainId": chain_id, "from": pub_key, "nonce": nonce,
    })
    signed = web3.eth.account.sign_transaction(tx, private_key=priv_key)
    tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = web3.eth.wait_for_transaction_receipt(tx_hash, 600)
    print(f"  CTF approve:  {'✓' if receipt.status == 1 else '✗'}  tx={tx_hash.hex()[:16]}...")

print("\nDone! All allowances set.")
