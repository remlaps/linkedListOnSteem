"""
linkedListOnSteem
=================
A library for implementing a doubly-linked list data structure using
custom_json transactions on the Steem blockchain.

Each node in the list is a custom_json transaction whose JSON payload has
the following envelope structure:

{
    "ll_id":    "<list identifier>",       # groups nodes belonging to the same list
    "seq":      <int>,                     # 0-based sequence index within this list
    "timestamp": "<str>",                  # ISO-8601 time when transaction was initiated
    "head": {                              # pointer to the FIRST node of the list
        "block": <int>,                    # 0 if null
        "trx_id": "<str>",                 # empty string if null
        "trx_num": <int | null>            # transaction index within the block
    },
    "prev": {                              # pointer to the PREVIOUS node (null for head)
        "block": <int>,
        "trx_id": "<str>",
        "trx_num": <int | null>
    },
    "payload": { ... }                     # your arbitrary application data
}

Because Steem transactions are immutable once confirmed, "next" pointers
cannot be back-patched.  This library therefore maintains a lightweight
off-chain index (a Python dict) that is rebuilt on demand by walking
``prev`` pointers backwards from the tail — no broad blockchain scan needed.

Index rebuild strategy
----------------------
1. Query the account's transaction history (an indexed API call) to locate
   the most recent custom_json op for this ``ll_id``.  That is the tail.
2. Follow each node's ``prev`` pointer (block_num + trx_id) backwards,
   fetching one block at a time, until a node with a null ``prev`` is
   reached (the head).
3. Reverse the collected nodes to restore forward order and assign seq numbers.

This is O(n) targeted fetches — typically completing in seconds regardless
of how long ago the list was created.  Use ``rebuild_index()``
to resync, or ``import_index()`` to restore from a local cache.

Dependencies
------------
    pip install steem
"""

from __future__ import annotations

__version__ = "0.0.8-alpha"

import datetime
import json
import logging
import random
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Iterator, List, Optional, Tuple

from steem import Steem
from steem.account import Account

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class NodePointer:
    """A (block_num, trx_id) reference to a Steem transaction."""
    block: int = 0
    trx_id: str = ""
    trx_num: Optional[int] = None

    def is_null(self) -> bool:
        return not self.block or not self.trx_id

    def to_dict(self) -> Dict[str, Any]:
        return {"block": self.block, "trx_id": self.trx_id, "trx_num": self.trx_num}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "NodePointer":
        b = d.get("block")
        t = d.get("trx_id")
        n = d.get("trx_num")
        return cls(block=b if b else 0, trx_id=t if t else "", trx_num=n)

    @classmethod
    def null(cls) -> "NodePointer":
        return cls(block=0, trx_id="", trx_num=None)


@dataclass
class ListNode:
    """In-memory representation of a single linked-list node."""
    ll_id: str                          # list identifier
    seq: int                            # 0-based position
    head: NodePointer                   # pointer to list head
    prev: NodePointer                   # pointer to previous node
    next: NodePointer                   # pointer to next node (may be stale on-chain)
    payload: Dict[str, Any]             # application data
    timestamp: Optional[str] = None     # time transaction was initiated
    # Populated after the transaction is confirmed:
    block_num: Optional[int] = None
    trx_id: Optional[str] = None
    trx_num: Optional[int] = None
    author: Optional[str] = None        # Steem account that authored the node

    # ------------------------------------------------------------------ #
    @property
    def pointer(self) -> NodePointer:
        """Return a NodePointer to this node (requires confirmed tx)."""
        return NodePointer(block=self.block_num or 0, trx_id=self.trx_id or "", trx_num=self.trx_num)

    def to_json_payload(self) -> Dict[str, Any]:
        payload_dict = {
            "ll_id":   self.ll_id,
            "seq":     self.seq,
            "head":    self.head.to_dict(),
            "prev":    self.prev.to_dict(),
            "payload": self.payload,
        }
        if self.timestamp:
            payload_dict["timestamp"] = self.timestamp
        return payload_dict

    @classmethod
    def from_json_payload(
        cls,
        data: Dict[str, Any],
        block_num: Optional[int] = None,
        trx_id: Optional[str] = None,
        trx_num: Optional[int] = None,
        author: Optional[str] = None,
    ) -> "ListNode":
        return cls(
            ll_id=data["ll_id"],
            seq=data["seq"],
            head=NodePointer.from_dict(data["head"]),
            prev=NodePointer.from_dict(data["prev"]),
            next=NodePointer.from_dict(data.get("next", {})),
            payload=data.get("payload", {}),
            timestamp=data.get("timestamp"),
            block_num=block_num,
            trx_id=trx_id,
            trx_num=trx_num,
            author=author,
        )


# ---------------------------------------------------------------------------
# Main library class
# ---------------------------------------------------------------------------

class SteemLinkedList:
    """
    Manage a linked list stored as custom_json transactions on Steem.

    Parameters
    ----------
    steem_instance : Steem
        A connected steem.Steem instance (may carry active/posting keys).
    account : str
        The Steem account name that will broadcast transactions.
    ll_id : str
        A unique string identifier for this particular linked list.
        Multiple lists can coexist by using different ll_id values.
    custom_json_id : str
        The ``id`` field used in the custom_json operation (max 32 chars).
    use_active_key : bool
        If True, broadcast with the active key; otherwise posting key.
    wait_for_irreversible : bool
        If True, wait for the transaction to be included in an irreversible
        block before returning (~45-60s). Default is `True`.
    enforce_single_author : bool
        If `True` (default), the index will only accept nodes created by the
        same account that created the head node.
    """

    REQUIRED_AUTHS_KEY = "required_auths"
    REQUIRED_POSTING_KEY = "required_posting_auths"

    def __init__(
        self,
        steem_instance: Steem,
        account: str,
        ll_id: str,
        custom_json_id: str = "steem_ll",
        use_active_key: bool = False,
        wait_for_irreversible: bool = True,
        enforce_single_author: bool = True,
    ) -> None:
        self.steem = steem_instance
        self.account = account
        self.ll_id = ll_id
        self.custom_json_id = custom_json_id[:32]   # Steem enforces 32-char limit
        self.use_active_key = use_active_key
        self.wait_for_irreversible = wait_for_irreversible
        self.enforce_single_author = enforce_single_author

        # Off-chain index: seq -> ListNode (populated by rebuild_index)
        self._index: Dict[int, ListNode] = {}
        self._head: Optional[ListNode] = None
        self._tail: Optional[ListNode] = None
        self._author: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _broadcast(self, node: ListNode) -> Dict[str, Any]:
        """Broadcast a custom_json op and return the response dict."""
        json_str = json.dumps(node.to_json_payload(), separators=(",", ":"))

        if self.use_active_key:
            required_auths = [self.account]
            required_posting_auths = []
        else:
            required_auths = []
            required_posting_auths = [self.account]

        try:
            props = self.steem.get_dynamic_global_properties()
            head_block = props.get("head_block_number", 0)
        except Exception:
            head_block = 0

        result = self.steem.commit.custom_json(
            id=self.custom_json_id,
            json=json_str,
            required_auths=required_auths,
            required_posting_auths=required_posting_auths,
        )

        if not isinstance(result, dict):
            result = {}

        # Wait for the transaction to be included in a block to guarantee
        # one node per block and retrieve the exact block_num and trx_id.
        if head_block > 0:
            logger.debug("Waiting for transaction to be included in a block...")
            found = False
            current_check_block = head_block

            while current_check_block <= head_block + 10:
                try:
                    props = self.steem.get_dynamic_global_properties()
                    latest_block = props.get("head_block_number", 0)

                    while current_check_block <= latest_block:
                        block_data = self.steem.get_block(current_check_block)
                        if block_data:
                            for tx_idx, tx in enumerate(block_data.get("transactions", [])):
                                for op_wrapper in tx.get("operations", []):
                                    op_type, op_value = self._extract_op(op_wrapper)
                                    if op_type == "custom_json" and op_value.get("id") == self.custom_json_id:
                                        # Verify this is the exact operation we just broadcasted
                                        if op_value.get("json") == json_str:
                                            result["block_num"] = current_check_block
                                            tx_id = tx.get("transaction_id")
                                            if not tx_id and "transaction_ids" in block_data:
                                                tx_ids = block_data["transaction_ids"]
                                                if tx_idx < len(tx_ids):
                                                    tx_id = tx_ids[tx_idx]
                                            result["trx_id"] = tx_id or ""
                                            result["trx_num"] = tx_idx
                                            found = True
                                            
                                            if self.wait_for_irreversible:
                                                logger.debug("Tx found in block %s. Waiting for irreversibility (~45s)...", current_check_block)
                                                while True:
                                                    try:
                                                        props_now = self.steem.get_dynamic_global_properties()
                                                        lib = props_now.get("last_irreversible_block_num", 0)
                                                        if lib >= current_check_block:
                                                            break
                                                    except Exception:
                                                        pass
                                                    time.sleep(3)
                                            break
                                if found:
                                    break
                        if found:
                            break
                        current_check_block += 1

                    if found:
                        break
                except Exception:
                    pass
                time.sleep(3)

            if not found:
                logger.warning("Transaction not found within 10 blocks.")
                if "block_num" not in result:
                    result["block_num"] = head_block + 1
        elif "block_num" not in result:
            result["block_num"] = 0

        return result

    @staticmethod
    def _extract_op(op_obj: Any) -> Tuple[str, Dict[str, Any]]:
        """Extract (op_type, op_value) safely from different Steem API node formats."""
        if isinstance(op_obj, dict):
            # AppBase nodes format operations as objects
            op_type = op_obj.get("type", "").replace("_operation", "")
            op_value = op_obj.get("value", {})
            return op_type, op_value
        elif isinstance(op_obj, (list, tuple)) and len(op_obj) >= 2:
            # Legacy nodes format operations as [type, value]
            return op_obj[0], op_obj[1]
        return "", {}

    @staticmethod
    def _parse_custom_json_op(op_value: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return the parsed inner JSON dict from a custom_json op, or None."""
        try:
            data = json.loads(op_value.get("json", "{}"))
            if isinstance(data, dict) and "ll_id" in data:
                return data
        except (json.JSONDecodeError, TypeError):
            pass
        return None

    def _node_from_op(
        self,
        op_value: Dict[str, Any],
        block_num: int,
        trx_id: str,
        trx_num: Optional[int] = None,
    ) -> Optional[ListNode]:
        data = self._parse_custom_json_op(op_value)
        if data and data.get("ll_id") == self.ll_id:
            author = None
            auths = op_value.get(self.REQUIRED_POSTING_KEY, []) or op_value.get(self.REQUIRED_AUTHS_KEY, [])
            if auths:
                author = auths[0]
            return ListNode.from_json_payload(
                data, block_num=block_num, trx_id=trx_id, trx_num=trx_num, author=author)
        return None

    # ------------------------------------------------------------------ #
    # Index management
    # ------------------------------------------------------------------ #

    def _find_tail_pointer(self, after_block: Optional[int] = None) -> Optional[NodePointer]:
        """
        Search the account's transaction history for the most recent
        custom_json op whose inner JSON belongs to this list (matching
        ``ll_id`` and ``custom_json_id``).

        Parameters
        ----------
        after_block : int or None
            If given, only consider transactions from blocks strictly after
            this number.  Pass your cached tail's block_num to find only
            newer nodes since the last sync.

        Returns
        -------
        NodePointer or None
            Pointer to the most recent matching transaction, or None if the
            account has no matching history.
        """
        # get_account_history returns (index, op) pairs in ascending order.
        # We page backwards through history (-1 = latest, batch of 100) until
        # we find the first match (which is the most recent, since we reverse).

        batch_size = 100
        start = -1          # steem-python sentinel: begin from latest op

        while True:
            # Steem RPC requires limit <= start when start != -1
            current_limit = batch_size if start == -1 else min(batch_size, start)

            try:
                # Use raw RPC to fetch exactly one batch, avoiding Account generator's full history scan
                history = self.steem.get_account_history(self.account, start, current_limit)
            except Exception as exc:
                logger.warning("get_account_history failed: %s", exc)
                return None

            if not history:
                break

            # Walk this batch newest-first
            for item in reversed(history):
                if isinstance(item, dict):
                    op = item
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    op = item[1]
                else:
                    continue

                op_type, op_value = self._extract_op(op.get("op"))
                blk      = op.get("block")

                if after_block is not None and blk is not None and blk <= after_block:
                    # We've gone back past our cached tail — nothing newer exists.
                    return None

                if op_type != "custom_json":
                    continue
                if op_value.get("id") != self.custom_json_id:
                    continue

                data = self._parse_custom_json_op(op_value)
                if data and data.get("ll_id") == self.ll_id:
                    trx_id = op.get("trx_id") or op.get("transaction_id", "")
                    trx_num = op.get("trx_in_block")
                    logger.debug("Tail found at block=%s trx_id=%s trx_num=%s", blk, trx_id, trx_num)
                    return NodePointer(block=blk, trx_id=trx_id, trx_num=trx_num)

            # Move the window further back in history
            oldest_item = history[0]
            if isinstance(oldest_item, dict):
                oldest_idx = oldest_item.get("index", oldest_item.get("_id", 0))
            elif isinstance(oldest_item, (list, tuple)) and len(oldest_item) >= 1:
                oldest_idx = oldest_item[0]
            else:
                break

            if oldest_idx == 0:
                break                   # reached the beginning of account history
            start = max(0, oldest_idx - 1)

        return None

    def _walk_prev_from(self, tail_ptr: NodePointer, stop_ptr: Optional[NodePointer] = None) -> List[ListNode]:
        """
        Starting at *tail_ptr*, walk ``prev`` pointers back to the head,
        collecting every node.  Returns the nodes in head-to-tail order
        with seq numbers assigned (0, 1, 2, …).

        Cycle detection is included as a safety net.
        """
        nodes_reversed: List[ListNode] = []
        ptr = tail_ptr
        seen: set = set()

        while not ptr.is_null():
            if stop_ptr and ptr.trx_id == stop_ptr.trx_id:
                break

            key = ptr.trx_id
            if key in seen:
                logger.error("Cycle detected at trx_id=%s — aborting walk.", key)
                break
            seen.add(key)

            node = self.fetch_node_by_pointer(ptr)
            if node is None:
                logger.warning("Could not fetch node at block=%s trx_id=%s", ptr.block, ptr.trx_id)
                break

            nodes_reversed.append(node)
            ptr = node.prev          # step backwards

        # --- Author validation ---
        if self.enforce_single_author and nodes_reversed:
            head_node = nodes_reversed[-1]

            # Determine the canonical author for this list segment.
            # If we already know it from a previous sync, use that.
            # Otherwise, if we've walked to the true head, use its author.
            list_author = self._author
            if list_author is None and head_node.prev.is_null():
                list_author = head_node.author
                self._author = list_author
                if list_author:
                    logger.info("List author determined from head node: %s", list_author)

            if list_author:
                # Filter the chain, truncating at the first sign of a fork.
                # We iterate from tail to head (current order of nodes_reversed).
                valid_nodes_reversed = []
                for node in nodes_reversed:
                    if node.author != list_author:
                        logger.warning(
                            "Chain broken at trx_id=%s: author '%s' does not match list author '%s'. Truncating list here.",
                            node.trx_id, node.author, list_author
                        )
                        break  # Stop at the first foreign node.
                    valid_nodes_reversed.append(node)
                nodes_reversed = valid_nodes_reversed

        # Reverse to get head-first order and assign canonical seq numbers
        nodes_reversed.reverse()
        for seq, node in enumerate(nodes_reversed):
            node.seq = seq

        for i in range(len(nodes_reversed) - 1):
            nodes_reversed[i].next = nodes_reversed[i + 1].pointer

        return nodes_reversed

    def rebuild_index(self, after_block: Optional[int] = None) -> int:
        """
        Rebuild the in-memory index by walking ``prev`` pointers from the
        tail back to the head — no full blockchain scan required.
        
        Strategy
        --------
        This uses a hybrid tail-discovery strategy to be both efficient and robust:
        1. It queries the primary account's history (`get_account_history`) for a
           candidate tail. This is fast for finding old, single-author lists.
        2. It also scans the last ~30 blocks directly to find any nodes that the
           history API may have missed due to indexing lag, or nodes from other authors.
        3. It compares all candidates and selects the true tail (highest seq/block),
           then walks the `prev` chain backwards to build the index.
        Parameters
        ----------
        after_block : int or None
            If provided, only history entries from blocks *after* this
            number are considered when searching for the tail.  Useful for
            incremental updates: pass ``self._tail.block_num`` to pick up
            only nodes added since the last sync.

        Returns
        -------
        int
            Total number of nodes now in the index.
        """
        self._index.clear()
        self._head = None
        self._tail = None
        self._author = None

        logger.info("Locating tail for ll_id=%s …", self.ll_id)

        # --- Hybrid Tail Discovery ---
        # 1. Use account history for a fast, long-range search (for single-author lists).
        history_tail_ptr = self._find_tail_pointer(after_block=after_block)

        # 2. Scan recent blocks to find tails missed by lagging history or from other authors.
        num_blocks_to_scan = 100 if (not history_tail_ptr or history_tail_ptr.is_null()) else 30
        MAX_SCAN_LIMIT = 200     # Cap the scan at ~10 minutes to prevent massive blockchain reads
        if history_tail_ptr and not history_tail_ptr.is_null():
            try:
                props = self.steem.get_dynamic_global_properties()
                head_block = props.get("head_block_number", 0)
                if head_block > history_tail_ptr.block:
                    # Scan the gap to cover history lag, but strictly cap it to avoid freezing the app
                    lag_gap = head_block - history_tail_ptr.block + 10
                    num_blocks_to_scan = max(num_blocks_to_scan, min(lag_gap, MAX_SCAN_LIMIT))
                    logger.debug("Dynamically scanning %d blocks to cover history lag.", num_blocks_to_scan)
            except Exception:
                pass  # Stick with default on error
        
        recent_nodes = self._scan_blocks_for_nodes(num_blocks=num_blocks_to_scan)

        candidate_nodes = []
        if history_tail_ptr and not history_tail_ptr.is_null():
            # Fetch the node to get its sequence number for sorting
            history_tail_node = self.fetch_node_by_pointer(history_tail_ptr)
            if history_tail_node:
                candidate_nodes.append(history_tail_node)

        candidate_nodes.extend(recent_nodes)

        if not candidate_nodes:
            logger.info("No transactions found for ll_id=%s.", self.ll_id)
            return 0

        # 3. Find the true tail among all candidates (Last-Write-Wins).
        def sort_key(n: ListNode):
            return (n.seq, n.block_num or 0, n.trx_num if n.trx_num is not None else 0)

        unique_candidates = {n.trx_id: n for n in candidate_nodes if n.trx_id}.values()
        true_tail_node = sorted(unique_candidates, key=sort_key)[-1]
        tail_ptr = true_tail_node.pointer

        logger.info("Walking prev-chain from block=%s …", tail_ptr.block)
        nodes = self._walk_prev_from(tail_ptr)

        for node in nodes:
            self._index[node.seq] = node

        if self._index:
            self._head = self._index[0]
            self._tail = self._index[max(self._index.keys())]
            
        self._apply_tombstones()

        logger.info("Index rebuilt: %d nodes loaded.", len(self._index))
        return len(self._index)

    def sync(self) -> int:
        """
        Incrementally sync the index by fetching only nodes added since the
        current tail.  Much faster than a full ``rebuild_index()`` when the
        list is long and only a few new nodes have been appended.

        Returns the number of *new* nodes added to the index.
        """
        if self._tail is None:
            return self.rebuild_index()

        after_block = self._tail.block_num
        if not after_block: # Should not happen if tail exists, but as a safeguard
            return self.rebuild_index()
            
        logger.info("Syncing from block=%s …", after_block)

        # --- Hybrid Tail Discovery for new nodes ---
        history_tail_ptr = self._find_tail_pointer(after_block=after_block)
        
        num_blocks_to_scan = 100 if (not history_tail_ptr or history_tail_ptr.is_null()) else 30
        MAX_SCAN_LIMIT = 200
        if history_tail_ptr and not history_tail_ptr.is_null():
            try:
                props = self.steem.get_dynamic_global_properties()
                head_block = props.get("head_block_number", 0)
                if head_block > history_tail_ptr.block:
                    lag_gap = head_block - history_tail_ptr.block + 10
                    num_blocks_to_scan = max(num_blocks_to_scan, min(lag_gap, MAX_SCAN_LIMIT))
            except Exception:
                pass
                
        recent_nodes = self._scan_blocks_for_nodes(num_blocks=num_blocks_to_scan)

        candidate_nodes = []
        if history_tail_ptr and not history_tail_ptr.is_null():
            history_tail_node = self.fetch_node_by_pointer(history_tail_ptr)
            if history_tail_node:
                candidate_nodes.append(history_tail_node)
        
        candidate_nodes.extend([n for n in recent_nodes if n.block_num and n.block_num > after_block])

        if not candidate_nodes:
            logger.info("No new nodes found.")
            return 0

        def sort_key(n: ListNode):
            return (n.seq, n.block_num or 0, n.trx_num if n.trx_num is not None else 0)

        unique_candidates = {n.trx_id: n for n in candidate_nodes if n.trx_id}.values()
        true_tail_node = sorted(unique_candidates, key=sort_key)[-1]
        tail_ptr = true_tail_node.pointer

        if self._tail and tail_ptr.trx_id == self._tail.trx_id:
            logger.info("No new nodes found.")
            return 0

        stop_ptr = self._tail.pointer if self._tail else None
        new_nodes = self._walk_prev_from(tail_ptr, stop_ptr=stop_ptr)

        # The walk stops when it reaches a node already in the index (prev is
        # our current tail), so new_nodes are all genuinely new.
        base_seq = max(self._index.keys()) + 1 if self._index else 0
        for offset, node in enumerate(new_nodes):
            node.seq = base_seq + offset
            self._index[node.seq] = node

        # Link the old tail to the new nodes
        if self._tail and new_nodes:
            self._tail.next = new_nodes[0].pointer

        if new_nodes:
            self._tail = self._index[max(self._index.keys())]
            
        self._apply_tombstones()

        logger.info("Sync added %d new nodes.", len(new_nodes))
        return len(new_nodes)

    def _apply_tombstones(self) -> None:
        """Find appended tombstone nodes and logically mark their targets as deleted."""
        for node in self._index.values():
            if node.payload.get("_deleted") and "_target_trx_id" in node.payload:
                target_trx = node.payload["_target_trx_id"]
                for t_node in self._index.values():
                    if t_node.trx_id == target_trx:
                        t_node.payload["_deleted"] = True

    def _scan_blocks_for_nodes(self, num_blocks: int) -> List[ListNode]:
        """Scans the last `num_blocks` and returns all nodes for this list."""
        found_nodes = []
        try:
            props = self.steem.get_dynamic_global_properties()
            head_block = props.get("head_block_number", 0)
        except Exception as e:
            logger.warning("Could not get head block number: %s", e)
            return []

        start_block = max(1, head_block - num_blocks)
        logger.debug("Scanning blocks from %d to %d for list nodes...", start_block, head_block)

        for block_num in range(head_block, start_block - 1, -1):
            try:
                block_data = self.steem.get_block(block_num)
                if not block_data:
                    continue
                
                transactions = block_data.get("transactions", [])
                tx_ids = block_data.get("transaction_ids", [])

                for tx_idx, tx in enumerate(transactions):
                    tx_id = tx.get("transaction_id")
                    if not tx_id and tx_idx < len(tx_ids):
                        tx_id = tx_ids[tx_idx]
                    
                    for op_wrapper in tx.get("operations", []):
                        op_type, op_value = self._extract_op(op_wrapper)
                        if op_type == "custom_json" and op_value.get("id") == self.custom_json_id:
                            node = self._node_from_op(op_value, block_num, tx_id, tx_idx)
                            if node:
                                found_nodes.append(node)
            except Exception as e:
                logger.warning("Failed to process block %d: %s", block_num, e)
                continue
                
        return found_nodes

    def _is_orphaned(self, node: ListNode) -> Optional[bool]:
        """
        Check if the given node was orphaned by a concurrent writer by scanning
        recent blocks to find the canonical tail. This method does not
        rely on account_history and works for multi-author lists.
        """
        logger.debug("Verifying node %s by scanning recent blocks...", node.trx_id)
        
        target_block = node.block_num or 0
        try:
            props = self.steem.get_dynamic_global_properties()
            head_block = props.get("head_block_number", target_block + 30)
        except Exception:
            head_block = target_block + 30
            
        # Ensure we scan deep enough to cover the node's block, plus a buffer
        num_blocks = max(25, head_block - target_block + 10)
        recent_nodes = self._scan_blocks_for_nodes(num_blocks=num_blocks)

        if not any(n.trx_id == node.trx_id for n in recent_nodes):
            logger.debug("Node not found in initial scan, waiting 3s and rescanning...")
            time.sleep(3)
            # Head block likely increased by 1 while waiting
            recent_nodes = self._scan_blocks_for_nodes(num_blocks=num_blocks + 1)

        if not recent_nodes:
            logger.warning("Could not find any list nodes in recent blocks. Cannot determine status.")
            return None

        if not any(n.trx_id == node.trx_id for n in recent_nodes):
            logger.warning("Just-broadcasted node %s not found in recent blocks. Cannot determine status.", node.trx_id)
            return None

        def sort_key(n: ListNode):
            return (n.seq, n.block_num or 0, n.trx_num if n.trx_num is not None else 0)

        canonical_tail = sorted(recent_nodes, key=sort_key)[-1]
        logger.debug("Canonical tail identified as seq=%d, trx=%s", canonical_tail.seq, canonical_tail.trx_id)

        ptr = canonical_tail.pointer
        walk_limit = len(self._index) + len(recent_nodes) + 5
        for _ in range(walk_limit):
            if ptr.is_null():
                break
            
            if ptr.trx_id == node.trx_id:
                logger.debug("Node %s found in canonical chain. Not orphaned.", node.trx_id)
                return False

            fetched = self.fetch_node_by_pointer(ptr)
            if not fetched:
                logger.warning("Chain walk broke at %s, could not fetch.", ptr.trx_id)
                break
            ptr = fetched.prev

        logger.warning("Node %s was not found in canonical chain. It is orphaned.", node.trx_id)
        return True

    def _require_index(self) -> None:
        if not self._index and self._head is None:
            raise RuntimeError(
                "Index is empty. Call rebuild_index() first, or append nodes."
            )
    # ------------------------------------------------------------------ #
    # Core list operations
    # ------------------------------------------------------------------ #

    def append(self, payload: Dict[str, Any], timestamp: Optional[str] = None) -> ListNode:
        """
        Append a new node carrying *payload* to the tail of the list.

        Returns the confirmed ListNode (block_num and trx_id populated).
        """
        if self.enforce_single_author and self._author and self.account != self._author:
            raise ValueError(
                f"Account '{self.account}' does not match the list author '{self._author}'. "
                f"Cannot append to this list with enforce_single_author=True."
            )

        # Automatically create a dataless anchor if the list is completely empty
        if not self._index and not payload.get("_is_anchor"):
            logger.info("List is empty. Automatically creating a dataless anchor at seq=0.")
            self.append({"_is_anchor": True, "desc": "List Anchor", "_nonce": random.randint(0, 9999999)}, timestamp=timestamp)

        seq = len(self._index)
        prev_ptr = self._tail.pointer if self._tail else NodePointer.null()
        head_ptr = self._head.pointer if self._head else NodePointer.null()

        # Head pointer for the very first node points to itself (filled after broadcast).
        if seq == 0:
            # Placeholder — will be updated once we know the trx_id.
            head_ptr = NodePointer.null()

        initiated_time = timestamp or datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()

        node = ListNode(
            ll_id=self.ll_id,
            seq=seq,
            head=head_ptr,
            prev=prev_ptr,
            next=NodePointer.null(),
            payload=payload,
            timestamp=initiated_time,
        )

        result = self._broadcast(node)

        # Populate confirmed location
        node.block_num = result.get("block_num")
        node.trx_id    = result.get("id") or result.get("trx_id") or result.get("transaction_id", "")
        node.trx_num   = result.get("trx_num")
        node.author    = self.account

        if seq == 0:
            # Self-referential head pointer
            node.head = node.pointer
            if self.enforce_single_author:
                self._author = self.account
        else:
            node.head = self._head.pointer  # type: ignore[union-attr]

        # Update previous tail's next pointer in memory
        if self._tail:
            self._tail.next = node.pointer

        # Update in-memory index
        self._index[seq] = node
        if seq == 0:
            self._head = node
        self._tail = node

        logger.info(
            "Appended node seq=%d  block=%s  trx_id=%s",
            seq, node.block_num, node.trx_id,
        )
        return node

    def safe_append(self, payload: Dict[str, Any], max_retries: int = 5, wait_time: float = 15.0) -> ListNode:
        """
        Append a node with Optimistic Concurrency Control (OCC).
        
        Use this method instead of `append()` if you expect multiple instances 
        of your application to write to the list simultaneously.
        
        It broadcasts the node, waits for propagation, and verifies that the node
        became part of the canonical main chain. If a concurrent instance appended 
        at the exact same time and caused a fork, the orphaned node is detected, 
        and the append is automatically retried on the winning branch.
        """
        initiated_time = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()

        for attempt in range(max_retries):
            self.sync()
            old_tail = self._tail
            
            try:
                node = self.append(payload, timestamp=initiated_time)
                logger.info("safe_append attempt %d: broadcast tx %s", attempt + 1, node.trx_id)
            except Exception as e:
                logger.warning("safe_append attempt %d failed during broadcast (collision or network error): %s. Retrying...", attempt + 1, e)
                current_wait = wait_time * (1.5 ** attempt) + random.uniform(0, 3.0)
                time.sleep(current_wait)
                self.rebuild_index()
                
                # Check if it was actually broadcasted despite the exception
                found_node = None
                start_seq = old_tail.seq if old_tail else 0
                for n in self._index.values():
                    if n.seq >= start_seq and n.timestamp == initiated_time and n.author == self.account and n.payload == payload:
                        found_node = n
                        break
                if found_node:
                    logger.info("Transaction was actually successful despite exception: %s", found_node.trx_id)
                    node = found_node
                else:
                    continue
            
            # Wait for potential concurrent writes to settle in the blockchain
            # Exponential backoff with jitter to reduce collision probability on retry
            current_wait = wait_time * (1.5 ** attempt) + random.uniform(0, 3.0)
            time.sleep(current_wait)
            
            is_orphaned_status = self._is_orphaned(node)
            if is_orphaned_status is False:
                return node
                
            if is_orphaned_status is None:
                raise RuntimeError(
                    f"Could not verify append of {node.trx_id}. The node was not found in "
                    "recent blocks, which could indicate a broadcast issue or severe API node lag. "
                    "Cannot confirm state."
                )

            logger.warning(
                "safe_append collision detected: %s was orphaned by a concurrent writer. Retrying...", 
                node.trx_id
            )
            
            # Rollback local state completely to be safe against complex multi-node forks
            self.rebuild_index()
                
        raise RuntimeError(f"Failed to safely append after {max_retries} attempts due to high contention.")

    # ------------------------------------------------------------------ #
    # Soft-delete  (tombstone approach — Steem is immutable)
    # ------------------------------------------------------------------ #

    def delete(self, seq: int, timestamp: Optional[str] = None) -> ListNode:
        """
        Logically delete the node at *seq* by appending a tombstone node.
        
        Because the blockchain is immutable, we cannot alter the original node.
        Instead, we append a new node to the tail of the list to maintain the
        continuous `prev` chain, carrying a payload that marks the target as deleted.
        """
        self._require_index()
        if seq == 0:
            raise ValueError("Cannot delete the root anchor node (seq=0).")
        if seq not in self._index:
            raise KeyError(f"No node at seq={seq}.")

        target = self._index[seq]
        if target.payload.get("_is_anchor"):
            raise ValueError("Cannot delete an anchor node.")
        if target.payload.get("_deleted"):
            raise ValueError(f"Node at seq={seq} is already deleted or is a tombstone.")
            
        tombstone_payload = {"_deleted": True, "_target_trx_id": target.trx_id, "_nonce": random.randint(0, 9999999)}
        
        # Keep the prev-chain continuous by making the tombstone a normal append
        tombstone = self.append(tombstone_payload, timestamp=timestamp)
        
        # Logically delete the target in the local index
        target.payload["_deleted"] = True
        
        logger.info("Tombstoned node seq=%d via append seq=%d", seq, tombstone.seq)
        return tombstone

    def safe_delete(self, seq: int, max_retries: int = 5, wait_time: float = 15.0) -> ListNode:
        """
        Logically delete a node using Optimistic Concurrency Control (OCC).
        Safe to use when multiple instances might be modifying the list.
        """
        initiated_time = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
        op_nonce = random.randint(0, 9999999)

        for attempt in range(max_retries):
            self.sync()
            
            self._require_index()
            if seq == 0:
                raise ValueError("Cannot delete the root anchor node (seq=0).")
            if seq not in self._index:
                raise KeyError(f"No node at seq={seq}.")

            target = self._index[seq]
            if target.payload.get("_is_anchor"):
                raise ValueError("Cannot delete an anchor node.")
            if target.payload.get("_deleted"):
                if attempt > 0:
                    logger.info("Node at seq=%d was successfully deleted by a concurrent writer. Aborting redundant delete.", seq)
                    for n in self._index.values():
                        if n.payload.get("_deleted") and n.payload.get("_target_trx_id") == target.trx_id:
                            return n
                    return target
                raise ValueError(f"Node at seq={seq} is already deleted or is a tombstone.")
                
            old_tail = self._tail
            tombstone_payload = {"_deleted": True, "_target_trx_id": target.trx_id, "_nonce": op_nonce}
            
            try:
                # Broadcast directly using append
                tombstone = self.append(tombstone_payload, timestamp=initiated_time)
                logger.info("safe_delete attempt %d: broadcast tx %s", attempt + 1, tombstone.trx_id)
            except Exception as e:
                logger.warning("safe_delete attempt %d failed during broadcast (collision or network error): %s. Retrying...", attempt + 1, e)
                current_wait = wait_time * (1.5 ** attempt) + random.uniform(0, 3.0)
                time.sleep(current_wait)
                self.rebuild_index()
                
                found_node = None
                start_seq = old_tail.seq if old_tail else 0
                for n in self._index.values():
                    if n.seq >= start_seq and n.timestamp == initiated_time and n.author == self.account and n.payload == tombstone_payload:
                        found_node = n
                        break
                if found_node:
                    logger.info("Tombstone transaction was actually successful despite exception: %s", found_node.trx_id)
                    tombstone = found_node
                else:
                    continue
            
            # Exponential backoff with jitter to reduce collision probability on retry
            current_wait = wait_time * (1.5 ** attempt) + random.uniform(0, 3.0)
            time.sleep(current_wait)
            
            is_orphaned_status = self._is_orphaned(tombstone)
            if is_orphaned_status is False:
                target.payload["_deleted"] = True
                logger.info("Safely tombstoned node seq=%d via append seq=%d", seq, tombstone.seq)
                return tombstone
                
            if is_orphaned_status is None:
                raise RuntimeError(f"Could not verify delete of {tombstone.trx_id}. Cannot confirm state.")
                
            logger.warning("safe_delete collision detected: %s was orphaned. Retrying...", tombstone.trx_id)
            
            # Rollback local state completely to be safe against complex multi-node forks
            self.rebuild_index()

        raise RuntimeError(f"Failed to safely delete after {max_retries} attempts due to high contention.")

    def delete_active(self, index: int, timestamp: Optional[str] = None) -> ListNode:
        """
        Logically delete the n-th active (non-deleted) node in the list.
        Supports negative indexing (e.g., -1 for the last active node).
        """
        target_node = self.get_active(index)
        return self.delete(target_node.seq, timestamp=timestamp)

    def safe_delete_active(self, index: int, max_retries: int = 5, wait_time: float = 15.0) -> ListNode:
        """
        Logically delete the n-th active node using Optimistic Concurrency Control.
        """
        target_node = self.get_active(index)
        return self.safe_delete(target_node.seq, max_retries, wait_time)

    # ------------------------------------------------------------------ #
    # Traversal
    # ------------------------------------------------------------------ #

    def __len__(self) -> int:
        return len(self._index)

    def __iter__(self) -> Iterator[ListNode]:
        """Iterate nodes in sequence order, skipping deleted nodes and the anchor."""
        for seq in sorted(self._index.keys()):
            node = self._index[seq]
            if node.payload.get("_is_anchor"):
                continue
            if not node.payload.get("_deleted", False):
                yield node

    def __getitem__(self, seq: int) -> ListNode:
        return self._index[seq]

    def __contains__(self, seq: int) -> bool:
        return seq in self._index

    def head(self) -> Optional[ListNode]:
        """Return the head node (first non-deleted node), or None."""
        for node in self:
            return node
        return None

    def tail(self) -> Optional[ListNode]:
        """Return the tail node (last non-deleted node), or None."""
        last = None
        for node in self:
            last = node
        return last

    def get(self, seq: int, default: Any = None) -> Optional[ListNode]:
        """Return the node at *seq*, or *default* if absent."""
        return self._index.get(seq, default)

    def get_active(self, index: int) -> ListNode:
        """
        Return the n-th active (non-deleted) node in the list.
        Supports negative indexing (e.g., -1 for the last active node).
        """
        if index < 0:
            # For negative indexing, we rely on to_list() to resolve the offset from the end
            active_nodes = self.to_list(include_deleted=False)
            return active_nodes[index]
            
        for i, node in enumerate(self):
            if i == index:
                return node
                
        raise IndexError("List index out of range")

    def to_list(self, include_deleted: bool = False, include_anchor: bool = False) -> List[ListNode]:
        """Return all nodes as a list, optionally including tombstones or the anchor."""
        nodes = sorted(self._index.values(), key=lambda n: n.seq)
        if not include_anchor:
            nodes = [n for n in nodes if not n.payload.get("_is_anchor")]
        if not include_deleted:
            nodes = [n for n in nodes if not n.payload.get("_deleted", False)]
        return nodes

    def payloads(self, include_deleted: bool = False, include_anchor: bool = False) -> List[Dict[str, Any]]:
        """Return just the payload dicts in sequence order."""
        return [n.payload for n in self.to_list(include_deleted=include_deleted, include_anchor=include_anchor)]

    def find(self, predicate) -> Optional[ListNode]:
        """Return the first node whose payload satisfies *predicate(payload)*."""
        for node in self:
            if predicate(node.payload):
                return node
        return None

    def find_all(self, predicate) -> List[ListNode]:
        """Return all nodes whose payload satisfies *predicate(payload)*."""
        return [node for node in self if predicate(node.payload)]

    def walk(
        self,
        start_seq: int = 0,
        reverse: bool = False,
    ) -> Iterator[ListNode]:
        """
        Walk the list from *start_seq*, yielding each non-deleted node.

        Parameters
        ----------
        start_seq : int
            Sequence number to begin from.
        reverse : bool
            If True, walk backwards toward the head.
        """
        keys = sorted(self._index.keys())
        if reverse:
            keys = [k for k in reversed(keys) if k <= start_seq]
        else:
            keys = [k for k in keys if k >= start_seq]

        for k in keys:
            node = self._index[k]
            if node.payload.get("_is_anchor"):
                continue
            if not node.payload.get("_deleted", False):
                yield node

    # ------------------------------------------------------------------ #
    # Blockchain fetch helpers
    # ------------------------------------------------------------------ #

    def fetch_node_by_pointer(self, pointer: NodePointer) -> Optional[ListNode]:
        """
        Fetch and parse a single node directly from the blockchain using
        a NodePointer (block_num + trx_id).

        Returns None if the transaction cannot be found or is not a list node.
        """
        if pointer.is_null():
            return None
        try:
            block = self.steem.get_block(pointer.block)
            if not block:
                return None
                
            transactions = block.get("transactions", [])
            
            # Fast path using trx_num
            if pointer.trx_num is not None and 0 <= pointer.trx_num < len(transactions):
                tx = transactions[pointer.trx_num]
                tid = tx.get("transaction_id") or tx.get("id", "")
                if tid == pointer.trx_id:
                    for op_wrapper in tx.get("operations", []):
                        op_type, op_value = self._extract_op(op_wrapper)
                        if op_type == "custom_json":
                            return self._node_from_op(op_value, pointer.block, tid, pointer.trx_num)

            # Fallback scan for backwards compatibility with older pointers
            for tx_idx, tx in enumerate(transactions):
                tid = tx.get("transaction_id") or tx.get("id", "")
                if tid == pointer.trx_id:
                    for op_wrapper in tx.get("operations", []):
                        op_type, op_value = self._extract_op(op_wrapper)
                        if op_type == "custom_json":
                            return self._node_from_op(op_value, pointer.block, tid, tx_idx)
        except Exception as exc:
            logger.warning("fetch_node_by_pointer failed for block %s: %s", pointer.block, exc)
                
        return None

    # ------------------------------------------------------------------ #
    # Serialisation
    # ------------------------------------------------------------------ #

    def export_index(self) -> List[Dict[str, Any]]:
        """Serialise the entire in-memory index to a JSON-serialisable list."""
        result = []
        for seq in sorted(self._index.keys()):
            n = self._index[seq]
            entry = n.to_json_payload()
            entry["block_num"] = n.block_num
            entry["trx_id"]    = n.trx_id
            entry["trx_num"]   = n.trx_num
            entry["author"]    = n.author
            result.append(entry)
        return result

    def import_index(self, data: List[Dict[str, Any]]) -> None:
        """
        Load a previously exported index without hitting the blockchain.

        Useful for caching / faster startup.
        """
        self._index.clear()
        self._author = None
        for entry in data:
            block_num = entry.pop("block_num", None)
            trx_id    = entry.pop("trx_id", None)
            trx_num   = entry.pop("trx_num", None)
            author    = entry.pop("author", None)
            node = ListNode.from_json_payload(
                entry, block_num=block_num, trx_id=trx_id, trx_num=trx_num, author=author
            )
            self._index[node.seq] = node
        if self._index:
            self._head = self._index[0]
            self._tail = self._index[max(self._index.keys())]
            if self.enforce_single_author and self._head.author:
                self._author = self._head.author

        self._apply_tombstones()

    # ------------------------------------------------------------------ #
    # Dunder helpers
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:
        return (
            f"SteemLinkedList(ll_id={self.ll_id!r}, "
            f"account={self.account!r}, "
            f"nodes={len(self._index)})"
        )
