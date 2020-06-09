import asyncio
import logging
import time
from pathlib import Path
from blspy import ExtendedPrivateKey, PrivateKey
from secrets import token_bytes

from typing import List, Optional, Tuple, Dict, Callable

from src.util.byte_types import hexstr_to_bytes
from src.util.keychain import (
    Keychain,
    seed_from_mnemonic,
    generate_mnemonic,
    bytes_to_mnemonic,
)
from src.util.path import path_from_root
from src.util.ws_message import create_payload
from src.wallet.trade_manager import TradeManager

try:
    import uvloop
except ImportError:
    uvloop = None

from src.cmds.init import check_keys
from src.server.outbound_message import NodeType, OutboundMessage, Message, Delivery
from src.simulator.simulator_protocol import FarmNewBlockProtocol
from src.util.config import load_config_cli
from src.util.ints import uint64, uint32
from src.util.logging import initialize_logging
from src.wallet.util.wallet_types import WalletType
from src.wallet.rl_wallet.rl_wallet import RLWallet
from src.wallet.cc_wallet.cc_wallet import CCWallet
from src.wallet.wallet_info import WalletInfo
from src.wallet.wallet_node import WalletNode
from src.types.mempool_inclusion_status import MempoolInclusionStatus

# Timeout for response from wallet/full node for sending a transaction
TIMEOUT = 30

log = logging.getLogger(__name__)


class WalletRpcApi:
    def __init__(self, keychain: Keychain, root_path: Path):
        self.config = load_config_cli(root_path, "config.yaml", "wallet")
        initialize_logging("Wallet %(name)-25s", self.config["logging"], root_path)
        self.log = log
        self.keychain = keychain
        self.websocket = None
        self.root_path = root_path
        self.wallet_node: Optional[WalletNode] = None
        self.trade_manager: Optional[TradeManager] = None
        self.shut_down = False
        if self.config["testing"] is True:
            self.config["database_path"] = "test_db_wallet.db"

    def get_routes(self) -> Dict[str, Callable]:
        return {
            "/get_wallet_balance": self.get_wallet_balance,
            "/send_transaction": self.send_transaction,
            "/get_next_puzzle_hash": self.get_next_puzzle_hash,
            "/get_transactions": self.get_transactions,
            "/farm_block": self.farm_block,
            "/get_sync_status": self.get_sync_status,
            "/get_height_info": self.get_height_info,
            "/get_connection_info": self.get_connection_info,
            "/create_new_wallet": self.create_new_wallet,
            "/get_wallets": self.get_wallets,
            "/rl_set_admin_info": self.rl_set_admin_info,
            "/rl_set_user_info": self.rl_set_user_info,
            "/cc_set_name": self.cc_set_name,
            "/cc_get_name": self.cc_get_name,
            "/cc_spend": self.cc_spend,
            "/cc_get_colour": self.cc_get_colour,
            "/create_offer_for_ids": self.create_offer_for_ids,
            "/get_discrepancies_for_offer": self.get_discrepancies_for_offer,
            "/respond_to_offer": self.respond_to_offer,
            "/get_wallet_summaries": self.get_wallet_summaries,
            "/get_public_keys": self.get_public_keys,
            "/generate_mnemonic": self.generate_mnemonic,
            "/log_in": self.log_in,
            "/add_key": self.add_key,
            "/delete_key": self.delete_key,
            "/delete_all_keys": self.delete_all_keys,
            "/get_private_key": self.get_private_key,
        }

    async def _state_changed(self, *args) -> List[str]:
        change = args[0]
        wallet_id = args[1]
        data = {
            "state": change,
        }
        if wallet_id is not None:
            data["wallet_id"] = wallet_id
        return [create_payload("state_changed", data, "chia-wallet", "wallet_ui")]

    async def get_next_puzzle_hash(self, request: Dict) -> Dict:
        """
        Returns a new puzzlehash
        """
        if self.wallet_node is None:
            return {"success": False}

        wallet_id = uint32(int(request["wallet_id"]))
        wallet = self.wallet_node.wallet_state_manager.wallets[wallet_id]

        if wallet.wallet_info.type == WalletType.STANDARD_WALLET:
            puzzle_hash = (await wallet.get_new_puzzlehash()).hex()
        elif wallet.wallet_info.type == WalletType.COLOURED_COIN:
            puzzle_hash = await wallet.get_new_inner_hash()

        response = {
            "wallet_id": wallet_id,
            "puzzle_hash": puzzle_hash,
        }

        return response

    async def send_transaction(self, request):
        wallet_id = int(request["wallet_id"])
        wallet = self.wallet_node.wallet_state_manager.wallets[wallet_id]
        try:
            tx = await wallet.generate_signed_transaction_dict(request)
        except BaseException as e:
            data = {
                "status": "FAILED",
                "reason": f"Failed to generate signed transaction {e}",
            }
            return data

        if tx is None:
            data = {
                "status": "FAILED",
                "reason": "Failed to generate signed transaction",
            }
            return data
        try:
            await wallet.push_transaction(tx)
        except BaseException as e:
            data = {
                "status": "FAILED",
                "reason": f"Failed to push transaction {e}",
            }
            return data
        self.log.error(tx)
        sent = False
        start = time.time()
        while time.time() - start < TIMEOUT:
            sent_to: List[
                Tuple[str, MempoolInclusionStatus, Optional[str]]
            ] = await self.wallet_node.wallet_state_manager.get_transaction_status(
                tx.name()
            )

            if len(sent_to) == 0:
                await asyncio.sleep(0.1)
                continue
            status, err = sent_to[0][1], sent_to[0][2]
            if status == MempoolInclusionStatus.SUCCESS:
                data = {"status": "SUCCESS"}
                sent = True
                break
            elif status == MempoolInclusionStatus.PENDING:
                assert err is not None
                data = {"status": "PENDING", "reason": err}
                sent = True
                break
            elif status == MempoolInclusionStatus.FAILED:
                assert err is not None
                data = {"status": "FAILED", "reason": err}
                sent = True
                break
        if not sent:
            data = {
                "status": "FAILED",
                "reason": "Timed out. Transaction may or may not have been sent.",
            }

        return data

    async def get_transactions(self, request):
        wallet_id = int(request["wallet_id"])
        transactions = await self.wallet_node.wallet_state_manager.get_all_transactions(
            wallet_id
        )

        response = {"success": True, "txs": transactions, "wallet_id": wallet_id}
        return response

    async def farm_block(self, request):
        puzzle_hash = bytes.fromhex(request["puzzle_hash"])
        request = FarmNewBlockProtocol(puzzle_hash)
        msg = OutboundMessage(
            NodeType.FULL_NODE, Message("farm_new_block", request), Delivery.BROADCAST,
        )

        self.wallet_node.server.push_message(msg)
        return {"success": True}

    async def get_wallet_balance(self, request):
        wallet_id = int(request["wallet_id"])
        wallet = self.wallet_node.wallet_state_manager.wallets[wallet_id]
        balance = await wallet.get_confirmed_balance()
        pending_balance = await wallet.get_unconfirmed_balance()
        spendable_balance = await wallet.get_spendable_balance()
        pending_change = await wallet.get_pending_change_balance()
        if wallet.wallet_info.type == WalletType.COLOURED_COIN:
            frozen_balance = 0
        else:
            frozen_balance = await wallet.get_frozen_amount()

        response = {
            "wallet_id": wallet_id,
            "success": True,
            "confirmed_wallet_balance": balance,
            "unconfirmed_wallet_balance": pending_balance,
            "spendable_balance": spendable_balance,
            "frozen_balance": frozen_balance,
            "pending_change": pending_change,
        }

        return response

    async def get_sync_status(self):
        syncing = self.wallet_node.wallet_state_manager.sync_mode

        response = {"syncing": syncing}

        return response

    async def get_height_info(self):
        lca = self.wallet_node.wallet_state_manager.lca
        height = self.wallet_node.wallet_state_manager.block_records[lca].height

        response = {"height": height}

        return response

    async def get_connection_info(self):
        connections = (
            self.wallet_node.server.global_connections.get_full_node_peerinfos()
        )

        response = {"connections": connections}

        return response

    async def create_new_wallet(self, request):
        config, wallet_state_manager, main_wallet = self.get_wallet_config()

        if request["wallet_type"] == "cc_wallet":
            if request["mode"] == "new":
                cc_wallet: CCWallet = await CCWallet.create_new_cc(
                    wallet_state_manager, main_wallet, request["amount"]
                )
                response = {"success": True, "type": cc_wallet.wallet_info.type.name}
                return response
            elif request["mode"] == "existing":
                cc_wallet = await CCWallet.create_wallet_for_cc(
                    wallet_state_manager, main_wallet, request["colour"]
                )
                response = {"success": True, "type": cc_wallet.wallet_info.type.name}
                return response

        response = {"success": False}
        return response

    def get_wallet_config(self):
        return (
            self.wallet_node.config,
            self.wallet_node.wallet_state_manager,
            self.wallet_node.wallet_state_manager.main_wallet,
        )

    async def get_wallets(self):
        wallets: List[
            WalletInfo
        ] = await self.wallet_node.wallet_state_manager.get_all_wallets()

        response = {"wallets": wallets, "success": True}

        return response

    async def rl_set_admin_info(self, request):
        wallet_id = int(request["wallet_id"])
        wallet: RLWallet = self.wallet_node.wallet_state_manager.wallets[wallet_id]
        user_pubkey = request["user_pubkey"]
        limit = uint64(int(request["limit"]))
        interval = uint64(int(request["interval"]))
        amount = uint64(int(request["amount"]))

        success = await wallet.admin_create_coin(interval, limit, user_pubkey, amount)

        response = {"success": success}

        return response

    async def rl_set_user_info(self, request):
        wallet_id = int(request["wallet_id"])
        wallet: RLWallet = self.wallet_node.wallet_state_manager.wallets[wallet_id]
        admin_pubkey = request["admin_pubkey"]
        limit = uint64(int(request["limit"]))
        interval = uint64(int(request["interval"]))
        origin_id = request["origin_id"]

        success = await wallet.set_user_info(interval, limit, origin_id, admin_pubkey)

        response = {"success": success}

        return response

    async def cc_set_name(self, request):
        wallet_id = int(request["wallet_id"])
        wallet: CCWallet = self.wallet_node.wallet_state_manager.wallets[wallet_id]
        await wallet.set_name(str(request["name"]))
        response = {"wallet_id": wallet_id, "success": True}
        return response

    async def cc_get_name(self, request):
        wallet_id = int(request["wallet_id"])
        wallet: CCWallet = self.wallet_node.wallet_state_manager.wallets[wallet_id]
        name: str = await wallet.get_name()
        response = {"wallet_id": wallet_id, "name": name}
        return response

    async def cc_spend(self, request):
        wallet_id = int(request["wallet_id"])
        wallet: CCWallet = self.wallet_node.wallet_state_manager.wallets[wallet_id]
        puzzle_hash = hexstr_to_bytes(request["innerpuzhash"])
        try:
            tx = await wallet.cc_spend(request["amount"], puzzle_hash)
        except BaseException as e:
            data = {
                "status": "FAILED",
                "reason": f"{e}",
            }
            return data

        if tx is None:
            data = {
                "status": "FAILED",
                "reason": "Failed to generate signed transaction",
            }
            return data

        self.log.error(tx)
        sent = False
        start = time.time()
        while time.time() - start < TIMEOUT:
            sent_to: List[
                Tuple[str, MempoolInclusionStatus, Optional[str]]
            ] = await self.wallet_node.wallet_state_manager.get_transaction_status(
                tx.name()
            )

            if len(sent_to) == 0:
                await asyncio.sleep(0.1)
                continue
            status, err = sent_to[0][1], sent_to[0][2]
            if status == MempoolInclusionStatus.SUCCESS:
                data = {"status": "SUCCESS"}
                sent = True
                break
            elif status == MempoolInclusionStatus.PENDING:
                assert err is not None
                data = {"status": "PENDING", "reason": err}
                sent = True
                break
            elif status == MempoolInclusionStatus.FAILED:
                assert err is not None
                data = {"status": "FAILED", "reason": err}
                sent = True
                break
        if not sent:
            data = {
                "status": "FAILED",
                "reason": "Timed out. Transaction may or may not have been sent.",
            }

        return data

    async def cc_get_colour(self, request):
        wallet_id = int(request["wallet_id"])
        wallet: CCWallet = self.wallet_node.wallet_state_manager.wallets[wallet_id]
        colour: str = await wallet.get_colour()
        response = {"colour": colour, "wallet_id": wallet_id}
        return response

    async def get_wallet_summaries(self):
        response = {}
        for wallet_id in self.wallet_node.wallet_state_manager.wallets:
            wallet = self.wallet_node.wallet_state_manager.wallets[wallet_id]
            balance = await wallet.get_confirmed_balance()
            type = wallet.wallet_info.type
            if type == WalletType.COLOURED_COIN:
                name = wallet.cc_info.my_colour_name
                colour = await wallet.get_colour()
                response[wallet_id] = {
                    "type": type,
                    "balance": balance,
                    "name": name,
                    "colour": colour,
                }
            else:
                response[wallet_id] = {"type": type, "balance": balance}
        return response

    async def get_discrepancies_for_offer(self, request):
        file_name = request["filename"]
        file_path = Path(file_name)
        (
            success,
            discrepancies,
            error,
        ) = await self.trade_manager.get_discrepancies_for_offer(file_path)

        if success:
            response = {"success": True, "discrepancies": discrepancies}
        else:
            response = {"success": False, "error": error}

        return response

    async def create_offer_for_ids(self, request):
        offer = request["ids"]
        file_name = request["filename"]
        success, spend_bundle, error = await self.trade_manager.create_offer_for_ids(
            offer
        )
        if success:
            self.trade_manager.write_offer_to_disk(Path(file_name), spend_bundle)
            response = {"success": success}
        else:
            response = {"success": success, "reason": error}

        return response

    async def respond_to_offer(self, request):
        file_path = Path(request["filename"])
        success, reason = await self.trade_manager.respond_to_offer(file_path)
        if success:
            response = {"success": success}
        else:
            response = {"success": success, "reason": reason}
        return response

    async def get_public_keys(self):
        fingerprints = [
            (esk.get_public_key().get_fingerprint(), seed is not None)
            for (esk, seed) in self.keychain.get_all_private_keys()
        ]
        response = {"success": True, "public_key_fingerprints": fingerprints}
        return response

    async def get_private_key(self, request):
        fingerprint = request["fingerprint"]
        for esk, seed in self.keychain.get_all_private_keys():
            if esk.get_public_key().get_fingerprint() == fingerprint:
                s = bytes_to_mnemonic(seed) if seed is not None else None
                self.log.warning(f"{s}, {esk}")
                return {
                    "success": True,
                    "private_key": {
                        "fingerprint": fingerprint,
                        "esk": bytes(esk).hex(),
                        "seed": s,
                    },
                }
        return {"success": False, "private_key": {"fingerprint": fingerprint}}

    async def log_in(self, request):
        await self.stop_wallet()
        fingerprint = request["fingerprint"]

        started = await self.start_wallet(fingerprint)

        response = {"success": started}
        return response

    async def add_key(self, request):
        if "mnemonic" in request:
            # Adding a key from 24 word mnemonic
            mnemonic = request["mnemonic"]
            seed = seed_from_mnemonic(mnemonic)
            self.keychain.add_private_key_seed(seed)
            esk = ExtendedPrivateKey.from_seed(seed)
        elif "hexkey" in request:
            # Adding a key from hex private key string. Two cases: extended private key (HD)
            # which is 77 bytes, and int private key which is 32 bytes.
            if len(request["hexkey"]) != 154 and len(request["hexkey"]) != 64:
                return {"success": False}
            if len(request["hexkey"]) == 64:
                sk = PrivateKey.from_bytes(bytes.fromhex(request["hexkey"]))
                self.keychain.add_private_key_not_extended(sk)
                key_bytes = bytes(sk)
                new_extended_bytes = bytearray(
                    bytes(ExtendedPrivateKey.from_seed(token_bytes(32)))
                )
                final_extended_bytes = bytes(
                    new_extended_bytes[: -len(key_bytes)] + key_bytes
                )
                esk = ExtendedPrivateKey.from_bytes(final_extended_bytes)
            else:
                esk = ExtendedPrivateKey.from_bytes(bytes.fromhex(request["hexkey"]))
                self.keychain.add_private_key(esk)

        else:
            return {"success": False}

        fingerprint = esk.get_public_key().get_fingerprint()
        await self.stop_wallet()

        # Makes sure the new key is added to config properly
        check_keys(self.root_path)

        # Starts the wallet with the new key selected
        started = await self.start_wallet(fingerprint)

        response = {"success": started}
        return response

    async def delete_key(self, request):
        await self.stop_wallet()
        fingerprint = request["fingerprint"]
        self.keychain.delete_key_by_fingerprint(fingerprint)
        response = {"success": True}
        return response

    async def clean_all_state(self):
        self.keychain.delete_all_keys()
        path = path_from_root(self.root_path, self.config["database_path"])
        if path.exists():
            path.unlink()

    async def stop_wallet(self):
        if self.wallet_node is not None:
            if self.wallet_node.server is not None:
                self.wallet_node.server.close_all()
            self.wallet_node._shutdown()
            await self.wallet_node.wallet_state_manager.close_all_stores()
            self.wallet_node = None

    async def delete_all_keys(self):
        await self.stop_wallet()
        await self.clean_all_state()
        response = {"success": True}
        return response

    async def generate_mnemonic(self):
        mnemonic = generate_mnemonic()
        response = {"success": True, "mnemonic": mnemonic}
        return response
