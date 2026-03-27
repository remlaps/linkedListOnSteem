# ⚠️ Important: Concurrency and Forks

This library is designed for scenarios where a **single process** appends to a list.

**Do not attempt to append to the same list from multiple processes or accounts simultaneously.** Doing so will create a "fork" in the list, where different clients will see different versions of the data, leading to data inconsistency.

To mitigate this, the library includes an `enforce_single_author` protection (enabled by default) which ensures that only the account that created the list can append new nodes. While this prevents forks from *different accounts*, it does **not** protect against concurrent appends from the *same account* running in parallel processes.

# linkedListOnSteem

A Python library for implementing a doubly-linked list data structure using `custom_json` transactions on the Steem blockchain.

## Overview

Because Steem transactions are immutable once confirmed, traditional "next" pointers cannot be back-patched when new nodes are added. This library overcomes this constraint by maintaining a continuous chain of `prev` pointers (using precise `block_num` and `trx_id` references) and building a lightweight off-chain index. 

The index is quickly rebuilt on-demand by locating the list's tail in the account history and walking the pointers backwards to the head. This requires only O(n) targeted API fetches rather than a broad, expensive blockchain scan, typically completing in seconds.

## Features

* **Append-Only Immutability**: Store arbitrary JSON payloads securely on the Steem blockchain.
* **Soft Deletion**: Logical deletion via tombstone nodes appended to the list, preserving the integrity of the immutable `prev` chain while seamlessly hiding deleted items from normal traversal.
* **Efficient Syncing**: 
    * `rebuild_index()`: Reconstructs the list in seconds, regardless of how long ago it was created.
    * `sync()`: Incrementally fetches only new nodes added since the last check.
* **Local Caching**: Export and import the list index (`export_index()` / `import_index()`) to avoid re-querying the blockchain on startup.
* **Rich Traversal**: Forward and reverse iteration, absolute and active index access (`get()`, `get_active()`), and predicate-based searching (`find()`, `find_all()`).

## Requirements

* Python 3.7+
* `steem-python` (`pip install steem`)

## Quick Start

Make sure to set your Steem posting key securely, for example via an environment variable.

```python
import os
from steem import Steem
from steem_linked_list import SteemLinkedList

# 1. Connect to Steem
POSTING_KEY = os.getenv("STEEM_POSTING_KEY")
steem = Steem(keys=[POSTING_KEY])

# 2. Initialize the List
ll = SteemLinkedList(
    steem_instance=steem,
    account="your-steem-account",
    ll_id="my_unique_list_id",
    custom_json_id="linkedListOnSteem"
)

# 3. Append Nodes
ll.append({"title": "First Entry", "value": 100})
ll.append({"title": "Second Entry", "value": 200})

# 4. Read & Traverse
# (In a fresh session, rebuild the index first)
ll.rebuild_index() 

for node in ll:
    print(f"Sequence: {node.seq}, Data: {node.payload}")
```

## Examples and Testing

* **Examples**: Check out `examples/steem_linked_list_examples.py` for a comprehensive walkthrough of all features, including soft-deletion, reverse traversal, and caching.
* **Tests**: Run `tests/steem_linked_list_post_test.py` to execute a full integration test suite. This script optionally broadcasts its execution report directly to the Steem blockchain as a top-level post.

## License

MIT
