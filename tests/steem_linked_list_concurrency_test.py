"""
steem_linked_list_concurrency_test.py
=====================================
Tests Optimistic Concurrency Control (OCC) by forcing simultaneous writes.
"""

import os
import sys
import time
import threading

# Add parent directory to sys.path so it can find steem_linked_list.py
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from steem import Steem
from steem_linked_list import SteemLinkedList

ACCOUNT = os.getenv("STEEM_ACCOUNT", "")
POSTING_KEY = os.getenv("STEEM_POSTING_KEY", "")
LIST_ID = f"occ_test_{int(time.time())}"
CUSTOM_JSON_ID = "linkedListOnSteem"


def main():
    if not POSTING_KEY:
        print("Error: STEEM_POSTING_KEY environment variable is not set.")
        print("Please set it in your environment or via a .env file to run this test.")
        sys.exit(1)

    print(f"Starting Concurrency (OCC) Test for list ID: {LIST_ID}")
    print("This test will force multiple threads to write simultaneously to test OCC.")
    
    steem = Steem(nodes=["https://api.steemit.com"], keys=[POSTING_KEY])
    ll_main = SteemLinkedList(steem, ACCOUNT, LIST_ID, CUSTOM_JSON_ID)
    
    print_lock = threading.Lock()

    def print_state(ll_instance, title):
        with print_lock:
            ll_instance.sync()
            print(f"\n{title}")
            for node in sorted(ll_instance.to_list(include_deleted=True, include_anchor=True), key=lambda n: n.seq):
                status = " [DELETED]" if node.payload.get("_deleted") else ""
                status += " [ANCHOR]" if node.payload.get("_is_anchor") else ""
                print(f"  Seq {node.seq}{status}: {node.payload}")

    print("\nCreating list anchor...")
    ll_main.append({"_is_anchor": True, "desc": "Anchor"})
    print_state(ll_main, "--- List State after Anchor Creation ---")
    
    # ---------------------------------------------------------
    # Phase 1: Concurrent safe_append
    # ---------------------------------------------------------
    print("\n--- Phase 1: Concurrent safe_append ---")
    print("Forcing two workers to append at the exact same time...")
    NUM_WORKERS = 2
    barrier_append = threading.Barrier(NUM_WORKERS)
    
    def append_worker(worker_id):
        # Each worker uses its own instance to mimic distributed apps
        ll_worker = SteemLinkedList(Steem(keys=[POSTING_KEY]), ACCOUNT, LIST_ID, CUSTOM_JSON_ID)
        ll_worker.rebuild_index()
        
        print(f"[Append Worker {worker_id}] Waiting at barrier...")
        barrier_append.wait()
        
        payload = {"worker": worker_id, "data": f"Concurrent append {worker_id}"}
        print(f"[Append Worker {worker_id}] Broadcasting safe_append...")
        try:
            node = ll_worker.safe_append(payload, max_retries=3, wait_time=15.0)
            print(f"[Append Worker {worker_id}] ✅ Confirmed at seq={node.seq} (trx_id: {node.trx_id})")
            print_state(ll_worker, f"--- List State after Append Worker {worker_id} ---")
        except Exception as e:
            print(f"[Append Worker {worker_id}] ❌ Failed: {e}")

    threads = []
    for i in range(NUM_WORKERS):
        t = threading.Thread(target=append_worker, args=(i + 1,))
        t.start()
        threads.append(t)
        
    for t in threads:
        t.join()

    print_state(ll_main, "--- List state after Phase 1 ---")

    # ---------------------------------------------------------
    # Phase 2: Concurrent safe_append vs safe_delete
    # ---------------------------------------------------------
    print("\n--- Phase 2: Concurrent safe_append vs safe_delete ---")
    print("One thread will append, another will delete seq=1 simultaneously.")
    barrier_mixed = threading.Barrier(2)
    
    def delete_worker():
        ll_worker = SteemLinkedList(Steem(keys=[POSTING_KEY]), ACCOUNT, LIST_ID, CUSTOM_JSON_ID)
        ll_worker.rebuild_index()
        print("[Delete Worker] Waiting at barrier...")
        barrier_mixed.wait()
        
        print("[Delete Worker] Broadcasting safe_delete(1)...")
        try:
            tombstone = ll_worker.safe_delete(1, max_retries=3, wait_time=15.0)
            print(f"[Delete Worker] ✅ Tombstone confirmed at seq={tombstone.seq} (trx_id: {tombstone.trx_id})")
            print_state(ll_worker, "--- List State after Phase 2 Delete Worker ---")
        except Exception as e:
            print(f"[Delete Worker] ❌ Failed: {e}")

    def append_worker_p2():
        ll_worker = SteemLinkedList(Steem(keys=[POSTING_KEY]), ACCOUNT, LIST_ID, CUSTOM_JSON_ID)
        ll_worker.rebuild_index()
        print("[Append Worker P2] Waiting at barrier...")
        barrier_mixed.wait()
        
        print("[Append Worker P2] Broadcasting safe_append...")
        try:
            node = ll_worker.safe_append({"data": "Another concurrent append"}, max_retries=3, wait_time=15.0)
            print(f"[Append Worker P2] ✅ Confirmed at seq={node.seq} (trx_id: {node.trx_id})")
            print_state(ll_worker, "--- List State after Phase 2 Append Worker ---")
        except Exception as e:
            print(f"[Append Worker P2] ❌ Failed: {e}")

    t1 = threading.Thread(target=delete_worker)
    t2 = threading.Thread(target=append_worker_p2)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    print_state(ll_main, "--- List state after Phase 2 ---")

    # ---------------------------------------------------------
    # Phase 3: Concurrent safe_delete
    # ---------------------------------------------------------
    print("\n--- Phase 3: Concurrent safe_delete ---")
    print("Two threads will try to delete seq=2 and seq=3 simultaneously.")
    print("This will cause one of the TOMBSTONES to be orphaned and retried.")
    barrier_delete = threading.Barrier(2)

    def delete_worker_p3(target_seq):
        ll_worker = SteemLinkedList(Steem(keys=[POSTING_KEY]), ACCOUNT, LIST_ID, CUSTOM_JSON_ID)
        ll_worker.rebuild_index()
        print(f"[Delete Worker P3, target={target_seq}] Waiting at barrier...")
        barrier_delete.wait()
        
        print(f"[Delete Worker P3, target={target_seq}] Broadcasting safe_delete({target_seq})...")
        try:
            tombstone = ll_worker.safe_delete(target_seq, max_retries=3, wait_time=15.0)
            print(f"[Delete Worker P3, target={target_seq}] ✅ Tombstone confirmed at seq={tombstone.seq} (trx_id: {tombstone.trx_id})")
            print_state(ll_worker, f"--- List State after Phase 3 Delete Worker (target={target_seq}) ---")
        except Exception as e:
            print(f"[Delete Worker P3, target={target_seq}] ❌ Failed: {e}")

    # At this point, seq=2 and seq=3 are the remaining active nodes
    t3 = threading.Thread(target=delete_worker_p3, args=(2,))
    t4 = threading.Thread(target=delete_worker_p3, args=(3,))
    t3.start()
    t4.start()
    t3.join()
    t4.join()

    # ---------------------------------------------------------
    # Final Verification
    # ---------------------------------------------------------
    print("\n--- Final Blockchain State ---")
    ll_main.rebuild_index()
    for node in sorted(ll_main.to_list(include_deleted=True, include_anchor=True), key=lambda n: n.seq):
        status = " [DELETED]" if node.payload.get("_deleted") else ""
        status += " [ANCHOR]" if node.payload.get("_is_anchor") else ""
        print(f"Seq {node.seq}{status}: {node.payload} (trx_id={node.trx_id})")

    expected_nodes = 1 + NUM_WORKERS + 1 + 1 + 2  # Anchor + 2 p1 appends + 1 p2 append + 1 p2 tombstone + 2 p3 tombstones
    if len(ll_main) == expected_nodes:
        print(f"\n✅ SUCCESS: Found exactly {expected_nodes} nodes. Collisions were successfully resolved!")
    else:
        print(f"\n❌ FAILURE: Expected {expected_nodes} nodes, but found {len(ll_main)}.")


if __name__ == "__main__":
    main()