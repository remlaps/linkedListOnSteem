"""
steem_linked_list_examples.py
==============================
Usage examples for the steem_linked_list library.

Setup
-----
    pip install steem

Quick-start
-----------
Set your STEEM_ACCOUNT and STEEM_POSTING_KEY environment variables, then run:

    python examples/steem_linked_list_examples.py

"""

import json
import os
import sys
import time

# Add parent directory to sys.path so it can find steem_linked_list.py
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from steem import Steem
from steem_linked_list import SteemLinkedList, NodePointer


# ---------------------------------------------------------------------------
# 1. Connect to Steem
# ---------------------------------------------------------------------------

ACCOUNT = os.getenv("STEEM_ACCOUNT", "")
POSTING_KEY = os.getenv("STEEM_POSTING_KEY", "")

if not POSTING_KEY:
    print("Error: Please set the STEEM_POSTING_KEY environment variable.")
    sys.exit(1)

steem = Steem(
    nodes=["https://api.steemit.com"],
    keys=[POSTING_KEY],   # posting key
)
LIST_ID      = f"my_photo_log__example_{int(time.time())}"        # unique per list - create a new one each time
# LIST_ID      = f"my_photo_log_example"                              # unique per list - Reuse a potentially existing one
CUSTOM_JSON_ID = "linkedListOnSteem"                        # on-chain custom_json id (≤32 chars)

# ---------------------------------------------------------------------------
# 2. Create the list helper
# ---------------------------------------------------------------------------

ll = SteemLinkedList(
    steem_instance=steem,
    account=ACCOUNT,
    ll_id=LIST_ID,
    custom_json_id=CUSTOM_JSON_ID,
    use_active_key=False,   # posting key is fine for custom_json
    wait_for_irreversible=True,
)


# ---------------------------------------------------------------------------
# 3. Append nodes  (each becomes a custom_json transaction on-chain)
# ---------------------------------------------------------------------------

# Fast append (assumes no concurrent writers)
node_a = ll.append({"title": "Conowingo Dam", "species": "Bald Eagle", "count": 3})

# Safe append (Optimistic Concurrency Control - automatically resolves forks if multiple instances are running)
node_b = ll.safe_append({"title": "Susquehanna Corridor", "species": "Red-tailed Hawk", "count": 7})
node_c = ll.safe_append({"title": "West Chester PA", "species": "Turkey Vulture", "count": 12})
node_d = ll.safe_append({"title": "Longwood Gardens", "species": "Northern Cardinal", "count": 5})

print(f"Head: block={node_a.block_num}  trx_id={node_a.trx_id}  trx_num={node_a.trx_num}")
print(f"Tail: block={node_d.block_num}  trx_id={node_d.trx_id}  trx_num={node_d.trx_num}")

# Wait for block confirmation before querying account history
print("\n*Waiting for blocks to be indexed by API nodes...*")
time.sleep(90) # A short wait is still helpful for nodes to catch up

# ---------------------------------------------------------------------------
# 4. Rebuild the index from account history + prev-pointer walking
#    (no blockchain scan — just targeted fetches)
# ---------------------------------------------------------------------------

ll_fresh = SteemLinkedList(steem, ACCOUNT, LIST_ID, CUSTOM_JSON_ID)
n = ll_fresh.rebuild_index()
print(f"Loaded {n} nodes by walking prev-chain.")

# Incremental sync — only fetches nodes newer than the current tail:
new_count = ll_fresh.sync()
print(f"Sync found {new_count} new nodes.")


# ---------------------------------------------------------------------------
# 5. Traversal
# ---------------------------------------------------------------------------

# --- Forward iteration (skips deleted / tombstoned nodes) ---
print("\n--- All nodes (forward) ---")
for node in ll_fresh:
    print(f"  seq={node.seq}  payload={node.payload}")

# --- Reverse walk from the tail ---
print("\n--- Reverse walk ---")
tail_seq = ll_fresh.tail().seq
for node in ll_fresh.walk(start_seq=tail_seq, reverse=True):
    print(f"  seq={node.seq}  payload={node.payload}")

# --- head() / tail() ---
print("\nHead node:", ll_fresh.head().payload)
print("Tail node:", ll_fresh.tail().payload)

# --- Random access by seq ---
node_at_1 = ll_fresh[1]
print("\nNode at seq=1:", node_at_1.payload)

# --- Search by predicate ---
hawk_node = ll_fresh.find(lambda p: p.get("species") == "Red-tailed Hawk")
if hawk_node:
    print("\nFound hawk sighting at block:", hawk_node.block_num)

all_large = ll_fresh.find_all(lambda p: p.get("count", 0) >= 7)
print(f"\nSightings with count >= 7: {[n.payload['title'] for n in all_large]}")


# ---------------------------------------------------------------------------
# 6. Soft-delete (tombstone) a node
# ---------------------------------------------------------------------------

# Initial active list: ['Conowingo Dam', 'Susquehanna Corridor', 'West Chester PA', 'Longwood Gardens']

node_to_delete_by_seq = ll_fresh.get(2) # Absolute position 2 is 'Susquehanna Corridor'
tombstone = ll_fresh.safe_delete(2)     # Concurrency-safe delete
print(f"\nTombstoned seq=2 ('{node_to_delete_by_seq.payload.get('title')}') via trx_id={tombstone.trx_id} (trx_num={tombstone.trx_num})")

# Now demonstrate delete_active on the remaining active nodes: ['Conowingo Dam', 'West Chester PA', 'Longwood Gardens']
node_to_delete_by_active_index = ll_fresh.get_active(1) # Active index 1 is now 'West Chester PA'
tombstone2 = ll_fresh.safe_delete_active(1)
print(f"Tombstoned active index 1 ('{node_to_delete_by_active_index.payload.get('title')}') via delete_active, trx_id={tombstone2.trx_id}")

# Deleted nodes are automatically skipped during normal iteration:
print("\n--- Nodes after delete (deleted hidden) ---")
for node in ll_fresh:
    print(f"  seq={node.seq}  {node.payload.get('title')}")

# Access including deleted:
print("\n--- All nodes including tombstones ---")
for node in ll_fresh.to_list(include_deleted=True, include_anchor=True):
    marker = " [DELETED]" if node.payload.get("_deleted") else ""
    print(f"  seq={node.seq}  {node.payload.get('title', node.payload.get('desc', '?'))}{marker}")

# --- get() vs get_active() ---
print("\n--- Absolute vs Active indexing ---")
# After deletions, active list is ['Conowingo Dam', 'Longwood Gardens']
print(f"get(2) [Absolute seq=2, now deleted]: '{ll_fresh.get(2).payload.get('title')}'")
print(f"get(3) [Absolute seq=3, also deleted]: '{ll_fresh.get(3).payload.get('title')}'")
print(f"get_active(0) [1st active node]: '{ll_fresh.get_active(0).payload.get('title')}'")
print(f"get_active(1) [2nd active node]: '{ll_fresh.get_active(1).payload.get('title')}'")
print(f"get_active(-1) [last active node]: '{ll_fresh.get_active(-1).payload.get('title')}'")

# ---------------------------------------------------------------------------
# 7. Fetch a single node directly by pointer (no index needed)
# ---------------------------------------------------------------------------

ptr = NodePointer(block=node_b.block_num, trx_id=node_b.trx_id, trx_num=node_b.trx_num)
fetched = ll_fresh.fetch_node_by_pointer(ptr)
if fetched:
    print(f"\nFetched live from chain: {fetched.payload}")


# ---------------------------------------------------------------------------
# 8. Export / import the index (for caching)
# ---------------------------------------------------------------------------

exported = ll_fresh.export_index()
with open("ll_cache.json", "w") as f:
    json.dump(exported, f, indent=2)
print(f"\nExported {len(exported)} nodes to ll_cache.json")

# Restore from cache (no blockchain scan needed):
ll_cached = SteemLinkedList(steem, ACCOUNT, LIST_ID, CUSTOM_JSON_ID)
with open("ll_cache.json") as f:
    ll_cached.import_index(json.load(f))
print(f"Restored from cache: {ll_cached}")


# ---------------------------------------------------------------------------
# 9. Low-level: inspect raw on-chain envelope
# ---------------------------------------------------------------------------

print("\n--- Raw on-chain JSON for node_a ---")
print(json.dumps(node_a.to_json_payload(), indent=2))
