# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.0.8-alpha] - 2026-03-28

### Added
- **Transaction Nonce**: Added a `_nonce` field to auto-created anchors and tombstone payloads to ensure unique transaction hashes when multiple instances broadcast identical automated payloads simultaneously.

### Changed
- **Documentation Updates**: Replaced module-level documentation references from `SteemLinkedList` to `linkedListOnSteem` to accurately reflect the newly restructured package name.
- **Increased Scan Depth**: Increased the default fallback block scan depth for newly discovered lists or missing history pointers from 30 to 100 blocks to better handle API indexing lag.

### Fixed
- **RPCError Handling in OCC**: Improved the Optimistic Concurrency Control loops in `safe_append` and `safe_delete` to gracefully catch and retry upon `RPCError` or network exceptions during broadcast (e.g., preventing complete crashes during Duplicate Transaction checks when multiple nodes auto-anchor).

## [0.0.7-alpha] - 2026-03-28

### Added
- **GitHub Installation Support**: Added `pyproject.toml` configuration enabling users to install the package directly from GitHub using `pip install git+https://github.com/remlaps/linkedListOnSteem.git`.
- **Proper Python Package Structure**: Reorganized the project into a proper Python package structure with `linkedListOnSteem/` as the main package directory.
- **Fixed Import Statements**: Updated all example and test scripts to use the correct import statements for the new package structure.

### Changed
- **Modern Packaging**: Migrated from a single-module structure to a proper Python package using `pyproject.toml` and setuptools.
- **Package Name**: The package is now imported as `import linkedListOnSteem` instead of `import steem_linked_list`.

## [0.0.6-alpha] - 2026-03-28

### Added
- **Transaction Initiation Time**: A `timestamp` field (ISO-8601 format) is now included in each node's on-chain envelope. This records the time an operation was first attempted, which is preserved across all retries in a `safe_append` or `safe_delete` loop.

## [0.0.5-alpha] - 2026-03-27

### Added
- **Deletion Guard**: `delete()` and `safe_delete()` now raise a `ValueError` if you attempt to delete a node that is already deleted or is a tombstone, preventing wasted blockchain operations.

### Changed
- **Smarter `safe_delete`**: The `safe_delete()` method now manages its own Optimistic Concurrency Control (OCC) loop. If two threads attempt to delete the same node, the second thread will now detect that the node has already been deleted upon retry and will abort gracefully instead of creating a redundant tombstone.
- **OCC Improvements**: Added exponential backoff with jitter to the `safe_append()` and `safe_delete()` retry loops to progressively reduce collision probabilities under high contention.
- **Robust Rollbacks**: Enhanced the fallback mechanism during concurrency collisions to perform a full `rebuild_index()`, guaranteeing sequence number alignment against complex multi-node forks.
- **Concurrent Deletes**: `safe_delete()` now correctly treats the operation as a success (returning the existing tombstone) if it detects that a concurrent writer already successfully deleted the target node while waiting to retry.

## [0.0.4-alpha] - 2026-03-27

### Changed
- **Switched to Block Scanning for Concurrency**: `_is_orphaned()` no longer uses `get_account_history`. It now scans recent blocks directly to detect forks. This makes concurrency control resilient to history API lag and fully compatible with multi-author lists.
- **Improved Indexing Resilience**: `rebuild_index()` and `sync()` now use a hybrid tail-discovery strategy. They combine a fast `get_account_history` lookup with a capped block scan to find the true tail, even if the history API is lagging.
- **Verify finality**: Switched default setting back to true for ```wait_for_irreversible```.

### Fixed
- Fixed a race condition where `safe_append` could fail if `wait_for_irreversible=True` pushed a node's block outside the fixed block-scanning window. The scan depth is now calculated dynamically.
- Fixed a bug where `rebuild_index` could fail to find recently appended nodes due to API history lag, causing `KeyError` on subsequent operations.
- Capped the dynamic block scan depth to a reasonable limit (`MAX_SCAN_LIMIT`) to prevent the library from attempting to scan millions of blocks on very old lists, which would freeze the application.

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
