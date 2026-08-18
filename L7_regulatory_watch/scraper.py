"""
L7: Regulatory Scraper

Scrapes regulatory websites (RBI) for new circulars.
"""
import requests
from bs4 import BeautifulSoup
import re
import json
import os

from aws import db, store as crdb_store

# Retained for local-file fallback only. CockroachDB watch_state is the source
# of truth, which is what lets the scraper run as a stateless Lambda.
STATE_FILE = "L7_regulatory_watch/l7_state.json"
SOURCE_KEY = "rbi-notifications"


def _get_last_processed_url():
    if db.available():
        row = crdb_store.get_watch_state(SOURCE_KEY)
        return row["last_processed_url"] if row else None

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                data = json.load(f)
                return data.get("last_processed_url")
        except Exception:
            return None
    return None


def set_last_processed_url(url: str):
    """Called by L7 main after a successful pass."""
    if db.available():
        crdb_store.set_watch_state(SOURCE_KEY, url)
        return
    with open(STATE_FILE, 'w') as f:
        json.dump({"last_processed_url": url}, f)

def scrape_sources():
    """
    Scrapes the live RBI notifications page. Fetches all notifications 
    newer than the last processed URL.
    """
    print("L7: Scraping live RBI website for new documents...")
    
    url = "https://www.rbi.org.in/scripts/NotificationUser.aspx"
    last_processed_url = _get_last_processed_url()
    
    try:
        response = requests.get(url, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        all_links = soup.select("a.link2")
        notification_links = [a for a in all_links if "NotificationUser.aspx?Id=" in a.get("href", "")]
        
        if not notification_links:
            print("L7: Could not find any notification links on the RBI page.")
            return []
            
        new_documents = []
        
        for link in notification_links:
            full_url = "https://www.rbi.org.in/scripts/" + link.get("href")
            title = link.text.strip()
            
            # Stop if we hit the one we processed last time
            if full_url == last_processed_url:
                print(f"L7: Reached previously processed URL. Stopping scrape.")
                break
                
            print(f"L7: Found new notification: '{title}' at {full_url}")
            
            detail_response = requests.get(full_url, timeout=10)
            detail_soup = BeautifulSoup(detail_response.text, 'html.parser')
            
            text_content = ""
            content_table = detail_soup.find("table", class_="tablebg")
            if content_table:
                text_content = content_table.get_text(separator="\n", strip=True)
            else:
                raw_text = detail_soup.get_text(separator="\n", strip=True)
                if "Reserve Bank of India" in raw_text:
                    text_content = raw_text[raw_text.find("Reserve Bank of India"):]
                else:
                    text_content = raw_text
                    
            text_content = re.sub(r'\n+', '\n', text_content)
            
            new_documents.append({
                "type": "text",
                "url": full_url,
                "title": title,
                "text": text_content
            })
            
        return new_documents
        
    except Exception as e:
        print(f"L7: Scraper failed: {e}")
        return []

if __name__ == "__main__":
    docs = scrape_sources()
    if docs:
        print(f"\nSuccessfully scraped {len(docs)} documents.")
        print(f"Title: {docs[0]['title']}")
        print(f"URL: {docs[0]['url']}")
