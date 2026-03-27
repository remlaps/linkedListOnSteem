# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.0.3-alpha] - 2026-03-27

### Added
- **Optimistic Concurrency Control (OCC)**: Added `safe_append()`, `safe_delete()`, and `safe_delete_active()`. These methods automatically detect if their transaction was orphaned by a concurrent writer (a fork) and automatically re-sync and retry on the winning branch. This completely eliminates data loss when running multiple concurrent app instances.
- `_is_orphaned(node)` helper method to verify if a node is in the canonical blockchain list.

### Changed
- Removed the prominent warning in `README.md` prohibiting multiple instances from writing simultaneously.

## [0.0.2-alpha] - 2026-03-26

### Added
- **Single Author Enforcement**: A new `enforce_single_author` parameter (default `True`) in the `SteemLinkedList` constructor prevents other accounts from appending to or deleting from a list. This provides a crucial backstop against accidental list forks.

### Changed
- Updated `README.md` with a prominent warning about concurrency and the new single-author protection.

## [0.0.1-alpha] - 2026-03-25

### Changed

- Decoupled `CUSTOM_JSON_ID` from the software version. The ID now represents the data protocol and should remain stable (e.g., `linkedListOnSteem`) unless the on-chain data format changes. This ensures that future client versions can read lists created by older versions.

### Added

- Initial release of `steem_linked_list`.
- `SteemLinkedList` class to manage on-chain linked lists using Steem `custom_json` operations.
- **Node Management**:
    - `append()`: Add new nodes to the end of the list.
    - `delete()`: Soft-delete (tombstone) a node by its absolute sequence number.
    - `delete_active()`: Soft-delete a node by its relative index in the active list.
- **Indexing and Syncing**:
    - `rebuild_index()`: Reconstruct the list index by walking the chain of `prev` pointers from the tail.
    - `sync()`: Incrementally fetch new nodes added since the last sync.
    - `import_index()` / `export_index()`: Cache the list index to a file to avoid re-scanning the blockchain.
- **Data Access and Traversal**:
    - `head()` and `tail()`: Get the first and last nodes.
    - `get()`: Access a node by its absolute sequence number (including deleted).
    - `get_active()`: Access a node by its relative index (skipping deleted).
    - `__getitem__`: Support for square-bracket indexing (e.g., `ll[i]`).
    - `find()` and `find_all()`: Search for nodes using a predicate function.
    - `walk()`: Traverse the list forwards or backwards from any starting point.
    - Iteration support (`for node in ll:`), which automatically skips deleted nodes.
- **Low-level operations**:
    - `fetch_node_by_pointer()`: Directly fetch a single node from the blockchain given its pointer.
- Example usage script (`steem_linked_list_examples.py`) demonstrating all core features.
- Test script (`steem_linked_list_post_test.py`) test all core features and optionally post results to the blockchain.
