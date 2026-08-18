"""
L7: Regulatory Watch

A scheduled job that monitors regulatory sources for changes.
"""
import hashlib
import os
import json

from aws import db, store as crdb_store
from L1_orchestrator.regulation_hash import update_hash
from .scraper import scrape_sources, set_last_processed_url
from .classifier import classify_changes
from .indexer import update_search_index, check_document_exists
from .s3_storage import upload_to_s3

async def regulatory_watch_job():
    """
    The main function for the 6-hour job.

    On AWS this runs as a Lambda (or Fargate task) on an EventBridge schedule.
    All state lives in CockroachDB, so the job is stateless and any invocation
    can pick up where the last one stopped.
    """
    print("L7: Starting regulatory watch job...")
    
    # 1. Scrape sources for new/updated documents
    new_documents = scrape_sources()
    
    if not new_documents:
        print("L7: No new documents found. Exiting.")
        return
        
    processed_hashes = []

    for doc in reversed(new_documents): # Process oldest to newest
        print(f"\nL7: Processing document: {doc['title']}")
        
        # 2. Check if already ingested to avoid re-classifying and re-chunking
        if check_document_exists(doc['title']):
            print(f"L7: Document '{doc['title']}' is already in the corpus. Skipping.")
            continue
            
        # 3. Classify change and generate summary
        change_info = classify_changes(doc['text'])
        
        # 4. Check Relevance Filter
        if not change_info.get("is_relevant", True):
            print(f"L7: Document '{doc['title']}' is marked IRRELEVANT (e.g. ATM maintenance). Skipping ingestion.")
            continue
            
        print(f"L7: Document '{doc['title']}' is RELEVANT for KYC/AML. Proceeding.")
        
        # 5. Chunk, embed, and commit to the CockroachDB corpus
        update_search_index(doc, change_info)

        # 6. Archive the raw circular to S3
        upload_to_s3(doc)

        processed_hashes.append(
            hashlib.sha256(doc.get("text", "").encode("utf-8")).hexdigest()
        )

    # 7. Close the loop. A new composite hash invalidates every cached verdict
    #    that was decided under the old regulation, so L1 can no longer
    #    short-circuit against reasoning that is now out of date.
    if processed_hashes:
        composite = hashlib.sha256("".join(sorted(processed_hashes)).encode()).hexdigest()
        update_hash(composite, {"rbi-notifications": {"documents": len(processed_hashes)}})
        print(f"L7: Regulation composite hash updated to {composite[:16]}...")

    # 8. Advance the watch cursor so these are not scraped again
    newest_url = new_documents[0]['url']
    set_last_processed_url(newest_url)
    print(f"\nL7: State updated. Last processed URL is now: {newest_url}")

    print("L7: Regulatory watch job finished.")

if __name__ == "__main__":
    import asyncio
    asyncio.run(regulatory_watch_job())
