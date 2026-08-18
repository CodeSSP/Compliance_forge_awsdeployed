import json
from L3_regulation_interpreter.llm_client import chat_json

SYSTEM_PROMPT = """
You are an expert regulatory analyst for the Reserve Bank of India (RBI) and FIU-IND.
You are given the text of a newly published regulatory notification or circular.
Your job is to read it and output a strict JSON object classifying the update.

Determine if this is a:
1. "new" regulation (entirely new rules)
2. "update" (amends or adds to an existing circular)
3. "replacement" (completely supersedes and replaces an older circular)

If it is an "update" or "replacement", you MUST identify the exact Circular ID, Name, or Document ID of the older document that it is modifying/replacing.

RELEVANCE FILTER:
You must also determine if this notification is relevant to Anti-Money Laundering (AML), Know Your Customer (KYC), suspicious transaction reporting (STR), financial fraud, or transaction monitoring. 
If it is about something irrelevant (e.g., ATM maintenance, public holidays, sovereign gold bonds, IT hardware upgrades), set "is_relevant" to false.

JSON OUTPUT FORMAT:
{
  "action": "new" | "update" | "replacement",
  "target_circular_id": "Exact name/ID of the old document if action is update or replacement, else null",
  "summary": "A 2-sentence plain English summary of what this new regulation does.",
  "is_relevant": true | false
}
"""

def classify_changes(document_text: str) -> dict:
    """
    Uses the LLM client to classify the change.
    """
    print("L7: Classifying document change type and summarizing...")
    
    snippet = document_text[:4000]
    
    try:
        change_info = chat_json(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=f"Here is the new RBI notification:\n\n{snippet}"
        )
        return change_info
    except Exception as e:
        print(f"L7: Classifier failed: {e}")
        return {
            "action": "new",
            "target_circular_id": None,
            "summary": "Failed to classify document.",
            "is_relevant": False
        }
