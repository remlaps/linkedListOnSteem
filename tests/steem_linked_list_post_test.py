"""
steem_linked_list_post_test.py
==============================
Executes all functions of the SteemLinkedList library, logs the results,
and posts the execution report as a top-level post on the Steem blockchain.

Setup
-----
    pip install steem
"""

import os
import sys
import time

# Add parent directory to sys.path so it can find steem_linked_list.py
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from steem import Steem
from steem_linked_list import SteemLinkedList, NodePointer, __version__

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ACCOUNT = os.getenv("STEEM_ACCOUNT", "")
POSTING_KEY = os.getenv("STEEM_POSTING_KEY", "")
LOGDIR = "logs"
LIST_ID = f"test_run_{int(time.time())}"           # Unique list ID for this run
CUSTOM_JSON_ID = "linkedListOnSteem"               # Stable ID for the data protocol
DRY_RUN = True                                     # Skip posting and save to file instead


def main():
    if not POSTING_KEY:
        print("Error: STEEM_POSTING_KEY environment variable is not set.")
        print("Please set it in your environment or via a .env file to run this test.")
        return

    print(f"Starting test run for list ID: {LIST_ID}...")
    
    steem = Steem(
        nodes=["https://api.steemit.com"],
        keys=[POSTING_KEY]
    )

    ll = SteemLinkedList(
        steem_instance=steem,
        account=ACCOUNT,
        ll_id=LIST_ID,
        custom_json_id=CUSTOM_JSON_ID,
        use_active_key=False,
        wait_for_irreversible=True
    )

    markdown_report = []

    def log(msg, md_only=False):
        if not md_only:
            print(msg)
        markdown_report.append(msg)

    def log_header(title):
        print(f"\n--- {title} ---")
        markdown_report.append(f"\n## {title}")

    # Initialize Markdown Report
    log(f"# SteemLinkedList Execution Report", md_only=True)
    log(f"**List ID:** `{LIST_ID}`\n", md_only=True)
    log("This is an automated post demonstrating the capabilities of the "
        "`steem_linked_list` data structure on the Steem blockchain.\n", md_only=True)

    try:
        # 1. Append Nodes
        log_header("1. Append Nodes")
        n1 = ll.append({"action": "append", "value": "A", "desc": "First node"})
        log(f"* Appended Node A: `block={n1.block_num} trx_id={n1.trx_id} trx_num={n1.trx_num}`")
        n2 = ll.append({"action": "append", "value": "B", "desc": "Second node"})
        log(f"* Appended Node B: `block={n2.block_num} trx_id={n2.trx_id} trx_num={n2.trx_num}`")
        n3 = ll.append({"action": "append", "value": "C", "desc": "Third node"})
        log(f"* Appended Node C: `block={n3.block_num} trx_id={n3.trx_id} trx_num={n3.trx_num}`")
        n4 = ll.append({"action": "append", "value": "D", "desc": "Fourth node"})
        log(f"* Appended Node D: `block={n4.block_num} trx_id={n4.trx_id} trx_num={n4.trx_num}`")

        # Wait for block confirmation before querying account history
        log("\n*Waiting 90 seconds for blocks to confirm...*")
        time.sleep(90)

        # 2. Rebuild Index & Sync
        log_header("2. Index Rebuild & Sync")
        ll_fresh = SteemLinkedList(steem, ACCOUNT, LIST_ID, CUSTOM_JSON_ID)
        rebuild_count = ll_fresh.rebuild_index()
        log(f"* `rebuild_index()` successfully loaded **{rebuild_count}** nodes via targeted prev-chain walking.")
        
        sync_count = ll_fresh.sync()
        log(f"* `sync()` found **{sync_count}** new nodes (incremental sync test).")

        # 3. Traversal and Searches
        log_header("3. Traversal & Searches")
        forward_vals = [n.payload.get("value") for n in ll_fresh]
        log(f"* **Forward iteration:** `{forward_vals}`")
        
        tail_seq = ll_fresh.tail().seq if ll_fresh.tail() else 0
        reverse_vals = [n.payload.get("value") for n in ll_fresh.walk(start_seq=tail_seq, reverse=True)]
        log(f"* **Reverse iteration:** `{reverse_vals}`")
        
        head_val = ll_fresh.head().payload.get("value") if ll_fresh.head() else "None"
        log(f"* **Head node value:** `{head_val}`")
        
        found_node = ll_fresh.find(lambda p: p.get("value") == "B")
        log(f"* **Search:** Payload with value 'B' found at seq=`{found_node.seq if found_node else 'Not Found'}`")

        # 4. Delete (Tombstone)
        log_header("4. Delete Node by Absolute Sequence")
        # Initial active list: ['A', 'B', 'C', 'D']
        node_to_delete = ll_fresh.get(2) # Absolute seq=2 is 'B'
        tombstone = ll_fresh.delete(2)
        log(f"* Soft-deleted node at seq=2 (value: '{node_to_delete.payload.get('value')}'). Tombstone trx_id=`{tombstone.trx_id}`.")

        # 4a. Delete Node by Active Index
        log_header("4a. Delete Node by Active Index")
        # Active list is now ['A', 'C', 'D']. Active index 1 is 'C'.
        node_to_delete_active = ll_fresh.get_active(1)
        tombstone2 = ll_fresh.delete_active(1)
        log(f"* Soft-deleted node at active_index=1 (value: '{node_to_delete_active.payload.get('value')}'). Tombstone trx_id=`{tombstone2.trx_id}`.")
        
        active_vals = [n.payload.get("value") for n in ll_fresh]
        all_vals = [n.payload.get("value", "[DELETED]") for n in ll_fresh.to_list(include_deleted=True)]
        log(f"* Active items visible during normal iteration: `{active_vals}`")
        log(f"* All items including tombstones: `{all_vals}`")
        
        get_2 = ll_fresh.get(2).payload.get("value")
        get_active_1 = ll_fresh.get_active(1).payload.get("value")
        log(f"* `get(2)` accesses absolute seq=2: `{get_2}` (Now deleted)")
        log(f"* `get_active(1)` accesses the 2nd *remaining* active node: `{get_active_1}`")

        # 5. Fetch by Pointer
        log_header("5. Fetch Node Directly by Pointer")
        ptr = NodePointer(block=n2.block_num, trx_id=n2.trx_id, trx_num=n2.trx_num)
        fetched = ll_fresh.fetch_node_by_pointer(ptr)
        log(f"* Fetched node directly from blockchain via pointer `(block={ptr.block}, trx={ptr.trx_id}, trx_num={ptr.trx_num})`: `{fetched.payload if fetched else 'None'}`")

        # 6. Export / Import Index
        log_header("6. Export & Import Index")
        exported_data = ll_fresh.export_index()
        log(f"* Exported local index into a standard JSON list (length: **{len(exported_data)}**)")
        
        ll_cached = SteemLinkedList(steem, ACCOUNT, LIST_ID, CUSTOM_JSON_ID)
        ll_cached.import_index(exported_data)
        log(f"* Imported index into a new list instance without querying the blockchain. Verified head value: `{ll_cached.head().payload.get('value') if ll_cached.head() else 'None'}`")

        # 7. Final Linked List State
        log_header("7. Final Linked List State")
        log("Here are the block and transaction numbers for the final linked list:")
        for node in ll_cached.to_list(include_deleted=True, include_anchor=True):
            val = node.payload.get("value", node.payload.get("desc", "[DELETED]"))
            log(f"* Seq {node.seq}: block=`{node.block_num}`, trx_id=`{node.trx_id}`, trx_num=`{node.trx_num}` (value: {val})")

        # -------------------------------------------------------------------
        # Post Results to Steem
        # -------------------------------------------------------------------
        log_header("Publishing Report")

        post_title = f"SteemLinkedList v{__version__} Automated Test Run ({LIST_ID})"
        post_body = "\n".join(markdown_report)

        if DRY_RUN:
            print("DRY_RUN enabled: Skipping Steem post broadcast.")
            filename = f"{LOGDIR}/{LIST_ID}_report.md"
            os.makedirs(LOGDIR, exist_ok=True)
            with open(filename, "w", encoding="utf-8") as f:
                f.write(post_title + "\n")
                f.write("=" * len(post_title) + "\n\n")
                f.write(post_body)
            print(f"\nSuccess! The test report has been saved locally to {filename}.")
        else:
            print("Broadcasting Markdown payload to the Steem blockchain as a top-level post...")
            steem.commit.post(
                title=post_title,
                body=post_body,
                author=ACCOUNT,
                tags=["programming", "steem", "python", "testing", "development"]
            )
            print("\nSuccess! The test report has been posted to your Steem account.")

    except Exception as e:
        print(f"\nAn error occurred during execution: {e}")

if __name__ == "__main__":
    main()
