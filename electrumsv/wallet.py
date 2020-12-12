# Electrum - lightweight Bitcoin client
# Copyright (C) 2015 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# Wallet classes:
#   - ImportedAddressAccount: imported address, no keystore
#   - ImportedPrivkeyAccount: imported private keys, keystore
#   - StandardAccount: one keystore, P2PKH
#   - MultisigAccount: several keystores, P2SH

from collections import defaultdict
from datetime import datetime
import json
import os
import random
import threading
import time
from typing import (Any, cast, Dict, Iterable, List, NamedTuple, Optional, Sequence,
    Set, Tuple, TypeVar, TYPE_CHECKING, Union)
import weakref

import aiorpcx
import attr
from bitcoinx import (Address, PrivateKey, PublicKey, hash_to_hex_str, hex_str_to_hash,
    MissingHeader, Ops, pack_be_uint32, pack_byte, push_item, Script)

from . import coinchooser
from .app_state import app_state
from .bitcoin import compose_chain_string, COINBASE_MATURITY, scripthash_bytes, ScriptTemplate
from .constants import (ACCOUNT_SCRIPT_TYPES, AccountType, CHANGE_SUBPATH,
    DEFAULT_TXDATA_CACHE_SIZE_MB, DerivationType,
    KeyInstanceFlag, KeystoreTextType, MAXIMUM_TXDATA_CACHE_SIZE_MB, MINIMUM_TXDATA_CACHE_SIZE_MB,
    RECEIVING_SUBPATH, ScriptType, TransactionInputFlag, TransactionOutputFlag, TxFlags,
    WalletEventFlag, WalletEventType, WalletSettings)
from .contacts import Contacts
from .crypto import pw_encode, sha256
from .exceptions import (ExcessiveFee, NotEnoughFunds, PreviousTransactionsMissingException,
    UserCancelled, UnknownTransactionException, WalletLoadError)
from .i18n import _
from .keys import extract_public_key_hash, get_multi_signer_script_template, \
    get_single_signer_script_template
from .keystore import (DerivablePaths, Deterministic_KeyStore, Hardware_KeyStore, Imported_KeyStore,
    instantiate_keystore, KeyStore, Multisig_KeyStore, SinglesigKeyStoreTypes,
    SignableKeystoreTypes, StandardKeystoreTypes, Xpub)
from .logs import logs
from .networks import Net
from .services import InvoiceService, RequestService
from .simple_config import SimpleConfig
from .storage import WalletStorage
from .transaction import (Transaction, TransactionContext, TxSerialisationFormat, NO_SIGNATURE,
    XPublicKey, XPublicKeyType, XTxInput, XTxOutput)
from .types import TxoKeyType, WaitingUpdateCallback
from .util import (format_satoshis, get_wallet_name_from_path, timestamp_to_datetime,
    TriggeredCallbacks)
from .wallet_database import functions as db_functions
from .wallet_database import TxData, TxProof, TransactionCacheEntry, TransactionCache
from .wallet_database.functions import KeyListRow
from .wallet_database.tables import (AccountTable, AccountTransactionDescriptionRow,
    AccountTransactionTable, AddTransactionInputRow, AddTransactionOutputRow, AddTransactionRow,
    AsynchronousFunctions, InvoiceTable, KeyInstanceTable,
    MasterKeyTable, TransactionTable, TransactionOutputTable,
    PaymentRequestTable, WalletEventRow, WalletEventTable)
from .wallet_database.sqlite_support import CompletionCallbackType, DatabaseContext, \
    SynchronousWriter
from .wallet_database.types import AccountRow, KeyInstanceRow, MasterKeyRow, \
    PaymentRequestRow, TransactionDeltaSumRow, TransactionOutputRow

if TYPE_CHECKING:
    from .network import Network
    from electrumsv.gui.qt.main_window import ElectrumWindow
    from electrumsv.devices.hw_wallet.qt import QtPluginBase

logger = logs.get_logger("wallet")


@attr.s(auto_attribs=True)
class DeterministicKeyAllocation:
    masterkey_id: int
    derivation_type: DerivationType
    derivation_path: Sequence[int]

@attr.s(auto_attribs=True)
class BIP32KeyData:
    masterkey_id: int
    derivation_path: Sequence[int]
    script_type: ScriptType
    script_pubkey: bytes


class HistoryLine(NamedTuple):
    sort_key: Tuple[int, int]
    tx_hash: bytes
    tx_flags: TxFlags
    height: Optional[int]
    value_delta: int


@attr.s(slots=True, hash=False)
class UTXO:
    value = attr.ib()
    script_pubkey = attr.ib()
    script_type: ScriptType = attr.ib()
    tx_hash: bytes = attr.ib()
    out_index: int = attr.ib()
    keyinstance_id: int = attr.ib()
    address = attr.ib()
    # To determine if matured and spendable
    is_coinbase = attr.ib()
    flags: TransactionOutputFlag = attr.ib()

    def __eq__(self, other):
        return isinstance(other, UTXO) and self.key() == other.key()

    def __hash__(self):
        return hash(self.key())

    def key(self) -> TxoKeyType:
        return TxoKeyType(self.tx_hash, self.out_index)

    def key_str(self) -> str:
        return f"{hash_to_hex_str(self.tx_hash)}:{self.out_index}"

    def to_tx_input(self, account: 'AbstractAccount') -> XTxInput:
        threshold = account.get_threshold(self.script_type)
        # NOTE(rt12) The typing of attrs subclasses is not detected, so have to ignore.
        x_pubkeys = account.get_xpubkeys_for_id(self.keyinstance_id)
        return XTxInput( # type: ignore
            prev_hash=self.tx_hash,
            prev_idx=self.out_index,
            script_sig=Script(),
            sequence=0xffffffff,
            threshold=threshold,
            script_type=self.script_type,
            signatures=[NO_SIGNATURE] * len(x_pubkeys),
            x_pubkeys=x_pubkeys,
            value=self.value,
            keyinstance_id=self.keyinstance_id
        )


# class SyncState:
#     def __init__(self) -> None:
#         self._key_history: Dict[int, List[Tuple[str, int]]] = {}
#         self._tx_keys: Dict[str, Set[int]] = {}

#     def get_key_history(self, key_id: int) -> List[Tuple[str, int]]:
#         return self._key_history.get(key_id, [])

#     def set_key_history(self, key_id: int, history: List[Tuple[str, int]]) \
#             -> Tuple[Set[str], Set[str]]:
#         old_history = self._key_history.get(key_id, [])
#         self._key_history[key_id] = history

#         old_tx_ids = set(t[0] for t in old_history)
#         new_tx_ids = set(t[0] for t in history)

#         removed_tx_ids = old_tx_ids - new_tx_ids
#         added_tx_ids = new_tx_ids - old_tx_ids

#         for tx_id in removed_tx_ids:
#             self._tx_keys[tx_id].remove(key_id)

#         for tx_id in added_tx_ids:
#             if tx_id not in self._tx_keys:
#                 self._tx_keys[tx_id] = set()
#             self._tx_keys[tx_id].add(key_id)

#         return removed_tx_ids, added_tx_ids

#     def get_transaction_key_ids(self, tx_id: str) -> Set[int]:
#         tx_keys = self._tx_keys.get(tx_id)
#         if tx_keys is None:
#             return set()
#         return tx_keys


def dust_threshold(network):
    return 546 # hard-coded Bitcoin SV dust threshold. Was changed to this as of Sept. 2018

CachedScriptType = Tuple[Script, bytes, Optional[ScriptTemplate]]

T = TypeVar('T', bound='AbstractAccount')

class AbstractAccount:
    """
    Account classes are created to handle various address generation methods.
    Completion states (watching-only, single account, no seed, etc) are handled inside classes.
    """

    _default_keystore: Optional[KeyStore] = None
    _stopped: bool = False

    max_change_outputs = 10

    def __init__(self, wallet: 'Wallet', row: AccountRow, keyinstance_rows: List[KeyInstanceRow],
            output_rows: List[TransactionOutputRow],
            transaction_descriptions: List[AccountTransactionDescriptionRow]) -> None:
        # Prevent circular reference keeping parent and accounts alive.
        self._wallet = weakref.proxy(wallet)
        self._row = row
        self._id = row.account_id

        self._logger = logs.get_logger("account[{}]".format(self.name()))
        self._network = None

        self._script_cache: Dict[Tuple[int, ScriptType], CachedScriptType] = {}

        # For synchronization.
        self._activated_keys: List[int] = []
        self._activated_keys_lock = threading.Lock()
        self._activated_keys_event = app_state.async_.event()
        self._deactivated_keys: List[int] = []
        self._deactivated_keys_lock = threading.Lock()
        self._deactivated_keys_event = app_state.async_.event()
        self._synchronize_event = app_state.async_.event()
        self._synchronized_event = app_state.async_.event()

        self._subpath_gap_limits: Dict[Sequence[int], int] = {}
        self.request_count = 0
        self.response_count = 0
        self.last_poll_time: Optional[float] = None

        self._utxos: Dict[TxoKeyType, UTXO] = {}
        self._utxos_lock = threading.RLock()
        self._stxos: Dict[TxoKeyType, int] = {}
        self._keypath: Dict[int, Sequence[int]] = {}
        self._keyinstances: Dict[int, KeyInstanceRow] = { r.keyinstance_id: r for r
            in keyinstance_rows }
        self._masterkey_ids: Set[int] = set(row.masterkey_id for row in keyinstance_rows
            if row.masterkey_id is not None)
        self._transaction_descriptions: Dict[bytes, str] = { r.tx_hash: cast(str, r.description)
            for r in transaction_descriptions }

        self._load_keys(keyinstance_rows)
        self._load_txos(output_rows)

        # locks: if you need to take several, acquire them in the order they are defined here!
        self.lock = threading.RLock()
        self.transaction_lock = threading.RLock()

        self.invoices = InvoiceService(self)
        self.requests = RequestService(self)

    def scriptpubkey_to_scripthash(self, script):
        script_bytes = bytes(script)
        return sha256(script_bytes)

    def get_id(self) -> int:
        return self._id

    def get_wallet(self) -> 'Wallet':
        return self._wallet

    def requires_input_transactions(self) -> bool:
        return any(k.requires_input_transactions() for k in self.get_keystores())

    def get_keyinstance(self, key_id: int) -> KeyInstanceRow:
        return self._keyinstances[key_id]

    def set_keyinstance(self, key_id: int, keyinstance: KeyInstanceRow) -> None:
        self._keyinstances[key_id] = keyinstance

    def get_keyinstance_ids(self) -> Sequence[int]:
        return tuple(self._keyinstances.keys())

    def get_next_derivation_index(self, derivation_path: Sequence[int]) -> int:
        raise NotImplementedError

    def allocate_keys(self, count: int,
            derivation_path: Sequence[int]) -> Sequence[DeterministicKeyAllocation]:
        return ()

    def get_fresh_keys(self, derivation_parent: Sequence[int], count: int) -> List[KeyInstanceRow]:
        raise NotImplementedError

    def get_gap_limit_for_path(self, subpath: Sequence[int]) -> int:
        return self._subpath_gap_limits.get(subpath, 20)  # defaults to 20

    def set_gap_limit_for_path(self, subpath: Sequence[int], limit: int) -> None:
        # TODO - this is an interim step towards persisting these settings via the
        #  database and allowing for modification via the GUI preferences Accounts tab
        self._subpath_gap_limits[subpath] = limit

    def create_keys_until(self, derivation: Sequence[int]) -> Sequence[KeyInstanceRow]:
        derivation_path = derivation[:-1]
        desired_index = derivation[-1]
        with self.lock:
            next_index = self.get_next_derivation_index(derivation_path)
            required_count = (desired_index - next_index) + 1
            assert required_count > 0, f"desired={desired_index}, current={next_index-1}"
            self._logger.debug("create_keys_until path=%s index=%d count=%d",
                derivation_path, desired_index, required_count)
            return self.create_keys(required_count, derivation_path)

    def create_keys(self, count: int, derivation_path: Sequence[int]) -> Sequence[KeyInstanceRow]:
        key_allocations = self.allocate_keys(count, derivation_path)
        return self.create_allocated_keys(key_allocations)

    def create_allocated_keys(self, key_allocations: Sequence[DeterministicKeyAllocation]) \
            -> Sequence[KeyInstanceRow]:
        if not len(key_allocations):
            return []

        keyinstances = [ KeyInstanceRow(-1, self.get_id(), ka.masterkey_id,
            ka.derivation_type, self.create_derivation_data(ka),
            self.create_derivation_data2(ka),
            KeyInstanceFlag.IS_ACTIVE, None) for ka in key_allocations ]
        rows = self._wallet.create_keyinstances(self._id, keyinstances)
        for i, row in enumerate(rows):
            self._keyinstances[row.keyinstance_id] = row
            self._keypath[row.keyinstance_id] = key_allocations[i].derivation_path
        self._add_activated_keys(rows)
        return rows

    def create_derivation_data(self, key_allocation: DeterministicKeyAllocation) -> bytes:
        assert key_allocation.derivation_type == DerivationType.BIP32_SUBPATH
        return json.dumps({ "subpath": key_allocation.derivation_path }).encode()

    def create_derivation_data2(self, key_allocation: DeterministicKeyAllocation) \
            -> Optional[bytes]:
        # `create_derivation_data` already checks the derivation type is `BIP32_SUBPATH`.
        return b''.join(pack_be_uint32(v) for v in key_allocation.derivation_path)

    def archive_keys(self, key_ids: Set[int]) -> Set[int]:
        assert len(key_ids), "should never be called with no keys to deactivate"
        candidate_key_ids: Set[int] = set()
        keyinstance_updates: List[Tuple[KeyInstanceFlag, int]] = []
        for key_id in key_ids:
            keyinstance = self._keyinstances.get(key_id)
            if keyinstance is None:
                continue
            assert keyinstance.flags & KeyInstanceFlag.IS_ACTIVE, \
                f"unexpected deactivated key {key_id}"
            # Persist the removal of the active state from the key.
            keyinstance_updates.append((keyinstance.flags & ~KeyInstanceFlag.ACTIVE_MASK, key_id))
            candidate_key_ids.add(key_id)

        if len(candidate_key_ids):
            self._unload_keys(candidate_key_ids)
            self._wallet.update_keyinstance_flags(keyinstance_updates)
        return candidate_key_ids

    def unarchive_transaction_keys(self, tx_key_ids: List[Tuple[bytes, Set[int]]]) -> None:
        """
        This should reload key and transaction output state for archived keys.
        """
        candidate_key_ids: Set[int] = set()
        for tx_hash, key_ids in tx_key_ids:
            candidate_key_ids |= key_ids

        assert len(candidate_key_ids), "should never be called with no keys to activate"

        for txo_row in self._wallet.read_transactionoutputs(key_ids=list(candidate_key_ids)):
            self._load_txo(txo_row)

        keyinstance_updates: List[Tuple[KeyInstanceFlag, int]] = []
        for row in self._wallet.read_keyinstances(key_ids=list(candidate_key_ids)):
            # TODO: Work out the correct thing to do for these assertions? Ignore these keys?
            assert row.keyinstance_id not in self._keyinstances
            assert row.flags & KeyInstanceFlag.IS_ACTIVE != KeyInstanceFlag.IS_ACTIVE

            flags = row.flags | KeyInstanceFlag.IS_ACTIVE
            self._keyinstances[row.keyinstance_id] = row._replace(flags=flags)
            keyinstance_updates.append((flags, row.keyinstance_id))

        if len(keyinstance_updates):
            self._wallet.update_keyinstance_flags(keyinstance_updates)

    def _unload_keys(self, key_ids: Set[int]) -> None:
        utxokeys, stxokeys = self.get_key_txokeys(key_ids)
        # Flush the associated UTXO state and account state from memory.
        with self._utxos_lock:
            for utxo_key in utxokeys:
                if utxo_key in self._frozen_coins:
                    self._frozen_coins.remove(utxo_key)
                del self._utxos[utxo_key]
        for stxokey in stxokeys:
            del self._stxos[stxokey]
        for key_id in key_ids:
            del self._keyinstances[key_id]

    def get_key_txokeys(self, key_ids: Set[int]) -> Tuple[List[TxoKeyType], List[TxoKeyType]]:
        with self._utxos_lock:
            utxo_keys = [ k for (k, v) in self._utxos.items() if v.keyinstance_id in key_ids ]
        stxo_keys = [ k for (k, v) in self._stxos.items() if v in key_ids ]
        return utxo_keys, stxo_keys

    def get_key_utxos(self, key_ids: Set[int]) -> List[UTXO]:
        with self._utxos_lock:
            return [ u for u in self._utxos.values() if u.keyinstance_id in key_ids ]

    def get_script_template_for_id(self, keyinstance_id: int, script_type: ScriptType) \
            -> ScriptTemplate:
        raise NotImplementedError

    def get_possible_scripts_for_id(self, keyinstance_id: int) -> List[Tuple[ScriptType, Script]]:
        script_types = ACCOUNT_SCRIPT_TYPES.get(self.type())
        if script_types is None:
            raise NotImplementedError
        return [ (script_type,
                self.get_script_template_for_id(keyinstance_id, script_type).to_script())
            for script_type in script_types ]

    def get_script_for_id(self, keyinstance_id: int, script_type: ScriptType) -> Script:
        script_template = self.get_script_template_for_id(keyinstance_id, script_type)
        return script_template.to_script()

    # This is started by the network `maintain_account` loop.
    async def synchronize_loop(self) -> None:
        while True:
            await self._synchronize()
            await self._synchronize_event.wait()

    async def _synchronize_account(self) -> None:
        '''Class-specific synchronization (generation of missing addresses).'''
        pass

    async def _synchronize(self) -> None:
        self._logger.debug('synchronizing...')
        self._synchronize_event.clear()
        self._synchronized_event.clear()
        await self._synchronize_account()
        self._synchronized_event.set()
        self._logger.debug('synchronized.')
        if self._network:
            self._network.trigger_callback('updated')

    def synchronize(self) -> None:
        app_state.async_.spawn_and_wait(self._trigger_synchronization)
        app_state.async_.spawn_and_wait(self._synchronized_event.wait)

    async def _trigger_synchronization(self) -> None:
        if self._network:
            self._synchronize_event.set()
        else:
            await self._synchronize()

    def is_synchronized(self) -> bool:
        return (self._synchronized_event.is_set() and
                not (self._network and self._wallet.missing_transactions()))

    def get_keystore(self) -> Optional[KeyStore]:
        if self._row.default_masterkey_id is not None:
            return self._wallet.get_keystore(self._row.default_masterkey_id)
        return self._default_keystore

    def get_keystores(self) -> Sequence[KeyStore]:
        keystore = self.get_keystore()
        return [ keystore ] if keystore is not None else []

    def get_master_public_key(self):
        return None

    def have_transaction(self, tx_hash: bytes) -> bool:
        return self._wallet._transaction_cache.is_cached(tx_hash)

    def has_received_transaction(self, tx_hash: bytes) -> bool:
        # At this time, this means received over the P2P network.
        flags = self._wallet._transaction_cache.get_flags(tx_hash)
        return flags is not None and (flags & (TxFlags.StateCleared | TxFlags.StateSettled)) != 0

    def get_transaction(self, tx_hash: bytes, flags: Optional[int]=None) -> Optional[Transaction]:
        tx = self._wallet._transaction_cache.get_transaction(tx_hash, flags)
        if tx is not None:
            # Populate the description.
            desc = self.get_transaction_label(tx_hash)
            if desc:
                tx.context.description = desc
            return tx
        return None

    def get_transaction_entry(self, tx_hash: bytes, flags: Optional[int]=None,
            mask: Optional[int]=None) -> Optional[TransactionCacheEntry]:
        return self._wallet._transaction_cache.get_entry(tx_hash, flags, mask)

    def get_transaction_metadata(self, tx_hash: bytes) -> Optional[TxData]:
        return self._wallet._transaction_cache.get_metadata(tx_hash)

    def set_transaction_label(self, tx_hash: bytes, text: Optional[str]) -> None:
        self.set_transaction_labels([ (tx_hash, text) ])

    def set_transaction_labels(self, entries: List[Tuple[bytes, Optional[str]]]) -> None:
        update_entries = []
        for tx_hash, value in entries:
            text = None if value is None or value.strip() == "" else value.strip()
            label = self._transaction_descriptions.get(tx_hash)
            if label != text:
                if label is not None and value is None:
                    del self._transaction_descriptions[tx_hash]
                update_entries.append((text, self._id, tx_hash))

        with self._wallet.get_account_transaction_table() as table:
            table.update_descriptions(update_entries)

        for text, _account_id, tx_hash in update_entries:
            app_state.app.on_transaction_label_change(self, tx_hash, text)

    def get_transaction_label(self, tx_hash: bytes) -> str:
        label = self._transaction_descriptions.get(tx_hash)
        return "" if label is None else label

    def __str__(self) -> str:
        return self.name()

    def get_name(self) -> str:
        return self._row.account_name

    def set_name(self, name: str) -> None:
        with AccountTable(self._wallet._db_context) as table:
            table.update_name([ (self._row.account_id, name) ])

        self._row = AccountRow(self._row.account_id, self._row.default_masterkey_id,
            self._row.default_script_type, name)

        self._wallet.trigger_callback('on_account_renamed', self._id, name)

    # Displayed in the regular user UI.
    def display_name(self) -> str:
        return self._row.account_name if self._row.account_name else _("unnamed account")

    # Displayed in the advanced user UI/logs.
    def name(self) -> str:
        parent_name = self._wallet.name()
        return f"{parent_name}/{self._id}"

    # Used for exception reporting account class instance classification.
    def type(self) -> AccountType:
        return AccountType.UNSPECIFIED

    # Used for exception reporting overall account classification.
    def debug_name(self) -> str:
        k = self.get_keystore()
        if k is None:
            return self.type().value
        return f"{self.type().value}/{k.debug_name()}"

    def _load_keys(self, keyinstance_rows: List[KeyInstanceRow]) -> None:
        pass

    def _load_txos(self, output_rows: List[TransactionOutputRow]) -> None:
        self._stxos.clear()
        self._utxos.clear()
        self._frozen_coins: Set[TxoKeyType] = set([])

        for row in output_rows:
            self._load_txo(row)

    def _load_txo(self, row: TransactionOutputRow) -> None:
        assert row.keyinstance_id is not None
        assert row.script_type is not None
        txo_key = TxoKeyType(row.tx_hash, row.tx_index)
        if row.flags & TransactionOutputFlag.IS_SPENT:
            self._stxos[txo_key] = row.keyinstance_id
        else:
            script_template = self.get_script_template_for_id(row.keyinstance_id, row.script_type)
            address = script_template if isinstance(script_template, Address) else None
            self.register_utxo(row.tx_hash, row.tx_index, row.value, row.flags,
                row.keyinstance_id, row.script_type, script_template.to_script(), address)

    def register_utxo(self, tx_hash: bytes, output_index: int, value: int,
            flags: TransactionOutputFlag, keyinstance_id: int, script_type: ScriptType,
            script: Script, address: Optional[ScriptTemplate]=None) -> None:
        is_coinbase = (flags & TransactionOutputFlag.IS_COINBASE) != 0
        utxo_key = TxoKeyType(tx_hash, output_index)
        with self._utxos_lock:
            self._utxos[utxo_key] = UTXO(
                value=value,
                script_pubkey=script,
                script_type=script_type,
                tx_hash=tx_hash,
                out_index=output_index,
                keyinstance_id=keyinstance_id,
                flags=flags,
                address=address,
                is_coinbase=is_coinbase)
            if flags & TransactionOutputFlag.IS_FROZEN:
                if flags & TransactionOutputFlag.IS_SPENT:
                    self._logger.warning("Ignoring frozen flag for spent txo %s:%d",
                        hash_to_hex_str(tx_hash), output_index)
                    return
                self._frozen_coins.add(utxo_key)

    # # Should be called with the transaction lock.
    # def create_transaction_output(self, tx_hash: bytes, output_index: int, value: int,
    #         flags: TransactionOutputFlag, keyinstance_id: int, script_type: ScriptType,
    #         script: Script, address: Optional[ScriptTemplate]=None) -> None:
    #     if flags & TransactionOutputFlag.IS_SPENT:
    #         self._stxos[TxoKeyType(tx_hash, output_index)] = keyinstance_id
    #     else:
    #         self.register_utxo(tx_hash, output_index, value, flags, keyinstance_id,
    #             script_type, script, address)

    #     # TODO(nocheckin) what about the script offset and length
    #     self._wallet.create_transactionoutputs(self._id, [ TransactionOutputRow(tx_hash,
    #         output_index, value, keyinstance_id, script_type,
    #         scripthash_bytes(script), flags) ])

    def is_deterministic(self) -> bool:
        # Not all wallets have a keystore, like imported address for instance.
        keystore = self.get_keystore()
        return keystore is not None and keystore.is_deterministic()

    def involves_hardware_wallet(self) -> bool:
        return any([ k for k in self.get_keystores() if isinstance(k, Hardware_KeyStore) ])

    def get_label_data(self) -> Dict[str, Any]:
        # Create exported data structure for account labels/descriptions.
        def _derivation_path(key_id: int) -> Optional[str]:
            derivation = self._keypath.get(key_id)
            return None if derivation is None else compose_chain_string(derivation)
        label_entries = [ (_derivation_path(key.keyinstance_id),  key.description)
            for key in self._keyinstances.values() if key.description is not None ]

        with AccountTransactionTable(self._wallet._db_context) as table:
            rows = table.read_descriptions(self._id)
        transaction_entries = [ (hash_to_hex_str(tx_hash), description)
            for tx_hash, description in rows ]

        data: Dict[str, Any] = {}
        if len(transaction_entries):
            data["transactions"] = transaction_entries
        if len(label_entries):
            data["keys"] = {
                "account_fingerprint": self.get_fingerprint().hex(),
                "entries": label_entries,
            }
        return data

    def get_keyinstance_label(self, key_id: int) -> str:
        return self._keyinstances[key_id].description or ""

    def set_keyinstance_label(self, key_id: int, text: Optional[str]) -> None:
        text = None if text is None or text.strip() == "" else text.strip()
        key = self._keyinstances[key_id]
        if key.description == text:
            return
        self._keyinstances[key_id] = key._replace(description=text)
        self._wallet.update_keyinstance_descriptions([ (text, key_id) ])
        app_state.app.on_keyinstance_label_change(self, key_id, text)

    def get_dummy_script_template(self, script_type: Optional[ScriptType]=None) -> ScriptTemplate:
        public_key = PrivateKey(os.urandom(32)).public_key
        return self.get_script_template(public_key, script_type)

    def get_script_template(self, public_key: PublicKey,
            script_type: Optional[ScriptType]=None) -> ScriptTemplate:
        if script_type is None:
            script_type = self.get_default_script_type()
        return get_single_signer_script_template(public_key, script_type)

    def get_default_script_type(self) -> ScriptType:
        return ScriptType(self._row.default_script_type)

    def set_default_script_type(self, script_type: ScriptType) -> None:
        if script_type == self._row.default_script_type:
            return
        self._wallet.update_account_script_types([ (script_type, self._row.account_id) ])
        self._row = self._row._replace(default_script_type=script_type)

    def get_key_paths(self) -> Dict[int, Sequence[int]]:
        return self._keypath

    def get_derivation_path(self, keyinstance_id: int) -> Optional[Sequence[int]]:
        return self._keypath.get(keyinstance_id)

    def get_derivation_path_text(self, keyinstance_id: int) -> Optional[str]:
        derivation = self._keypath.get(keyinstance_id)
        if derivation is not None:
            return compose_chain_string(derivation)
        return None

    def get_keyinstance_id_for_derivation(self, derivation: Sequence[int]) -> Optional[int]:
        for keyinstance_id, keypath in self._keypath.items():
            if keypath == derivation:
                return keyinstance_id
        return None

    def get_threshold(self, script_type: ScriptType) -> int:
        assert script_type in (ScriptType.P2PKH, ScriptType.P2PK), \
            f"get_threshold got bad script type {script_type}"
        return 1

    def export_private_key(self, keyinstance_id: int, password: str) -> Optional[str]:
        """ extended WIF format """
        if self.is_watching_only():
            return None
        keyinstance = self._keyinstances[keyinstance_id]
        keystore = self._wallet.get_keystore(keyinstance.masterkey_id)
        derivation_path = self.get_derivation_path(keyinstance_id)
        secret, compressed = keystore.get_private_key(derivation_path, password)
        return PrivateKey(secret).to_WIF(compressed=compressed, coin=Net.COIN)

    # Should be called with the transaction lock.
    def set_utxo_spent(self, tx_hash: bytes, output_index: int) -> None:
        with self._utxos_lock:
            txo_key = TxoKeyType(tx_hash, output_index)
            utxo = self._utxos.pop(txo_key)
        retained_flags = utxo.flags & TransactionOutputFlag.IS_COINBASE
        self._wallet.update_transactionoutput_flags(
            [ (retained_flags | TransactionOutputFlag.IS_SPENT, tx_hash, output_index)  ])
        self._stxos[txo_key] = utxo.keyinstance_id

    def is_frozen_utxo(self, utxo):
        return utxo.key() in self._frozen_coins

    def get_stxo(self, tx_hash: bytes, output_index: int) -> Optional[int]:
        return self._stxos.get(TxoKeyType(tx_hash, output_index), None)

    def get_utxo(self, tx_hash: bytes, output_index: int) -> Optional[UTXO]:
        return self._utxos.get(TxoKeyType(tx_hash, output_index), None)

    def get_spendable_coins(self, domain: Optional[List[int]], config) -> List[UTXO]:
        confirmed_only = config.get('confirmed_only', False)
        utxos = self.get_utxos(exclude_frozen=True, mature=True, confirmed_only=confirmed_only)
        if domain is not None:
            return [ utxo for utxo in utxos if utxo.keyinstance_id in domain ]
        return utxos

    def get_utxos(self, exclude_frozen=False, mature=False, confirmed_only=False) -> List[UTXO]:
        '''Note exclude_frozen=True checks for coin-level frozen status. '''
        mempool_height = self._wallet.get_local_height() + 1
        def is_spendable_utxo(utxo):
            metadata = self.get_transaction_metadata(utxo.tx_hash)
            if exclude_frozen and self.is_frozen_utxo(utxo):
                return False
            if confirmed_only and metadata.height <= 0:
                return False
            # A coin is spendable at height + COINBASE_MATURITY)
            if mature and utxo.is_coinbase and \
                    mempool_height < metadata.height + COINBASE_MATURITY:
                return False
            return True
        with self._utxos_lock:
            return [ utxo for utxo in self._utxos.values() if is_spendable_utxo(utxo)]

    def existing_active_keys(self) -> List[int]:
        with self._activated_keys_lock:
            self._activated_keys = []
            return [ key_id for (key_id, key) in self._keyinstances.items()
                if key.flags & KeyInstanceFlag.IS_ACTIVE ]

    def get_frozen_balance(self) -> Tuple[int, int, int]:
        with self._utxos_lock:
            return self.get_balance(self._frozen_coins)

    def get_balance(self, domain=None, exclude_frozen_coins: bool=False) -> Tuple[int, int, int]:
        with self._utxos_lock:
            if domain is None:
                domain = set(self._utxos.keys())
            c = u = x = 0
            for k in domain:
                if exclude_frozen_coins and k in self._frozen_coins:
                    continue
                o = self._utxos[k]
                metadata = cast(TxData, self.get_transaction_metadata(o.tx_hash))
                metadata_height = cast(int, metadata.height)
                if o.is_coinbase and metadata_height + COINBASE_MATURITY > \
                        self._wallet.get_local_height():
                    x += o.value
                elif metadata_height > 0:
                    c += o.value
                else:
                    u += o.value
            return c, u, x

    # NOTE(rt12): Only called by `maybe_set_transaction_dispatched`. Has limited utility.
    def _set_transaction_state(self, tx_hash: bytes, flags: TxFlags) -> None:
        with self.transaction_lock:
            if not self.have_transaction(tx_hash):
                raise UnknownTransactionException(f"tx {hash_to_hex_str(tx_hash)} unknown")
            existing_flags = self._wallet._transaction_cache.get_flags(tx_hash)
            updated_flags = self._wallet._transaction_cache.update_flags(tx_hash, flags,
                ~TxFlags.STATE_MASK)
        self._wallet.trigger_callback('transaction_state_change', self._id, tx_hash,
            existing_flags, updated_flags)

    def maybe_set_transaction_dispatched(self, tx_hash: bytes) -> bool:
        """
        We should only ever mark a transaction as dispatched if it hasn't already been broadcast.
        raises UnknownTransactionException
        """
        with self.transaction_lock:
            if not self.have_transaction(tx_hash):
                raise UnknownTransactionException(f"tx {hash_to_hex_str(tx_hash)} unknown")
            tx_flags = self._wallet.get_transaction_cache().get_flags(tx_hash)
            if tx_flags & (TxFlags.StateDispatched | TxFlags.STATE_BROADCAST_MASK) == 0:
                self._set_transaction_state(tx_hash, TxFlags.StateDispatched)
                return True
            return False

    # def process_key_usage(self, tx_hash: bytes, tx: Transaction,
    #         relevant_txos: Optional[List[Tuple[int, XTxOutput]]]) -> bool:
    #     with self.transaction_lock:
    #         return self._process_key_usage(tx_hash, tx, relevant_txos)

    # def _process_key_usage(self, tx_hash: bytes, tx: Transaction) -> None:
    #     import cProfile, pstats, io
    #     from pstats import SortKey
    #     pr = cProfile.Profile()
    #     pr.enable()
    #     self._process_key_usage2(tx_hash, tx)
    #     pr.disable()
    #     s = io.StringIO()
    #     sortby = SortKey.CUMULATIVE
    #     ps = pstats.Stats(pr, stream=s).sort_stats(sortby)
    #     ps.print_stats()
    #     print(s.getvalue())

    # def _process_key_usage(self, tx_hash: bytes, tx: Transaction,
    #         relevant_txos: Optional[List[Tuple[int, XTxOutput]]]) -> bool:
    #     tx_id = hash_to_hex_str(tx_hash)
    #     key_ids = self._sync_state.get_transaction_key_ids(tx_id)
    #     # key_matches = [(self.get_keyinstance(key_id),
    #     #     *self._get_cached_script(key_id)) for key_id in key_ids]

    #     base_txo_flags = TransactionOutputFlag.IS_COINBASE if tx.is_coinbase() \
    #         else TransactionOutputFlag.NONE
    #     tx_deltas: Dict[Tuple[bytes, int], int] = defaultdict(int)
    #     new_txos: List[Tuple[bytes, int, int, TransactionOutputFlag, KeyInstanceRow,
    #         ScriptTemplate]] = []
    #     for output_index, output in relevant_txos or enumerate(tx.outputs):
    #         utxo = self.get_utxo(tx_hash, output_index)
    #         if utxo is not None:
    #             continue
    #         keyinstance_id = self.get_stxo(tx_hash, output_index)
    #         if keyinstance_id is not None:
    #             continue

    #         output_bytes = bytes(output.script_pubkey)
    #         for keyinstance, script, script_bytes, address in key_matches:
    #             if script_bytes == output_bytes:
    #                 break
    #         else:
    #             continue

    #         # Search the known candidates to see if we already have this txo's spending input.
    #         txo_flags = base_txo_flags
    #         for spend_tx_id, _height in self._sync_state.get_key_history(
    #                 keyinstance.keyinstance_id):
    #             if spend_tx_id == tx_id:
    #                 continue
    #             spend_tx_hash = hex_str_to_hash(spend_tx_id)
    #             spend_tx = self._wallet._transaction_cache.get_transaction(spend_tx_hash)
    #             if spend_tx is None:
    #                 continue
    #             for spend_txin in spend_tx.inputs:
    #                 if spend_txin.prev_hash == tx_hash and spend_txin.prev_idx == output_index:
    #                     break
    #             else:
    #                 continue

    #             tx_deltas[(spend_tx_hash, keyinstance.keyinstance_id)] -= output.value
    #             txo_flags |= TransactionOutputFlag.IS_SPENT
    #             break

    #         # TODO(rt12) BACKLOG batch create the outputs.
    #         # TODO(nocheckin) keyinstance.script_type has been removed, need alternate source
    #         # if this logic is kept in any way.
    #         self.create_transaction_output(tx_hash, output_index, output.value,
    #             txo_flags, keyinstance.keyinstance_id, script_type, script, address)
    #         tx_deltas[(tx_hash, keyinstance.keyinstance_id)] += output.value

    #     for input_index, input in enumerate(tx.inputs):
    #         keyinstance_id = self.get_stxo(input.prev_hash, input.prev_idx)
    #         if keyinstance_id is not None:
    #             continue
    #         utxo = self.get_utxo(input.prev_hash, input.prev_idx)
    #         if utxo is None:
    #             continue

    #         self.set_utxo_spent(input.prev_hash, input.prev_idx)
    #         tx_deltas[(tx_hash, utxo.keyinstance_id)] -= utxo.value

    #     if len(tx_deltas):
    #         check_keyinstance_ids = set(r[1] for r in tx_deltas.keys())
    #         self._wallet.create_or_update_transactiondelta_relative(
    #             [ TransactionDeltaRow(k[0], k[1], v) for k, v in tx_deltas.items() ],
    #             partial(self.requests.check_paid_requests, check_keyinstance_ids))

    #         affected_keys = [self._keyinstances[k] for (_x, k) in tx_deltas.keys()]
    #         self._wallet.trigger_callback('on_keys_updated', self._id, affected_keys)

    #         return True

    #     return False

    def delete_transaction(self, tx_hash: bytes) -> None:
        # Invoices have foreign key on the transaction.
        tx_flags = self._wallet.get_transaction_cache().get_flags(tx_hash)
        if tx_flags & TxFlags.PaysInvoice:
            # This does not mean the transaction is still referenced by the invoice, but it
            # costs us little to just go ahead and clear it.
            self.invoices.clear_invoice_transaction(tx_hash)

        def _completion_callback(exc_value: Any) -> None:
            if exc_value is not None:
                raise exc_value # pylint: disable=raising-bad-type

            self._wallet.trigger_callback('transaction_deleted', self._id, tx_hash)

        tx_id = hash_to_hex_str(tx_hash)
        with self.transaction_lock:
            self._logger.debug("removing tx from history %s", tx_id)
            self._remove_transaction(tx_hash)
            self._logger.debug("deleting tx from cache and datastore: %s", tx_id)
            self._wallet._transaction_cache.delete(tx_hash, _completion_callback)

    def _remove_transaction(self, tx_hash: bytes) -> None:
        with self.transaction_lock:
            self._logger.debug("removing transaction %s", hash_to_hex_str(tx_hash))

            tx = self._wallet._transaction_cache.get_transaction(tx_hash)
            # tx_deltas: Dict[Tuple[bytes, int], int] = defaultdict(int)

            txo_key: TxoKeyType
            utxos: List[UTXO] = []
            for output_index, txout in enumerate(tx.outputs):
                txo_key = TxoKeyType(tx_hash, output_index)
                # Check if any outputs of this transaction have been spent already.
                if txo_key in self._stxos:
                    raise Exception("Cannot remove as spent by child")

                with self._utxos_lock:
                    if txo_key in self._utxos:
                        utxos.append(self._utxos[txo_key])

            # Collect the spent key metadata.
            candidate_spent_keys: Dict[TxoKeyType, int] = {}
            for input_index, txin in enumerate(tx.inputs):
                txo_key = TxoKeyType(txin.prev_hash, txin.prev_idx)
                if txo_key in self._stxos:
                    spent_keyinstance_id = self._stxos.pop(txo_key)
                    candidate_spent_keys[txo_key] = spent_keyinstance_id

            # Read the transaction outputs for any collected spent keys.
            txos: Dict[TxoKeyType, TransactionOutputRow] = {}
            with TransactionOutputTable(self._wallet._db_context) as table:
                output_rows = table.read(key_ids=list(candidate_spent_keys.values()))
                txos.update((TxoKeyType(row.tx_hash, row.tx_index), row) for row in output_rows)

            txout_flags: List[Tuple[TransactionOutputFlag, bytes, int]] = []
            for txo_key, spent_keyinstance_id in candidate_spent_keys.items():
                txo = txos[txo_key]
                # Need to set the TXO to non-spent.
                # tx_deltas[(txin.prev_hash, spent_keyinstance_id)] = txo.value
                txo_flags = txo.flags & ~TransactionOutputFlag.IS_SPENT
                script_template = self.get_script_template_for_id(spent_keyinstance_id,
                    txo.script_type)
                script = script_template.to_script()
                address = script_template if isinstance(script_template, Address) else None
                self.register_utxo(txo_key.tx_hash, txo_key.tx_index, txo.value, txo_flags,
                    spent_keyinstance_id, txo.script_type, script, address)
                txout_flags.append((txo_flags, txo_key.tx_hash, txo_key.tx_index))

            for utxo in utxos:
                # Update the cached key to be unused.
                key = self._keyinstances[utxo.keyinstance_id]
                # TODO(nocheckin) what replaces this?
                # self._keyinstances[utxo.keyinstance_id] =
                #   key._replace(script_type=ScriptType.NONE)

                # Expunge the UTXO.
                utxo_key = utxo.key()
                with self._utxos_lock:
                    if utxo_key in self._frozen_coins:
                        self._frozen_coins.remove(utxo_key)
                    del self._utxos[utxo_key]

            if len(txout_flags):
                self._wallet.update_transactionoutput_flags(txout_flags)

            # if len(tx_deltas):
            #     self._wallet.create_or_update_transactiondelta_relative(
            #         [ TransactionDeltaRow(k[0], k[1], v) for k, v in tx_deltas.items() ])

    # def get_key_history(self, keyinstance_id: int,
    #         script_type: ScriptType) -> List[Tuple[str, int]]:
    #     keyinstance = self._keyinstances[keyinstance_id]
    #     if keyinstance.script_type in (ScriptType.NONE, script_type):
    #         return self._sync_state.get_key_history(keyinstance_id)
    #     # This is normal for multi-script monitoring key registrations (fresh keys).
    #     # self._logger.warning("Received key history request from server for key that already "
    #     #     f"has script type {keyinstance.script_type}, where server history relates "
    #     #     f"to script type {script_type}. ElectrumSV has never handled this in the "
    #     #     f"past, and will ignore it for now. Please report it.")
    #     return []

    # Called by network.
    # async def set_key_history(self, keyinstance_id: int, script_type: ScriptType,
    #         hist: List[Tuple[str, int]], tx_fees: Dict[str, int]) -> None:
    #     if self._stopped:
    #         self._logger.debug("set_key_history on stopped wallet: %s", keyinstance_id)
    #         return

    #     # TODO: Re-enable network synchronisation.
    #     return

    #     # We need to delay post-processing until all of the following are completed:
    #     # - Any adds are written to the database.
    #     # - Any updates are written to the database.
    #     # - The key usage has been processed.
    #     # As some of the events may read from the database or access wallet state.
    #     update_state_changes: List[Tuple[bytes, TxFlags, TxFlags]] = []
    #     pending_event_count = 1

    #     def do_post_processing() -> None:
    #         nonlocal update_state_changes
    #         self._logger.debug("set_key_history post-processing %d state changes",
    #             len(update_state_changes))
    #         for state_change in update_state_changes:
    #             self._wallet.trigger_callback('transaction_state_change', self._id, *state_change)

    #         self._wallet.txs_changed_event.set()
    #         self.synchronize()

    #     def on_event_completed() -> None:
    #         nonlocal pending_event_count
    #         pending_event_count -= 1
    #         if pending_event_count > 0:
    #             return

    #         # We do not want to block the completion thread.
    #         app_state.app.run_in_thread(do_post_processing)

    #     with self.lock:
    #         self._logger.debug("set_key_history key_id=%s fees=%s", keyinstance_id, tx_fees)
    #         key = self._keyinstances[keyinstance_id]
    #         if key.script_type == ScriptType.NONE: # TODO(nocheckin) no keyinstance script type
    #             # This is the first use of the allocated key and we update the key to reflect it.
    #             self._keyinstances[keyinstance_id] = key._replace(script_type=script_type)
    #             # TODO(nocheckin) this no longer works and has been removed.
    #             self._wallet.update_keyinstance_script_types([ (script_type, keyinstance_id) ])
    #         elif key.script_type != script_type: # TODO(nocheckin) no keyinstance script type
    #             self._logger.error("Received key history from server for key that already "
    #                 f"has script type {key.script_type}, where server history relates "
    #                 f"to script type {script_type}. ElectrumSV has never handled this in the "
    #                 f"past, and will ignore it for now. Please report it. History={hist}")
    #             return

    #         # The history is in immediately usable order. Transactions are listed in ascending
    #         # block height (height > 0), followed by the unconfirmed (height == 0) and then
    #         # those with unconfirmed parents (height < 0). [ (tx_hash, tx_height), ... ]
    #         self._sync_state.set_key_history(keyinstance_id, hist)

    #         adds = []
    #         updates = []
    #         unique_tx_hashes: Set[bytes] = set([])
    #         for tx_id, tx_height in hist:
    #             tx_fee = tx_fees.get(tx_id, None)
    #             data = TxData(height=tx_height, fee=tx_fee)
    #             # The metadata flags indicate to the update call which TxData fields should
    #             # be updated. Fields that are not flagged in the existing cache record, should
    #             # remain as they are.
    #             flags = TxFlags.HasHeight
    #             if tx_fee is not None:
    #                 flags |= TxFlags.HasFee
    #             tx_hash = hex_str_to_hash(tx_id)
    #             entry_flags = self._wallet._transaction_cache.get_flags(tx_hash)
    #             if entry_flags is None:
    #                 adds.append((tx_hash, data, None, flags, None))
    #             else:
    #                 # If a transaction has bytedata at this point, but no state, then it is
    #                 # likely that we added it locally and broadcast it ourselves. Transactions
    #                 # without bytedata cannot have a state.
    #                 if entry_flags & \
    #                         (TxFlags.ZHasByteData|TxFlags.StateCleared|TxFlags.StateSettled) \
    #                         == TxFlags.ZHasByteData:
    #                     flags |= TxFlags.StateCleared
    #                     # Event workaround.
    #                     update_state_changes.append((tx_hash, entry_flags & TxFlags.STATE_MASK,
    #                         flags & TxFlags.STATE_MASK))
    #                 updates.append((tx_hash, data, None, flags))
    #             unique_tx_hashes.add(tx_hash)

    #         def _completion_callback(exc_value: Any) -> None:
    #             if exc_value is not None:
    #                 raise exc_value # pylint: disable=raising-bad-type
    #             on_event_completed()

    #         if len(adds):
    #             # The completion callback is guaranteed to be called.
    #             pending_event_count += 1
    #             self._wallet._transaction_cache.add(adds,
    #                 completion_callback=_completion_callback)

    #         if len(updates):
    #             # The completion callback is only guaranteed to be called if database updates are
    #             # actually made. We can infer this from the return value which is how many are.
    #             pending_event_count += 1
    #             if self._wallet._transaction_cache.update(updates,
    #                 completion_callback=_completion_callback) == 0:
    #                     pending_event_count -= 1

    #         for tx_id, tx_height in hist:
    #             tx_hash = hex_str_to_hash(tx_id)
    #             tx = self._wallet._transaction_cache.get_transaction(tx_hash)
    #             if tx is not None:
    #                 # relevant_txos = self.get_relevant_txos(keyinstance_id, tx, tx_id)
    #                 self.process_key_usage(tx_hash, tx, relevant_txos)

    #     # Reaching this stage is the only guaranteed event in triggering post-processing.
    #     on_event_completed()

    def get_paid_requests(self, keyinstance_ids: Sequence[int]) -> List[int]:
        return db_functions.read_paid_requests(self._wallet._db_context, self._id, keyinstance_ids)

    def get_balance2(self, flags: Optional[int]=None, mask: Optional[int]=None) \
            -> TransactionDeltaSumRow:
        return db_functions.read_account_balance(self._wallet._db_context, self._id, flags, mask)

    def get_key_list(self, keyinstance_ids: Optional[List[int]]=None) -> List[KeyListRow]:
        return db_functions.read_key_list(self._wallet._db_context, self._id, keyinstance_ids)

    def get_history(self, domain: Optional[Set[int]]=None) -> List[Tuple[HistoryLine, int]]:
        """
        Return the list of transactions in the account kind of sorted from newest to oldest.

        Sorting is nuanced, in that transactions that are in a block are sorted by both block
        height and position. Transactions that are not in a block are ordered according to when
        they were added to the account.

        This is called for three uses:
        - The transaction list in the history tab.
        - The transaction list in the key usage window.
        - Exporting the account history.
        """
        history_raw: List[HistoryLine] = []

        for row in db_functions.read_history_list(self._wallet._db_context, self._id, domain):
            if row.block_position is not None:
                sort_key = row.block_height, row.block_position
            else:
                sort_key = (1e9, row.date_added)
            history_raw.append(HistoryLine(sort_key, row.tx_hash, row.tx_flags, row.block_height,
                row.value_delta))

        history_raw.sort(key = lambda v: v.sort_key)

        history: List[Tuple[HistoryLine, int]] = []
        balance = 0
        for history_line in history_raw:
            balance += history_line.value_delta
            history.append((history_line, balance))

        history.reverse()

        return history

    def export_history(self, from_timestamp=None, to_timestamp=None,
                       show_addresses=False):
        h = self.get_history()
        fx = app_state.fx
        out = []

        network = app_state.daemon.network
        chain = app_state.headers.longest_chain()
        backfill_headers = network.backfill_headers_at_heights
        header_at_height = app_state.headers.header_at_height
        server_height = network.get_server_height() if network else 0
        for history_line, balance in h:
            try:
                timestamp = timestamp_to_datetime(header_at_height(chain,
                                history_line.height).timestamp)
            except MissingHeader:
                if history_line.height > 0:
                    self._logger.debug("fetching missing headers at height: %s",
                                       history_line.height)
                    assert history_line.height <= server_height, "inconsistent blockchain data"
                    backfill_headers([history_line.height])
                    timestamp = timestamp_to_datetime(header_at_height(chain,
                                    history_line.height).timestamp)
                else:
                    timestamp = datetime.now()
            if from_timestamp and timestamp < from_timestamp:
                continue
            if to_timestamp and timestamp >= to_timestamp:
                continue
            item = {
                'txid': hash_to_hex_str(history_line.tx_hash),
                'height': history_line.height,
                'timestamp': timestamp.isoformat(),
                'value': format_satoshis(history_line.value_delta,
                            is_diff=True) if history_line.value_delta is not None else '--',
                'balance': format_satoshis(balance),
                'label': self.get_transaction_label(history_line.tx_hash)
            }
            if fx:
                date = timestamp
                item['fiat_value'] = fx.historical_value_str(history_line.value_delta, date)
                item['fiat_balance'] = fx.historical_value_str(balance, date)
            out.append(item)
        return out

    def create_extra_outputs(self, coins: List[UTXO], outputs: List[XTxOutput], \
            force: bool=False) -> List[XTxOutput]:
        # Hardware wallets can only sign a limited range of output types (not OP_FALSE OP_RETURN).
        if self.involves_hardware_wallet() or len(coins) == 0:
            return []

        ## Extra: Add an output that is not compatible with Bitcoin Cash.
        if not force and not self._wallet.get_boolean_setting(WalletSettings.ADD_SV_OUTPUT):
            return []

        # We use the first signing public key from the first of the ordered UTXOs, for most coin
        # script types there will only be one signing public key, with the exception of
        # multi-signature accounts.
        ordered_coins = sorted(coins, key=lambda v: v.keyinstance_id)
        for public_key in self.get_public_keys_for_id(ordered_coins[0].keyinstance_id):
            raw_payload_bytes = push_item(os.urandom(random.randrange(32)))
            payload_bytes = public_key.encrypt_message(raw_payload_bytes)
            script_bytes = pack_byte(Ops.OP_0) + pack_byte(Ops.OP_RETURN) + push_item(payload_bytes)
            script = Script(script_bytes)
            # NOTE(rt12) This seems to be some attrs/mypy clash, the base class attrs should come
            # before the XTxOutput attrs, but typing expects these to be the XTxOutput attrs.
            return [XTxOutput(0, script)] # type: ignore

        return []

    def dust_threshold(self):
        return dust_threshold(self._network)

    def make_unsigned_transaction(self, utxos: List[UTXO], outputs: List[XTxOutput],
            config: SimpleConfig, fixed_fee: Optional[int]=None) -> Transaction:
        # check outputs
        all_index = None
        for n, output in enumerate(outputs):
            if output.value is all:
                if all_index is not None:
                    raise ValueError("More than one output set to spend max")
                all_index = n

        # Avoid index-out-of-range with inputs[0] below
        if not utxos:
            raise NotEnoughFunds()

        if fixed_fee is None and config.fee_per_kb() is None:
            raise Exception('Dynamic fee estimates not available')

        fee_estimator = config.estimate_fee if fixed_fee is None else lambda size: fixed_fee
        inputs = [utxo.to_tx_input(self) for utxo in utxos]
        if all_index is None:
            # Let the coin chooser select the coins to spend
            # TODO(rt12) BACKLOG Hardware wallets should use 1 change at most. Make sure the
            # corner case of the active multisig cosigning wallet being hardware is covered.
            max_change = self.max_change_outputs \
                if self._wallet.get_boolean_setting(WalletSettings.MULTIPLE_CHANGE) else 1
            if self._wallet.get_boolean_setting(WalletSettings.USE_CHANGE) and \
                    self.is_deterministic():
                script_type = self.get_default_script_type()
                change_keyinstances = self.get_fresh_keys(CHANGE_SUBPATH, max_change)
                change_outs = []
                for keyinstance in change_keyinstances:
                    change_outs.append(XTxOutput(0, # type: ignore
                        self.get_script_for_id(keyinstance.keyinstance_id, script_type),
                        script_type,
                        self.get_xpubkeys_for_id(keyinstance.keyinstance_id))) # type: ignore
            else:
                change_outs = [ XTxOutput(0, utxos[0].script_pubkey, # type: ignore
                    inputs[0].script_type, inputs[0].x_pubkeys) ] # type: ignore
            coin_chooser = coinchooser.CoinChooserPrivacy()
            tx = coin_chooser.make_tx(inputs, outputs, change_outs, fee_estimator,
                self.dust_threshold())
        else:
            assert all(txin.value is not None for txin in inputs)
            sendable = cast(int, sum(txin.value for txin in inputs))
            outputs[all_index].value = 0
            tx = Transaction.from_io(inputs, outputs)
            fee = cast(int, fee_estimator(tx.estimated_size()))
            outputs[all_index].value = max(0, sendable - tx.output_value() - fee)
            tx = Transaction.from_io(inputs, outputs)

        # If user tries to send too big of a fee (more than 50
        # sat/byte), stop them from shooting themselves in the foot
        tx_in_bytes=tx.estimated_size()
        fee_in_satoshis=tx.get_fee()
        sats_per_byte=fee_in_satoshis/tx_in_bytes
        if sats_per_byte > 50:
           raise ExcessiveFee()

        # Sort the inputs and outputs deterministically
        tx.BIP_LI01_sort()
        # Timelock tx to current height.
        locktime = self._wallet.get_local_height()
        if locktime == -1: # We have no local height data (no headers synced).
            locktime = 0
        tx.locktime = locktime
        return tx

    def set_frozen_coin_state(self, utxos: List[UTXO], freeze: bool) -> None:
        '''Set frozen state of the COINS to FREEZE, True or False.  Note that coin-level freezing
        is set/unset independent of address-level freezing, however both must be satisfied for
        a coin to be defined as spendable.'''
        update_entries: List[Tuple[TransactionOutputFlag, bytes, int]] = []
        if freeze:
            self._frozen_coins.update(utxo.key() for utxo in utxos)
            update_entries.extend(
                (utxo.flags | TransactionOutputFlag.FROZEN_MASK, utxo.tx_hash, utxo.out_index)
                for utxo in utxos if (utxo.flags & TransactionOutputFlag.FROZEN_MASK !=
                    TransactionOutputFlag.FROZEN_MASK))
        else:
            self._frozen_coins.difference_update(utxo.key() for utxo in utxos)
            update_entries.extend(
                (utxo.flags & ~TransactionOutputFlag.FROZEN_MASK, utxo.tx_hash, utxo.out_index)
                for utxo in utxos if utxo.flags & TransactionOutputFlag.FROZEN_MASK != 0)
        if update_entries:
            self._wallet.update_transactionoutput_flags(update_entries)

    def start(self, network) -> None:
        self._network = network
        if network:
            network.add_account(self)

    def stop(self) -> None:
        assert not self._stopped
        self._stopped = True

        self._logger.debug(f'stopping account %s', self)
        if self._network:
            self._network.remove_account(self)
            self._network = None

    def can_export(self) -> bool:
        if self.is_watching_only():
            return False
        keystore = self.get_keystore()
        if keystore is not None:
            return cast(KeyStore, keystore).can_export()
        return False

    def cpfp(self, tx: Transaction, fee: int) -> Optional[Transaction]:
        tx_hash = tx.hash()
        for output_index, tx_output in enumerate(tx.outputs):
            utxo = self.get_utxo(tx_hash, output_index)
            if utxo is not None:
                break
        else:
            return None

        inputs = [utxo.to_tx_input(self)]
        # TODO(rt12) BACKLOG does CPFP need to pay to the parent's output script? If not fix.
        # NOTE: Typing does not work well with attrs and subclasses attributes.
        outputs = [XTxOutput(tx_output.value - fee, utxo.script_pubkey, # type: ignore
            utxo.script_type, self.get_xpubkeys_for_id(utxo.keyinstance_id))] # type: ignore
        locktime = self._wallet.get_local_height()
        # note: no need to call tx.BIP_LI01_sort() here - single input/output
        return Transaction.from_io(inputs, outputs, locktime=locktime)

    def can_sign(self, tx: Transaction) -> bool:
        if tx.is_complete():
            return False
        for k in self.get_keystores():
            if k.can_sign(tx):
                return True
        return False

    def get_xpubkeys_for_id(self, keyinstance_id: int) -> List[XPublicKey]:
        raise NotImplementedError

    def get_master_public_keys(self):
        raise NotImplementedError

    def get_public_keys_for_id(self, keyinstance_id: int) -> List[PublicKey]:
        raise NotImplementedError

    def sign_transaction(self, tx: Transaction, password: str,
            tx_context: Optional[TransactionContext]=None) -> None:
        if self.is_watching_only():
            return

        if tx_context is None:
            tx_context = TransactionContext()

        # This is primarily required by hardware wallets in order for them to sign transactions.
        # But it should be extended to bundle SPV proofs, and other general uses at a later time.
        self.obtain_supporting_data(tx, tx_context)

        # sign
        for k in self.get_keystores():
            try:
                if k.can_sign(tx):
                    k.sign_transaction(tx, password, tx_context)
            except UserCancelled:
                continue

        # Incomplete transactions are multi-signature transactions that have not passed the
        # required signature threshold. We do not store these until they are fully signed.
        if tx.is_complete():
            tx_hash = tx.hash()
            tx_flags = TxFlags.StateSigned
            if tx_context.invoice_id:
                tx_flags |= TxFlags.PaysInvoice

            self._wallet.add_transaction(tx_hash, tx, tx_flags)

            # The transaction has to be in the database before we can refer to it in the invoice.
            if tx_flags & TxFlags.PaysInvoice:
                self.invoices.set_invoice_transaction(cast(int, tx_context.invoice_id), tx_hash)
            if tx_context.description:
                self.set_transaction_label(tx_hash, tx_context.description)

    def obtain_supporting_data(self, tx: Transaction, tx_context: TransactionContext) -> None:
        # Called by the signing logic to ensure all the required data is present.
        # Should be called by the logic that serialises incomplete transactions to gather the
        # context for the next party.
        if self.requires_input_transactions():
            self.obtain_previous_transactions(tx, tx_context)

        # Annotate the outputs to the account's own keys for hardware wallets.
        # - Digitalbitbox makes use of all available output annotations.
        # - Keepkey and Trezor use this to annotate one arbitrary change address.
        # - Ledger kind of ignores it?
        # Hardware wallets cannot send to internal outputs for multi-signature, only have P2SH!
        if any([isinstance(k,Hardware_KeyStore) and k.can_sign(tx) for k in self.get_keystores()]):
            self._add_hardware_derivation_context(tx)

    def obtain_previous_transactions(self, tx: Transaction, tx_context: TransactionContext,
            update_cb: Optional[WaitingUpdateCallback]=None) -> None:
        # Called by the signing logic to ensure all the required data is present.
        # Should be called by the logic that serialises incomplete transactions to gather the
        # context for the next party.
        # Raises PreviousTransactionsMissingException
        need_tx_hashes: Set[bytes] = set()
        for txin in tx.inputs:
            txid = hash_to_hex_str(txin.prev_hash)
            prev_tx: Optional[Transaction] = tx_context.prev_txs.get(txin.prev_hash)
            if prev_tx is None:
                # If the input is a coin we are spending, then it should be in the database.
                # Otherwise we'll try to get it from the network - as long as we are not offline.
                # In the longer term, the other party whose coin is being spent should have
                # provided the source transaction. The only way we should lack it is because of
                # bad wallet management.
                if self.have_transaction(txin.prev_hash):
                    self._logger.debug("fetching input transaction %s from cache", txid)
                    if update_cb is not None:
                        update_cb(False, _("Retrieving local transaction.."))
                    prev_tx = self.get_transaction(txin.prev_hash)
                else:
                    if update_cb is not None:
                        update_cb(False, _("Requesting transaction from external service.."))
                    prev_tx = self._external_transaction_request(txin.prev_hash)
            if prev_tx is None:
                need_tx_hashes.add(txin.prev_hash)
            else:
                tx_context.prev_txs[txin.prev_hash] = prev_tx
                if update_cb is not None:
                    update_cb(True)
        if need_tx_hashes:
            have_tx_hashes = set(tx_context.prev_txs)
            raise PreviousTransactionsMissingException(have_tx_hashes, need_tx_hashes)

    def _external_transaction_request(self, tx_hash: bytes) -> Optional[Transaction]:
        txid = hash_to_hex_str(tx_hash)
        if self._network is None:
            self._logger.debug("unable to fetch input transaction %s from network (offline)", txid)
            return None

        self._logger.debug("fetching input transaction %s from network", txid)
        try:
            tx_hex = self._network.request_and_wait('blockchain.transaction.get', [ txid ])
        except aiorpcx.jsonrpc.RPCError:
            self._logger.exception("failed retrieving transaction")
            return None
        else:
            # TODO(rt12) Once we've moved away from indexer state being authoritative
            # over the contents of a wallet, we should be able to add this to the
            # database as an non-owned input transaction.
            return Transaction.from_hex(tx_hex)

    def _add_hardware_derivation_context(self, tx: Transaction) -> None:
        # add output info for hw wallets
        # the hw keystore at the time of signing does not have access to either the threshold
        # or the larger set of xpubs it's own mpk is included in. So we collect these in the
        # wallet at this point before proceeding to sign.
        info = []
        xpubs = self.get_master_public_keys()
        for tx_output in tx.outputs:
            output_items = {}
            # NOTE(rt12) this will need to exclude all script types hardware wallets dont use
            if tx_output.script_type != ScriptType.MULTISIG_BARE:
                for xpubkey in tx_output.x_pubkeys:
                    candidate_keystores = [ k for k in self.get_keystores()
                        if k.is_signature_candidate(xpubkey) ]
                    if len(candidate_keystores) == 0:
                        continue
                    keyinstance_id = cast(int, self.get_keyinstance_id_for_derivation(
                        xpubkey.derivation_path()))
                    keyinstance = self._keyinstances[keyinstance_id]
                    pubkeys = self.get_public_keys_for_id(keyinstance_id)
                    pubkeys = [pubkey.to_hex() for pubkey in pubkeys]
                    sorted_pubkeys, sorted_xpubs = zip(*sorted(zip(pubkeys, xpubs)))
                    item = (xpubkey.derivation_path(), sorted_xpubs,
                        self.get_threshold(self.get_default_script_type()))
                    output_items[candidate_keystores[0].get_fingerprint()] = item
            info.append(output_items)
        tx.output_info = info

    def estimate_extend_serialised_transaction_steps(self, format: TxSerialisationFormat,
            tx: Transaction, data: Dict[str, Any]) -> int:
        # This should be updated as `extend_serialised_transaction` or any called functions are
        # changed.
        #
        # If there is possibly time consuming work involved in extending the data, we indicate
        # how many units of work are required so that any indication of progress can help the
        # user visualise the progress.
        if format == TxSerialisationFormat.JSON_WITH_PROOFS:
            return len(tx.inputs)
        return 0

    def extend_serialised_transaction(self, format: TxSerialisationFormat, tx: Transaction,
            data: Dict[str, Any], update_cb: Optional[WaitingUpdateCallback]=None) \
            -> Optional[Dict[str, Any]]:
        # `update_cb` if provided is provided after the preceding arguments by
        # `WaitingDialog`/`TxDialog`.
        if format == TxSerialisationFormat.JSON_WITH_PROOFS:
            try:
                self.obtain_previous_transactions(tx, tx.context, update_cb)
            except RuntimeError:
                if update_cb is None:
                    self._logger.exception("unexpected runtime error")
                else:
                    # RuntimeError: wrapped C/C++ object of type WaitingDialog has been deleted
                    self._logger.debug("extend_serialised_transaction interrupted")
                return None
            else:
                data["prev_txs"] = [ ptx.to_hex() for ptx in tx.context.prev_txs.values() ]
        return data

    def get_payment_status(self, req: PaymentRequestRow) -> Tuple[bool, int]:
        local_height = self._wallet.get_local_height()
        with self._utxos_lock:
            related_utxos = [ u for u in self._utxos.values()
                if u.keyinstance_id == req.keyinstance_id ]
        l = []
        for utxo in related_utxos:
            tx_height = self._wallet._transaction_cache.get_height(utxo.tx_hash)
            if tx_height is not None:
                confirmations = local_height - tx_height
            else:
                confirmations = 0
            l.append((confirmations, utxo.value))

        vsum = 0
        vrequired = cast(int, req.value)
        for conf, v in reversed(sorted(l)):
            vsum += v
            if vsum >= vrequired:
                return True, conf
        return False, 0

    def get_fingerprint(self) -> bytes:
        raise NotImplementedError()

    def can_import_privkey(self):
        return False

    def can_import_address(self):
        return False

    def can_delete_key(self):
        return False

    def _add_activated_keys(self, keys: Sequence[KeyInstanceRow]) -> None:
        if not len(keys):
            return

        # self._logger.debug("_add_activated_keys: %s", keys)
        with self._activated_keys_lock:
            self._activated_keys.extend(k.keyinstance_id for k in keys)
        self._activated_keys_event.set()

        # There is no unique id for the account, so we just pass the wallet for now.
        self._wallet.trigger_callback('on_keys_created', self._id, keys)

    async def new_activated_keys(self) -> List[int]:
        await self._activated_keys_event.wait()
        self._activated_keys_event.clear()
        with self._activated_keys_lock:
            result = self._activated_keys
            self._activated_keys = []
        return result

    # TODO(nocheckin) need to remove when we deal with a new deactivated key system
    # def poll_used_key_detection(self, every_n_seconds: int) -> None:
    #     if self.last_poll_time is None or time.time() - self.last_poll_time > every_n_seconds:
    #         self.last_poll_time = time.time()
    #         self.detect_used_keys()

    # TODO(nocheckin) need to remove when we deal with a new deactivated key system
    # def detect_used_keys(self) -> None:
    #     """Note: re-activation of keys is dealt with via:
    #       a) reorg detection time - see self.reactivate_reorged_keys()
    #       b) manual re-activation by the user

    #     Therefore, this function only needs to deal with deactivation"""

    #     if not self._wallet._storage.get('deactivate_used_keys', False):
    #         return

    #     # Get all used keys with zero balance (of the ones that are currently active)
    #     self._logger.debug("detect-used-keys: checking active keys for deactivation criteria")
    #     with TransactionDeltaTable(self._wallet._db_context) as table:
    #         used_keyinstance_ids = table.update_used_keys(self._id)

    #     if len(used_keyinstance_ids) == 0:
    #         return

    #     used_keyinstances = []
    #     with self._deactivated_keys_lock:
    #         for keyinstance_id in used_keyinstance_ids:
    #             self._deactivated_keys.append(keyinstance_id)
    #             key: KeyInstanceRow = self._keyinstances[keyinstance_id]
    #             used_keyinstances.append(key)
    #         self._deactivated_keys_event.set()

    #     self.update_key_activation_state_cache(used_keyinstances, False)
    #     self._logger.debug("deactivated %s used keys", len(used_keyinstance_ids))

    # def update_key_activation_state(self, keyinstances: List[KeyInstanceRow], activate: bool) \
    #         -> None:
    #     db_updates = self.update_key_activation_state_cache(keyinstances, activate)
    #     self._wallet.update_keyinstance_flags(db_updates)

    # def update_key_activation_state_cache(self, keyinstances: List[KeyInstanceRow],
    #         activate: bool) -> List[Tuple[KeyInstanceFlag, int]]:
    #     db_updates = []
    #     for key in keyinstances:
    #         old_flags = KeyInstanceFlag(key.flags)
    #         if activate:
    #             new_flags = old_flags | KeyInstanceFlag.IS_ACTIVE
    #         else:
    #             # if USER_SET_ACTIVE flag is set - this flag will remain
    #             new_flags = old_flags & (KeyInstanceFlag.INACTIVE_MASK |
    #                 KeyInstanceFlag.USER_SET_ACTIVE)
    #         self._keyinstances[key.keyinstance_id] = key._replace(flags=new_flags)
    #         db_updates.append((new_flags, key.keyinstance_id))
    #     return db_updates

    def reactivate_reorged_keys(self, reorged_tx_hashes: List[bytes]) -> None:
        """re-activate all of the reorged keys and allow deactivation to occur via the usual
        mechanisms."""
        with self.lock:
            tx_key_ids: List[Tuple[bytes, Set[int]]] = []
            # TODO(nocheckin) needs to be replaced
            # for tx_hash in reorged_tx_hashes:
            #     tx_key_ids.append((tx_hash, self._sync_state.get_transaction_key_ids(
            #         hash_to_hex_str(tx_hash))))
            self.unarchive_transaction_keys(tx_key_ids)

    async def new_deactivated_keys(self) -> List[int]:
        await self._deactivated_keys_event.wait()
        self._deactivated_keys_event.clear()
        with self._deactivated_keys_lock:
            result = self._deactivated_keys
            self._deactivated_keys = []
        return result

    def sign_message(self, keyinstance_id, message, password: str):
        derivation_path = self._keypath[keyinstance_id]
        keystore = cast(SignableKeystoreTypes, self.get_keystore())
        return keystore.sign_message(derivation_path, message, password)

    def decrypt_message(self, keyinstance_id: int, message, password: str):
        derivation_path = self._keypath[keyinstance_id]
        keystore = cast(SignableKeystoreTypes, self.get_keystore())
        return keystore.decrypt_message(derivation_path, message, password)

    def is_watching_only(self) -> bool:
        raise NotImplementedError

    def can_change_password(self) -> bool:
        raise NotImplementedError

    def can_spend(self) -> bool:
        # All accounts can at least construct unsigned transactions except for imported address
        # accounts.
        return True


class SimpleAccount(AbstractAccount):
    # wallet with a single keystore

    def is_watching_only(self) -> bool:
        return cast(KeyStore, self.get_keystore()).is_watching_only()

    def can_change_password(self) -> bool:
        return cast(KeyStore, self.get_keystore()).can_change_password()


class ImportedAccountBase(SimpleAccount):
    def can_delete_key(self) -> bool:
        return True

    def has_seed(self) -> bool:
        return False

    def get_master_public_keys(self):
        return []

    def get_fingerprint(self) -> bytes:
        return b''


class ImportedAddressAccount(ImportedAccountBase):
    # Watch-only wallet of imported addresses

    def __init__(self, wallet: 'Wallet', row: AccountRow,
            keyinstance_rows: List[KeyInstanceRow],
            output_rows: List[TransactionOutputRow],
            description_rows: List[AccountTransactionDescriptionRow]) -> None:
        self._hashes: Dict[int, str] = {}
        super().__init__(wallet, row, keyinstance_rows, output_rows, description_rows)

    def type(self) -> AccountType:
        return AccountType.IMPORTED_ADDRESS

    def is_watching_only(self) -> bool:
        return True

    def can_spend(self) -> bool:
        return False

    def can_import_privkey(self):
        return False

    def _load_keys(self, keyinstance_rows: List[KeyInstanceRow]) -> None:
        self._hashes.clear()
        for row in keyinstance_rows:
            self._hashes[row.keyinstance_id] = extract_public_key_hash(row)

    def _unload_keys(self, key_ids: Set[int]) -> None:
        for key_id in key_ids:
            del self._hashes[key_id]
        super()._unload_keys(key_ids)

    def can_change_password(self) -> bool:
        return False

    def can_import_address(self) -> bool:
        return True

    def import_address(self, address: Address) -> bool:
        assert isinstance(address, Address)
        address_string = address.to_string()
        if address_string in self._hashes.values():
            return False

        ia_data = { "hash": address_string }
        derivation_data = json.dumps(ia_data).encode()
        raw_keyinstance = KeyInstanceRow(-1, -1,
            None, DerivationType.PUBLIC_KEY_HASH, derivation_data,
            None, KeyInstanceFlag.IS_ACTIVE, None)
        keyinstance = self._wallet.create_keyinstances(self._id, [ raw_keyinstance ])[0]
        self._hashes[keyinstance.keyinstance_id] = address_string
        self._keyinstances[keyinstance.keyinstance_id] = keyinstance
        self._add_activated_keys([ keyinstance ])

        return True

    def get_public_keys_for_id(self, keyinstance_id: int) -> List[PublicKey]:
        return [ ]

    def get_script_template_for_id(self, keyinstance_id: int, script_type: ScriptType) \
            -> ScriptTemplate:
        # TODO(nocheckin) this accepts a `script_type value but does not use it, work out if we
        # need to do any additional handling or validation.
        return Address.from_string(self._hashes[keyinstance_id], Net.COIN)


class ImportedPrivkeyAccount(ImportedAccountBase):
    def __init__(self, wallet: 'Wallet', row: AccountRow,
            keyinstance_rows: List[KeyInstanceRow],
            output_rows: List[TransactionOutputRow],
            description_rows: List[AccountTransactionDescriptionRow]) -> None:
        assert all(row.derivation_type == DerivationType.PRIVATE_KEY for row in keyinstance_rows)
        self._default_keystore = Imported_KeyStore()
        AbstractAccount.__init__(self, wallet, row, keyinstance_rows, output_rows, description_rows)

    def type(self) -> AccountType:
        return AccountType.IMPORTED_PRIVATE_KEY

    def is_watching_only(self) -> bool:
        return False

    def can_import_privkey(self):
        return True

    def _load_keys(self, keyinstance_rows: List[KeyInstanceRow]) -> None:
        cast(Imported_KeyStore, self._default_keystore).load_state(keyinstance_rows)

    def _unload_keys(self, key_ids: Set[int]) -> None:
        for key_id in key_ids:
            cast(Imported_KeyStore, self._default_keystore).remove_key(key_id)
        super()._unload_keys(key_ids)

    def can_change_password(self) -> bool:
        return True

    def can_import_address(self) -> bool:
        return False

    def get_public_keys_for_id(self, keyinstance_id: int) -> List[PublicKey]:
        return [
            cast(Imported_KeyStore, self.get_keystore()).get_public_key_for_id(keyinstance_id) ]

    def import_private_key(self, private_key_text: str, password: str) -> str:
        public_key = PrivateKey.from_text(private_key_text).public_key

        k = cast(Imported_KeyStore, self.get_keystore())
        # Prevent re-importing existing entries.
        if k.get_keyinstance_id_for_public_key(public_key) is not None:
            return private_key_text

        enc_private_key_text = pw_encode(private_key_text, password)
        ik_data = {
            "pub": public_key.to_hex(),
            "prv": enc_private_key_text,
        }
        derivation_data = json.dumps(ik_data).encode()
        raw_keyinstance = KeyInstanceRow(-1, -1, None, DerivationType.PRIVATE_KEY, derivation_data,
            None, KeyInstanceFlag.IS_ACTIVE, None)
        keyinstance = self._wallet.create_keyinstances(self._id, [ raw_keyinstance ])[0]
        self._keyinstances[keyinstance.keyinstance_id] = keyinstance

        k.import_private_key(keyinstance.keyinstance_id, public_key, enc_private_key_text)

        self._add_activated_keys([ keyinstance ])
        return private_key_text

    def export_private_key(self, keyinstance_id: int, password: str) -> str:
        '''Returned in WIF format.'''
        keystore = cast(Imported_KeyStore, self.get_keystore())
        pubkey = keystore.get_public_key_for_id(keyinstance_id)
        return keystore.export_private_key(pubkey, password)

    def get_xpubkeys_for_id(self, keyinstance_id: int) -> List[XPublicKey]:
        keystore = cast(Imported_KeyStore, self.get_keystore())
        public_key = keystore.get_public_key_for_id(keyinstance_id)
        return [XPublicKey(pubkey_bytes=public_key.to_bytes())]

    def get_script_template_for_id(self, keyinstance_id: int, script_type: ScriptType) \
            -> ScriptTemplate:
        public_key = self.get_public_keys_for_id(keyinstance_id)[0]
        return self.get_script_template(public_key, script_type)

class DeterministicAccount(AbstractAccount):
    def __init__(self, wallet: 'Wallet', row: AccountRow,
            keyinstance_rows: List[KeyInstanceRow],
            output_rows: List[TransactionOutputRow],
            description_rows: List[AccountTransactionDescriptionRow]) -> None:
        AbstractAccount.__init__(self, wallet, row, keyinstance_rows, output_rows, description_rows)

    def has_seed(self) -> bool:
        return cast(Deterministic_KeyStore, self.get_keystore()).has_seed()

    def get_seed(self, password: Optional[str]) -> str:
        return cast(Deterministic_KeyStore, self.get_keystore()).get_seed(password)

    def _load_keys(self, keyinstance_rows: List[KeyInstanceRow]) -> None:
        for row in keyinstance_rows:
            derivation_data = json.loads(row.derivation_data)
            assert row.derivation_type == DerivationType.BIP32_SUBPATH
            self._keypath[row.keyinstance_id] = tuple(derivation_data["subpath"])

    def _unload_keys(self, key_ids: Set[int]) -> None:
        for key_id in key_ids:
            if key_id in self._keypath:
                del self._keypath[key_id]
        super()._unload_keys(key_ids)

    def get_next_derivation_index(self, derivation_path: Sequence[int]) -> int:
        with self.lock:
            keystore = cast(DerivablePaths, self.get_keystore())
            return keystore.get_next_index(derivation_path)

    def allocate_keys(self, count: int,
            derivation_path: Sequence[int]) -> Sequence[DeterministicKeyAllocation]:
        if count <= 0:
            return []

        self._logger.info(f'creating {count} new keys within {derivation_path}')
        keystore = cast(Deterministic_KeyStore, self.get_keystore())
        masterkey_id = keystore.get_id()
        path_keystore = cast(DerivablePaths, self.get_keystore())
        with self.lock:
            next_id = path_keystore.allocate_indexes(derivation_path, count)
            self._wallet.update_masterkey_derivation_data(masterkey_id)

        return tuple(DeterministicKeyAllocation(masterkey_id, DerivationType.BIP32_SUBPATH,
            tuple(derivation_path) + (i,)) for i in range(next_id, next_id + count))

    # Returns ordered from use first to use last.
    def get_fresh_keys(self, derivation_parent: Sequence[int], count: int) -> List[KeyInstanceRow]:
        fresh_keys = self.get_existing_fresh_keys(derivation_parent, count)
        if len(fresh_keys) < count:
            required_count = count - len(fresh_keys)
            new_keys = self.create_keys(required_count, derivation_parent)
            # Preserve oldest to newest ordering.
            fresh_keys += new_keys
            assert len(fresh_keys) == count
        return fresh_keys

    # Returns ordered from use first to use last.
    def get_existing_fresh_keys(self, derivation_parent: Sequence[int], limit: int) \
            -> List[KeyInstanceRow]:
        keystore = cast(Deterministic_KeyStore, self.get_keystore())
        masterkey_id = keystore.get_id()
        return db_functions.read_unused_bip32_keys(self._wallet._db_context, self._id,
            masterkey_id, derivation_parent, limit)
        # def _is_fresh_key(keyinstance: KeyInstanceRow) -> bool:
        #     return (keyinstance.script_type == ScriptType.NONE and
        #         (keyinstance.flags & KeyInstanceFlag.ALLOCATED_MASK) == 0)
        # parent_depth = len(derivation_parent)
        # candidates = [ key for key in self._keyinstances.values()
        #     if len(self._keypath[key.keyinstance_id]) == parent_depth+1
        #     and self._keypath[key.keyinstance_id][:parent_depth] == derivation_parent ]
        # # Order keys from newest to oldest and work out how many in front are unused/fresh.
        # keys = sorted(candidates, key=lambda v: -v.keyinstance_id)
        # newest_to_oldest = list(itertools.takewhile(_is_fresh_key, keys))
        # # Provide them in the more usable oldest to newest form.
        # return list(reversed(newest_to_oldest))

    def count_unused_keys(self, derivation_parent: Sequence[int]) -> int:
        keystore = cast(Deterministic_KeyStore, self.get_keystore())
        masterkey_id = keystore.get_id()
        return db_functions.count_unused_bip32_keys(self._wallet._db_context, self._id,
            masterkey_id, derivation_parent)

    async def _synchronize_chain(self, derivation_parent: Sequence[int], wanted: int) -> None:
        path_keystore = cast(DerivablePaths, self.get_keystore())
        existing_count = path_keystore.get_next_index(derivation_parent)
        fresh_count = self.count_unused_keys(derivation_parent)
        self.get_fresh_keys(derivation_parent, wanted)
        self._logger.info(
            f'derivation {derivation_parent} has {existing_count:,d} keys, {fresh_count:,d} fresh')

    async def _synchronize_account(self) -> None:
        '''Class-specific synchronization (generation of missing addresses).'''
        await self._synchronize_chain(RECEIVING_SUBPATH,
            self.get_gap_limit_for_path(RECEIVING_SUBPATH))
        await self._synchronize_chain(CHANGE_SUBPATH,
            self.get_gap_limit_for_path(CHANGE_SUBPATH))

    def get_master_public_keys(self) -> List[str]:
        return [self.get_master_public_key()]

    def get_fingerprint(self) -> bytes:
        keystore = cast(Deterministic_KeyStore, self.get_keystore())
        return keystore.get_fingerprint()


class SimpleDeterministicAccount(SimpleAccount, DeterministicAccount):
    """ Deterministic Wallet with a single pubkey per address """

    def __init__(self, wallet: 'Wallet', row: AccountRow,
            keyinstance_rows: List[KeyInstanceRow],
            output_rows: List[TransactionOutputRow],
            description_rows: List[AccountTransactionDescriptionRow]) -> None:
        DeterministicAccount.__init__(self, wallet, row, keyinstance_rows, output_rows,
            description_rows)

    def get_xpubkeys_for_id(self, keyinstance_id: int) -> List[XPublicKey]:
        keyinstance = self._keyinstances[keyinstance_id]
        derivation_path = self._keypath[keyinstance_id]
        return [self._wallet.get_keystore(keyinstance.masterkey_id).get_xpubkey(derivation_path)]

    def get_master_public_key(self) -> str:
        keystore = cast(StandardKeystoreTypes, self.get_keystore())
        return cast(str, keystore.get_master_public_key())

    def _get_public_key_for_id(self, keyinstance_id: int) -> PublicKey:
        derivation_path = self._keypath[keyinstance_id]
        keyinstance = self._keyinstances[keyinstance_id]
        keystore = self._wallet.get_keystore(keyinstance.masterkey_id)
        return keystore.derive_pubkey(derivation_path)

    def get_public_keys_for_id(self, keyinstance_id: int) -> List[PublicKey]:
        return [ self._get_public_key_for_id(keyinstance_id) ]

    def get_script_template_for_id(self, keyinstance_id: int, script_type: ScriptType) \
            -> ScriptTemplate:
        public_key = self._get_public_key_for_id(keyinstance_id)
        return self.get_script_template(public_key, script_type)

    def derive_pubkeys(self, derivation_path: Sequence[int]) -> PublicKey:
        keystore = cast(Xpub, self.get_keystore())
        return keystore.derive_pubkey(derivation_path)

    def derive_script_template(self, derivation_path: Sequence[int]) -> ScriptTemplate:
        return self.get_script_template(self.derive_pubkeys(derivation_path))



class StandardAccount(SimpleDeterministicAccount):
    def type(self) -> AccountType:
        return AccountType.STANDARD


class MultisigAccount(DeterministicAccount):
    def __init__(self, wallet: 'Wallet', row: AccountRow,
            keyinstance_rows: List[KeyInstanceRow],
            output_rows: List[TransactionOutputRow],
            description_rows: List[AccountTransactionDescriptionRow]) -> None:
        self._multisig_keystore = cast(Multisig_KeyStore,
            wallet.get_keystore(cast(int, row.default_masterkey_id)))
        self.m = self._multisig_keystore.m
        self.n = self._multisig_keystore.n

        DeterministicAccount.__init__(self, wallet, row, keyinstance_rows, output_rows,
            description_rows)

    def type(self) -> AccountType:
        return AccountType.MULTISIG

    def get_threshold(self, script_type: ScriptType) -> int:
        assert script_type in ACCOUNT_SCRIPT_TYPES[AccountType.MULTISIG], \
            f"get_threshold got bad script_type {script_type}"
        return self.m

    def get_public_keys_for_id(self, keyinstance_id: int) -> List[PublicKey]:
        derivation_path = self._keypath[keyinstance_id]
        return [ k.derive_pubkey(derivation_path) for k in self.get_keystores() ]

    def get_possible_scripts_for_id(self, keyinstance_id: int) -> List[Tuple[ScriptType, Script]]:
        public_keys = self.get_public_keys_for_id(keyinstance_id)
        public_keys_hex = [pubkey.to_hex() for pubkey in public_keys]
        return [ (script_type, self.get_script_template(public_keys_hex, script_type).to_script())
            for script_type in ACCOUNT_SCRIPT_TYPES[AccountType.MULTISIG] ]

    def get_script_template_for_id(self, keyinstance_id: int, script_type: ScriptType) \
            -> ScriptTemplate:
        public_keys = self.get_public_keys_for_id(keyinstance_id)
        public_keys_hex = [pubkey.to_hex() for pubkey in public_keys]
        return self.get_script_template(public_keys_hex, script_type)

    def get_dummy_script_template(self, script_type: Optional[ScriptType]=None) -> ScriptTemplate:
        public_keys_hex = []
        for i in range(self.m):
            public_keys_hex.append(PrivateKey(os.urandom(32)).public_key.to_hex())
        return self.get_script_template(public_keys_hex, script_type)

    def get_script_template(self, public_keys_hex: List[str],
            script_type: Optional[ScriptType]=None) -> ScriptTemplate:
        if script_type is None:
            script_type = self.get_default_script_type()
        return get_multi_signer_script_template(public_keys_hex, self.m, script_type)

    def derive_pubkeys(self, derivation_path: Sequence[int]) -> List[PublicKey]:
        return [ k.derive_pubkey(derivation_path) for k in self.get_keystores() ]

    def derive_script_template(self, derivation_path: Sequence[int]) -> ScriptTemplate:
        public_keys_hex = [pubkey.to_hex() for pubkey in self.derive_pubkeys(derivation_path)]
        return self.get_script_template(public_keys_hex)

    def get_keystore(self) -> Multisig_KeyStore:
        return self._multisig_keystore

    def get_keystores(self) -> Sequence[SinglesigKeyStoreTypes]:
        return self._multisig_keystore.get_cosigner_keystores()

    def has_seed(self) -> bool:
        return self.get_keystore().has_seed()

    def can_change_password(self) -> bool:
        return self.get_keystore().can_change_password()

    def is_watching_only(self) -> bool:
        return self._multisig_keystore.is_watching_only()

    def get_master_public_key(self) -> str:
        raise NotImplementedError
        # return cast(str, self.get_keystore().get_master_public_key())

    def get_master_public_keys(self) -> List[str]:
        return [cast(str, k.get_master_public_key()) for k in self.get_keystores()]

    def get_fingerprint(self) -> bytes:
        # Sort the fingerprints in the same order as their master public keys.
        mpks = self.get_master_public_keys()
        fingerprints = [ k.get_fingerprint() for k in self.get_keystores() ]
        sorted_mpks, sorted_fingerprints = zip(*sorted(zip(mpks, fingerprints)))
        return b''.join(sorted_fingerprints)

    def get_xpubkeys_for_id(self, keyinstance_id: int) -> List[XPublicKey]:
        derivation_path = self._keypath[keyinstance_id]
        x_pubkeys = [k.get_xpubkey(derivation_path) for k in self.get_keystores()]
        # Sort them using the order of the realized pubkeys
        sorted_pairs = sorted((x_pubkey.to_public_key().to_hex(), x_pubkey)
            for x_pubkey in x_pubkeys)
        return [x_pubkey for _hex, x_pubkey in sorted_pairs]


class Wallet(TriggeredCallbacks):
    _network: Optional['Network'] = None
    _stopped: bool = False

    def __init__(self, storage: WalletStorage) -> None:
        TriggeredCallbacks.__init__(self)

        self._id = random.randint(0, (1<<32)-1)

        self._storage = storage
        self._logger = logs.get_logger(f"wallet[{self.name()}]")
        self._db_context = storage.get_db_context()
        assert self._db_context is not None

        txdata_cache_size = self.get_cache_size_for_tx_bytedata() * (1024 * 1024)

        self.db_functions_async = AsynchronousFunctions(self._db_context)

        self._transaction_table = TransactionTable(self._db_context)
        self._transaction_cache = TransactionCache(self._db_context, self._transaction_table,
            txdata_cache_size=txdata_cache_size)

        self._masterkey_rows: Dict[int, MasterKeyRow] = {}
        self._account_rows: Dict[int, AccountRow] = {}

        self._accounts: Dict[int, AbstractAccount] = {}
        self._keystores: Dict[int, KeyStore] = {}

        self.load_state()

        self.contacts = Contacts(self._storage)

        self.txs_changed_event = app_state.async_.event()
        self.progress_event = app_state.async_.event()
        self.request_count = 0
        self.response_count = 0

    def __str__(self) -> str:
        return f"wallet(path='{self._storage.get_path()}')"

    def get_db_context(self) -> DatabaseContext:
        assert self._db_context is not None, "This wallet does not have a database context"
        return self._db_context

    def move_to(self, new_path: str) -> None:
        assert self._transaction_table is not None
        self._transaction_table.close()
        self._db_context = None

        self._storage.move_to(new_path)

        self._db_context = cast(DatabaseContext, self._storage.get_db_context())
        self._transaction_table = TransactionTable(self._db_context)
        self._transaction_cache.set_store(self._transaction_table)

    def load_state(self) -> None:
        if self._db_context is None:
            return

        self._last_load_height = self._storage.get('stored_height', 0)
        last_load_hash = self._storage.get('last_tip_hash')
        if last_load_hash is not None:
            last_load_hash = hex_str_to_hash(last_load_hash)
        self._last_load_hash = last_load_hash
        self._logger.debug("chain %d:%s", self._last_load_height,
            hash_to_hex_str(last_load_hash) if last_load_hash is not None else None)

        self._keystores.clear()
        self._accounts.clear()

        with self.get_account_transaction_table() as table:
            all_account_tx_descriptions = { r.account_id: r for r in table.read_descriptions() }

        with MasterKeyTable(self._db_context) as table:
            for row in sorted(table.read(), key=lambda t: 0 if t[1] is None else t[1]):
                self._realize_keystore(row)

        with KeyInstanceTable(self._db_context) as table:
            all_account_keys: Dict[int, List[KeyInstanceRow]] = defaultdict(list)
            keyinstances = {}
            for row in table.read():
                keyinstances[row.keyinstance_id] = row
                all_account_keys[row.account_id].append(row)

        with TransactionOutputTable(self._db_context) as table:
            all_account_outputs: Dict[int, List[TransactionOutputRow]] = defaultdict(list)
            for row in table.read():
                if row.keyinstance_id is None:
                    continue
                keyinstance = keyinstances[row.keyinstance_id]
                all_account_outputs[keyinstance.account_id].append(row)

        with AccountTable(self._db_context) as table:
            for row in table.read():
                account_keys = all_account_keys.get(row.account_id, [])
                account_outputs = all_account_outputs.get(row.account_id, [])
                account_descriptions = all_account_tx_descriptions.get(row.account_id, [])
                if row.default_masterkey_id is not None:
                    account = self._realize_account(row, account_keys, account_outputs,
                        account_descriptions)
                else:
                    found_types = set(key.derivation_type for key in account_keys)
                    prvkey_types = set([ DerivationType.PRIVATE_KEY ])
                    address_types = set([ DerivationType.PUBLIC_KEY_HASH,
                        DerivationType.SCRIPT_HASH ])
                    if found_types & prvkey_types:
                        account = ImportedPrivkeyAccount(self, row, account_keys, account_outputs,
                            account_descriptions)
                    elif found_types & address_types:
                        account = ImportedAddressAccount(self, row, account_keys, account_outputs,
                            account_descriptions)
                    else:
                        raise WalletLoadError(_("Account corrupt, types: %s"), found_types)
                self.register_account(row.account_id, account)

    def register_account(self, account_id: int, account: AbstractAccount) -> None:
        self._accounts[account_id] = account

    def name(self) -> str:
        return get_wallet_name_from_path(self.get_storage_path())

    def get_storage_path(self) -> str:
        return self._storage.get_path()

    def get_storage(self) -> WalletStorage:
        return self._storage

    def get_keystore(self, keystore_id: int) -> KeyStore:
        return self._keystores[keystore_id]

    def get_keystores(self) -> Sequence[KeyStore]:
        return list(self._keystores.values())

    def check_password(self, password: str) -> None:
        self._storage.check_password(password)

    def update_password(self, new_password: str, old_password: Optional[str]=None) -> None:
        assert new_password, "calling code must provide an new password"
        self._storage.put("password-token", pw_encode(os.urandom(32).hex(), new_password))
        for keystore in self._keystores.values():
            if keystore.can_change_password():
                keystore.update_password(new_password, old_password)
                if keystore.has_masterkey():
                    self.update_masterkey_derivation_data(keystore.get_id())
                else:
                    assert isinstance(keystore, Imported_KeyStore)
                    updates = []
                    for key_id, derivation_data in keystore.get_keyinstance_derivation_data():
                        derivation_bytes = json.dumps(derivation_data).encode()
                        updates.append((derivation_bytes, key_id))
                    self.update_keyinstance_derivation_data(updates)

    def get_account(self, account_id: int) -> Optional[AbstractAccount]:
        return self._accounts.get(account_id)

    def get_accounts_for_keystore(self, keystore: KeyStore) -> List[AbstractAccount]:
        accounts = []
        for account in self.get_accounts():
            account_keystore = account.get_keystore()
            if keystore is account_keystore:
                accounts.append(account)
        return accounts

    def get_account_ids(self) -> Set[int]:
        return set(self._accounts)

    def get_accounts(self) -> Sequence[AbstractAccount]:
        return list(self._accounts.values())

    def get_default_account(self) -> Optional[AbstractAccount]:
        if len(self._accounts):
            return list(self._accounts.values())[0]
        return None

    def _realize_keystore(self, row: MasterKeyRow) -> None:
        data: Dict[str, Any] = json.loads(row.derivation_data)
        parent_keystore: Optional[KeyStore] = None
        if row.parent_masterkey_id is not None:
            parent_keystore = self._keystores[row.parent_masterkey_id]
        keystore = instantiate_keystore(row.derivation_type, data, parent_keystore, row)
        self._keystores[row.masterkey_id] = keystore
        self._masterkey_rows[row.masterkey_id] = row

    def _realize_account(self, account_row: AccountRow,
            keyinstance_rows: List[KeyInstanceRow],
            output_rows: List[TransactionOutputRow],
            transaction_descriptions: List[AccountTransactionDescriptionRow]) \
                -> AbstractAccount:
        account_constructors = {
            DerivationType.BIP32: StandardAccount,
            DerivationType.BIP32_SUBPATH: StandardAccount,
            DerivationType.ELECTRUM_OLD: StandardAccount,
            DerivationType.ELECTRUM_MULTISIG: MultisigAccount,
            DerivationType.HARDWARE: StandardAccount,
        }
        if account_row.default_masterkey_id is None:
            if keyinstance_rows[0].derivation_type == DerivationType.PUBLIC_KEY_HASH:
                return ImportedAddressAccount(self, account_row, keyinstance_rows, output_rows,
                    transaction_descriptions)
            elif keyinstance_rows[0].derivation_type == DerivationType.PRIVATE_KEY:
                return ImportedPrivkeyAccount(self, account_row, keyinstance_rows, output_rows,
                    transaction_descriptions)
        else:
            masterkey_row = self._masterkey_rows[account_row.default_masterkey_id]
            klass = account_constructors.get(masterkey_row.derivation_type, None)
            if klass is not None:
                return klass(self, account_row, keyinstance_rows, output_rows,
                    transaction_descriptions)
        raise WalletLoadError(_("unknown account type %d"), masterkey_row.derivation_type)

    def _realize_account_from_row(self, account_row: AccountRow,
            keyinstance_rows: List[KeyInstanceRow], output_rows: List[TransactionOutputRow],
            transaction_descriptions: List[AccountTransactionDescriptionRow]) -> AbstractAccount:
        account = self._realize_account(account_row, keyinstance_rows, output_rows,
            transaction_descriptions)
        self.register_account(account_row.account_id, account)
        self.trigger_callback("on_account_created", account_row.account_id)

        self.create_wallet_events([
            WalletEventRow(0, WalletEventType.SEED_BACKUP_REMINDER, account_row.account_id,
                WalletEventFlag.FEATURED | WalletEventFlag.UNREAD, int(time.time()))
        ])

        if self._network is not None:
            account.start(self._network)
        return account

    def create_account_from_keystore(self, keystore) -> AbstractAccount:
        masterkey_row = self.create_masterkey_from_keystore(keystore)
        if masterkey_row.derivation_type == DerivationType.ELECTRUM_OLD:
            account_name = "Outdated Electrum account"
            script_type = ScriptType.P2PKH
        elif masterkey_row.derivation_type == DerivationType.BIP32:
            account_name = "Standard account"
            script_type = ScriptType.P2PKH
        elif masterkey_row.derivation_type == DerivationType.ELECTRUM_MULTISIG:
            account_name = "Multi-signature account"
            script_type = ScriptType.MULTISIG_BARE
        elif masterkey_row.derivation_type == DerivationType.HARDWARE:
            account_name = keystore.label or "Hardware wallet"
            script_type = ScriptType.P2PKH
        else:
            raise WalletLoadError(f"Unhandled derivation type {masterkey_row.derivation_type}")
        basic_row = AccountRow(-1, masterkey_row.masterkey_id, script_type, account_name)
        rows = self.add_accounts([ basic_row ])
        return self._realize_account_from_row(rows[0], [], [], [])

    def create_masterkey_from_keystore(self, keystore: KeyStore) -> MasterKeyRow:
        basic_row = keystore.to_masterkey_row()
        rows = self.add_masterkeys([ basic_row ])
        keystore.set_row(rows[0])
        self._keystores[rows[0].masterkey_id] = keystore
        self._masterkey_rows[rows[0].masterkey_id] = rows[0]
        return rows[0]

    def create_account_from_text_entries(self, text_type: KeystoreTextType, script_type: ScriptType,
            entries: List[str], password: str) -> AbstractAccount:
        account_name: Optional[str] = None
        if text_type == KeystoreTextType.ADDRESSES:
            account_name = "Imported addresses"
        elif text_type == KeystoreTextType.PRIVATE_KEYS:
            account_name = "Imported private keys"
        else:
            raise WalletLoadError(f"Unhandled text type {text_type}")

        raw_keyinstance_rows = []
        if text_type == KeystoreTextType.ADDRESSES:
            for address_string in entries:
                ia_data = { "hash": address_string }
                derivation_data = json.dumps(ia_data).encode()
                raw_keyinstance_rows.append(KeyInstanceRow(-1, -1,
                    None, DerivationType.PUBLIC_KEY_HASH, derivation_data,
                    None, KeyInstanceFlag.IS_ACTIVE, None))
        elif text_type == KeystoreTextType.PRIVATE_KEYS:
            for private_key_text in entries:
                private_key = PrivateKey.from_text(private_key_text)
                pubkey_hex = private_key.public_key.to_hex()
                ik_data = {
                    "pub": pubkey_hex,
                    "prv": pw_encode(private_key_text, password),
                }
                derivation_data = json.dumps(ik_data).encode()
                raw_keyinstance_rows.append(KeyInstanceRow(-1, -1,
                    None, DerivationType.PRIVATE_KEY, derivation_data,
                    None, KeyInstanceFlag.IS_ACTIVE, None))
        basic_account_row = AccountRow(-1, None, script_type, account_name)
        account_row = self.add_accounts([ basic_account_row ])[0]
        keyinstance_rows = self.create_keyinstances(account_row.account_id, raw_keyinstance_rows)
        return self._realize_account_from_row(account_row, keyinstance_rows, [], [])

    def add_masterkeys(self, entries: Sequence[MasterKeyRow]) -> Sequence[MasterKeyRow]:
        masterkey_id = self._storage.get("next_masterkey_id", 1)
        rows = []
        for entry in entries:
            row = MasterKeyRow(masterkey_id, entry.parent_masterkey_id, entry.derivation_type,
                entry.derivation_data)
            rows.append(row)
            self._masterkey_rows[masterkey_id] = row
            masterkey_id += 1
        self._storage.put("next_masterkey_id", masterkey_id)
        with SynchronousWriter() as writer:
            db_functions.create_master_keys(self.get_db_context(), rows,
                completion_callback=writer.get_callback())
            assert writer.succeeded()
        return rows

    def add_accounts(self, accounts: List[AccountRow]) -> List[AccountRow]:
        account_id = self._storage.get("next_account_id", 1)
        rows = []
        for account in accounts:
            rows.append(account._replace(account_id=account_id))
            account_id += 1

        self._storage.put("next_account_id", account_id)

        # Block waiting for the write to succeed here.
        with AccountTable(self.get_db_context()) as table:
            with SynchronousWriter() as writer:
                table.create(rows, completion_callback=writer.get_callback())
                assert writer.succeeded()
        return rows

    def create_keyinstances(self, account_id: int,
            keyinstances: List[KeyInstanceRow]) -> List[KeyInstanceRow]:
        keyinstance_id = self._storage.get("next_keyinstance_id", 1)

        rows = []
        for key in keyinstances:
            rows.append(key._replace(keyinstance_id=keyinstance_id, account_id=account_id))
            keyinstance_id += 1
        self._storage.put("next_keyinstance_id", keyinstance_id)

        with SynchronousWriter() as writer:
            db_functions.create_keys(self.get_db_context(), rows,
                completion_callback=writer.get_callback())
            assert writer.succeeded()

        return rows

    def create_transactionoutputs(self, account_id: int,
            entries: List[TransactionOutputRow]) -> List[TransactionOutputRow]:
        db_functions.create_transaction_outputs(self.get_db_context(), entries)
        return entries

    def get_account_transaction_table(self) -> AccountTransactionTable:
        return AccountTransactionTable(self.get_db_context())

    def get_invoice_table(self) -> InvoiceTable:
        return InvoiceTable(self.get_db_context())

    def get_payment_request_table(self) -> PaymentRequestTable:
        return PaymentRequestTable(self.get_db_context())

    def get_transactionoutput_table(self) -> TransactionOutputTable:
        return TransactionOutputTable(self.get_db_context())

    def create_payment_requests(self, requests: List[PaymentRequestRow],
            completion_callback: Optional[CompletionCallbackType]=None) -> List[PaymentRequestRow]:
        request_id = self._storage.get("next_paymentrequest_id", 1)
        rows = []
        for request in requests:
            rows.append(request._replace(paymentrequest_id=request_id))
            request_id += 1
        self._storage.put("next_paymentrequest_id", request_id)
        with PaymentRequestTable(self.get_db_context()) as table:
            table.create(rows, completion_callback=completion_callback)
        return rows

    def update_account_script_types(self, entries: Sequence[Tuple[ScriptType, int]]) -> None:
        with AccountTable(self.get_db_context()) as table:
            table.update_script_type(entries)

    def update_masterkey_derivation_data(self, masterkey_id: int) -> None:
        keystore = self.get_keystore(masterkey_id)
        derivation_data = json.dumps(keystore.to_derivation_data()).encode()
        with MasterKeyTable(self.get_db_context()) as table:
            table.update_derivation_data([ (derivation_data, masterkey_id) ])

    def read_keyinstances(self, mask: Optional[KeyInstanceFlag]=None,
            key_ids: Optional[List[int]]=None) -> List[KeyInstanceRow]:
        with KeyInstanceTable(self.get_db_context()) as table:
            return table.read(mask, key_ids)

    def update_keyinstance_derivation_data(self, entries: Sequence[Tuple[bytes, int]]) -> None:
        with KeyInstanceTable(self.get_db_context()) as table:
            table.update_derivation_data(entries)

    def update_keyinstance_descriptions(self,
            entries: Sequence[Tuple[Optional[str], int]]) -> None:
        with KeyInstanceTable(self.get_db_context()) as table:
            table.update_descriptions(entries)

    def update_keyinstance_flags(self, entries: Iterable[Tuple[KeyInstanceFlag, int]]) -> None:
        with KeyInstanceTable(self.get_db_context()) as table:
            table.update_flags(entries)

    def read_transaction_metadatas(self, flags: Optional[int]=None, mask: Optional[int]=None,
            tx_hashes: Optional[Sequence[bytes]]=None, account_id: Optional[int]=None) \
                -> List[Tuple[str, TxData]]:
        with TransactionTable(self.get_db_context()) as table:
            return table.read_metadata(flags, mask, tx_hashes, account_id)

    def read_transactionoutputs(self, mask: Optional[TransactionOutputFlag]=None,
            key_ids: Optional[List[int]]=None) -> List[TransactionOutputRow]:
        with TransactionOutputTable(self.get_db_context()) as table:
            return table.read(mask, key_ids)

    def update_transactionoutput_flags(self,
            entries: Iterable[Tuple[TransactionOutputFlag, bytes, int]]) -> None:
        with TransactionOutputTable(self.get_db_context()) as table:
            table.update_flags(entries)

    def get_transaction_deltas(self, tx_hash: bytes, account_id: Optional[int]=None) \
            -> List[TransactionDeltaSumRow]:
        return db_functions.read_transaction_value(self._db_context, tx_hash, account_id)

    def read_wallet_events(self, mask: WalletEventFlag=WalletEventFlag.NONE) \
            -> List[WalletEventRow]:
        with WalletEventTable(self.get_db_context()) as table:
            return table.read(mask=mask)

    def create_wallet_events(self,  entries: List[WalletEventRow]) -> List[WalletEventRow]:
        next_id = self._storage.get("next_wallet_event_id", 1)
        rows = []
        for entry in entries:
            rows.append(entry._replace(event_id=next_id))
            next_id += 1
        with WalletEventTable(self.get_db_context()) as table:
            table.create(rows)
        self._storage.put("next_wallet_event_id", next_id)
        for row in rows:
            app_state.app.on_new_wallet_event(self.get_storage_path(), row)
        return rows

    def update_wallet_event_flags(self,
            entries: Iterable[Tuple[WalletEventFlag, int]]) -> None:
        with WalletEventTable(self.get_db_context()) as table:
            table.update_flags(entries)

    def is_synchronized(self) -> bool:
        "If all the accounts are synchronized"
        return all(w.is_synchronized() for w in self.get_accounts())

    def get_transaction_cache(self) -> TransactionCache:
        return self._transaction_cache

    def get_tx_height(self, tx_hash: bytes) -> Tuple[int, int, Union[int, bool]]:
        """ return the height and timestamp of a verified transaction. """
        metadata = self._transaction_cache.get_metadata(tx_hash)
        assert metadata is not None, f"tx {hash_to_hex_str(tx_hash)} is unknown"
        assert metadata.height is not None, f"tx {hash_to_hex_str(tx_hash)} has no height"
        timestamp = None
        if metadata.height > 0:
            chain = app_state.headers.longest_chain()
            try:
                header = app_state.headers.header_at_height(chain, metadata.height)
                timestamp = header.timestamp
            except MissingHeader:
                pass
        if timestamp is not None:
            conf = max(self.get_local_height() - metadata.height + 1, 0)
            return metadata.height, conf, timestamp
        else:
            return metadata.height, 0, False

    def missing_transactions(self) -> List[bytes]:
        '''Returns a set of tx_hashes.'''
        raise NotImplementedError()

    def unverified_transactions(self) -> Dict[bytes, int]:
        '''Returns a map of tx_hash to tx_height.'''
        results = self._transaction_cache.get_unverified_entries(self.get_local_height())
        self._logger.debug("unverified_transactions: %s", [hash_to_hex_str(r[0]) for r in results])
        return { t[0]: cast(int, t[1].metadata.height) for t in results }

    async def import_transaction(self, tx_hash: bytes, tx: Transaction, flags: TxFlags,
            block_height: Optional[int]=None, block_position: Optional[int]=None,
            fee_hint: Optional[int]=None, external: bool=False) -> None:
        """
        This is a raw transaction we are adding to the database. We have no context on whether
        it is related to the wallet or any of it's accounts, and will process it as it gets
        added and attempt to integrate it where applicable.
        """
        assert tx.is_complete()
        timestamp = int(time.time())

        # The database layer should be decoupled from core wallet logic so we need to
        # break down the transaction and related data for it to consume.
        tx_row = AddTransactionRow(tx_hash, tx.version, tx.locktime, bytes(tx), flags,
            block_height, block_position, None, fee_hint, timestamp, timestamp)

        txi_rows: List[AddTransactionInputRow] = []
        for txi_index, input in enumerate(tx.inputs):
            txi_row = AddTransactionInputRow(tx_hash, txi_index,
                input.prev_hash, input.prev_idx, input.sequence,
                TransactionInputFlag.NONE,      # TODO(nocheckin) work out if different
                input.script_offset, input.script_length,
                timestamp, timestamp)
            txi_rows.append(txi_row)

        txo_rows: List[AddTransactionOutputRow] = []
        for txo_index, txo in enumerate(tx.outputs):
            txo_row = AddTransactionOutputRow(tx_hash, txo_index, txo.value,
                None,                           # Raw transaction means no idea of key usage.
                ScriptType.NONE,                # Raw transaction means no idea of script type.
                TransactionOutputFlag.NONE,     # TODO(nocheckin) work out if different
                scripthash_bytes(txo.script_pubkey),
                txo.script_offset, txo.script_length,
                timestamp, timestamp)
            txo_rows.append(txo_row)

        # TODO(nocheckin) work out if there should be a return value and what we do with it.
        await self.db_functions_async.import_transaction(tx_row, txi_rows, txo_rows)
        # . . .

    # Also called by network.
    def add_transaction(self, tx_hash: bytes, tx: Transaction, flags: TxFlags,
            external: bool=False) -> None:
        tx_id = hash_to_hex_str(tx_hash)
        if self._stopped:
            self._logger.debug("add_transaction on stopped wallet: %s", tx_id)
            return

        involved_account_ids: Set[int] = set()
        attempts_left = 2
        checklist_lock = threading.Lock()

        def attempt_callback() -> None:
            nonlocal attempts_left, tx_hash, tx, involved_account_ids
            with checklist_lock:
                attempts_left -= 1
                is_add_complete = attempts_left == 0

            if is_add_complete:
                self._logger.debug("wallet.add_transaction: %s = %s", tx_id, involved_account_ids)
                self.trigger_callback('transaction_added', tx_hash, tx, involved_account_ids,
                    external)

        def _completion_callback(exc_value: Any) -> None:
            if exc_value is not None:
                raise exc_value # pylint: disable=raising-bad-type

            attempt_callback()

        self._logger.debug("adding tx data %s (flags: %r)", tx_id, flags)
        self._transaction_cache.add_transaction(tx_hash, tx, flags, _completion_callback)

        # TODO(nocheckin) need to replace this
        # for account in self._accounts.values():
        #     if account.process_key_usage(tx_hash, tx, None):
        #         involved_account_ids.add(account.get_id())

        attempt_callback()

    # Called by network.
    def add_transaction_proof(self, tx_hash: bytes, height: int, timestamp: int, position: int,
            proof_position: int, proof_branch: Sequence[bytes]) -> None:
        tx_id = hash_to_hex_str(tx_hash)
        if self._stopped:
            self._logger.debug("add_transaction_proof on stopped wallet: %s", tx_id)
            return
        entry = self._transaction_cache.get_entry(tx_hash, TxFlags.StateCleared) # HasHeight

        # Ensure we are not verifying transactions multiple times.
        if entry is None:
            # We have proof now so regardless what TxState is, we can 'upgrade' it to StateSettled.
            entry = self._transaction_cache.get_entry(tx_hash, TxFlags.STATE_UNCLEARED_MASK)
            assert entry is not None, f"expected uncleared tx {hash_to_hex_str(tx_hash)}"
            self._logger.debug("Fast_tracking entry to StateSettled: %r", entry)

        # We only update a subset.
        flags = TxFlags.HasHeight | TxFlags.HasPosition
        data = TxData(height=height, position=position)
        self._transaction_cache.update(
            [ (tx_hash, data, None, flags | TxFlags.StateSettled) ])

        proof = TxProof(proof_position, proof_branch)
        self._transaction_cache.update_proof(tx_hash, proof)

        height, conf, _timestamp = self.get_tx_height(tx_hash)
        self._logger.debug("add_transaction_proof %d %d %d", height, conf, timestamp)
        self.trigger_callback('verified', tx_hash, height, conf, timestamp)

    def synchronize_incomplete_transaction(self, tx: Transaction) -> None:
        if tx.is_complete():
            return

        self._logger.debug("synchronize_incomplete_transaction complete")

        for txin in tx.inputs:
            for xpubkey in txin.unused_x_pubkeys():
                result = self.resolve_xpubkey(xpubkey)
                if result is None:
                    continue
                account, keyinstance_id = result
                if keyinstance_id is None:
                    account.create_keys_until(xpubkey.derivation_path())

        for txout in tx.outputs:
            if not len(txout.x_pubkeys):
                continue
            for xpubkey in txout.x_pubkeys:
                result = self.resolve_xpubkey(xpubkey)
                if result is None:
                    continue
                account, keyinstance_id = result
                if keyinstance_id is None:
                    account.create_keys_until(xpubkey.derivation_path())

    def undo_verifications(self, above_height: int) -> None:
        '''Called by network when a reorg has happened'''
        if self._stopped:
            self._logger.debug("undo_verifications on stopped wallet: %d", above_height)
            return

        reorg_count, updated_tx_hashes = self._transaction_cache.apply_reorg(above_height)
        self._logger.info(
            f'removing verification of {reorg_count} transactions above {above_height}')

        if self._storage.get('deactivate_used_keys', False):
            for account in self._accounts.values():
                account.reactivate_reorged_keys(updated_tx_hashes)

    def resolve_xpubkey(self,
            x_pubkey: XPublicKey) -> Optional[Tuple[AbstractAccount, Optional[int]]]:
        for account in self._accounts.values():
            for keystore in account.get_keystores():
                if keystore.is_signature_candidate(x_pubkey):
                    if x_pubkey.kind() == XPublicKeyType.PRIVATE_KEY:
                        assert isinstance(keystore, Imported_KeyStore)
                        keyinstance_id = keystore.get_keyinstance_id_for_public_key(
                            x_pubkey.to_public_key())
                    else:
                        keyinstance_id = account.get_keyinstance_id_for_derivation(
                            x_pubkey.derivation_path())
                    return account, keyinstance_id
        return None

    # def set_deactivate_used_keys(self, enabled: bool) -> None:
    #     current_setting = self._storage.get('deactivate_used_keys', None)
    #     if not enabled and current_setting is True:
    #         # ensure all keys are re-activated
    #         for account in self.get_accounts():
    #             account.update_key_activation_state(list(account._keyinstances.values()), True)

    #     return self._storage.put('deactivate_used_keys', enabled)

    def get_boolean_setting(self, setting_name: str, default_value: bool=False) -> bool:
        return self._storage.get(str(setting_name), default_value)

    def set_boolean_setting(self, setting_name: str, enabled: bool) -> None:
        self._storage.put(setting_name, enabled)
        self.trigger_callback('on_setting_changed', setting_name, enabled)

    def get_cache_size_for_tx_bytedata(self) -> int:
        """
        This returns the number of megabytes of cache. The caller should convert it to bytes for
        the cache.
        """
        return self._storage.get('tx_bytedata_cache_size', DEFAULT_TXDATA_CACHE_SIZE_MB)

    def set_cache_size_for_tx_bytedata(self, maximum_size: int, force_resize: bool=False) -> None:
        assert MINIMUM_TXDATA_CACHE_SIZE_MB <= maximum_size <= MAXIMUM_TXDATA_CACHE_SIZE_MB, \
            f"invalid cache size {maximum_size}"
        self._storage.put('tx_bytedata_cache_size', maximum_size)
        maximum_size_bytes = maximum_size * (1024 * 1024)
        self._transaction_cache.set_maximum_cache_size_for_bytedata(maximum_size_bytes,
            force_resize)

    def get_local_height(self) -> int:
        """ return last known height if we are offline """
        return (self._network.get_local_height() if self._network else
            self._storage.get('stored_height', 0))

    def get_request_response_counts(self) -> Tuple[int, int]:
        request_count = self.request_count
        response_count = self.response_count
        for account in self.get_accounts():
            if account.request_count > account.response_count:
                request_count += account.request_count
                response_count += account.response_count
            else:
                account.request_count = 0
                account.response_count = 0
        return request_count, response_count

    def start(self, network: Optional['Network']) -> None:
        self._network = network
        if network is not None:
            network.add_wallet(self)
        for account in self.get_accounts():
            account.start(network)
        self._stopped = False

    def stop(self) -> None:
        assert not self._stopped
        local_height = self._last_load_height
        chain_tip_hash = self._last_load_hash
        if self._network is not None and self._network.chain():
            chain_tip = self._network.chain().tip
            local_height = chain_tip.height
            chain_tip_hash = chain_tip.hash
        self._storage.put('stored_height', local_height)
        self._storage.put('last_tip_hash', chain_tip_hash.hex() if chain_tip_hash else None)

        for account in self.get_accounts():
            account.stop()
        if self._network is not None:
            self._network.remove_wallet(self)
        if self._transaction_table is not None:
            self._transaction_table.close()
        self.db_functions_async.close()
        self._storage.close()
        self._network = None
        self._stopped = True

    def create_gui_handler(self, window: 'ElectrumWindow', account: AbstractAccount) -> None:
        for keystore in account.get_keystores():
            if isinstance(keystore, Hardware_KeyStore):
                plugin = cast('QtPluginBase', keystore.plugin)
                plugin.replace_gui_handler(window, keystore)
