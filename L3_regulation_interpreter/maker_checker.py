import json
import requests

def run_maker_checker(tx, l3_analysis, model="qwen2.5:72b"):
    """
    Acts as an Explainability Agent that validates the primary L3 LLM's decision
    and provides a short, plain-language paragraph explaining the violation.
    """
    
    # Extract facts to present to the Maker-Checker
    verdict = l3_analysis.get("verdict", "unknown")
    confidence = l3_analysis.get("confidence", 0.0)
    citation = l3_analysis.get("citation", "No citation provided.")
    explanation = l3_analysis.get("explanation", "No explanation provided.")
    
    prompt = f"""
    You are an expert compliance Maker-Checker auditor for an autonomous financial regulatory pipeline.
    The primary LLM has analyzed a transaction and determined its status based on a specific regulation.
    
    TRANSACTION FACTS:
    {json.dumps(tx, indent=2)}
    
    PRIMARY LLM VERDICT: {verdict}
    PRIMARY LLM CONFIDENCE: {confidence}
    CITED REGULATION: {citation}
    PRIMARY LLM REASONING: {explanation}
    
    YOUR TASK:
    Write a short, clear paragraph (3-4 sentences maximum) validating this decision and explaining exactly WHY this transaction violates the cited regulation.
    - Avoid technical jargon.
    - Write it so a human analyst can immediately understand the core issue.
    - Do NOT output any JSON, just the paragraph text.
    """
    
    try:
        from aws import model_gateway
        print(f"Maker-Checker: Calling {model_gateway.describe()}...")
        explanation_paragraph = model_gateway.generate(
            prompt, max_tokens=512, temperature=0.0, timeout=45
        ).strip()
        print("Maker-Checker: Successfully validated transaction.")
        return explanation_paragraph

    except (requests.exceptions.RequestException, OSError) as e:
        print(f"Maker-Checker model error: {e}")
        return "Maker-Checker validation is currently offline (model endpoint not reachable)."
    except Exception as e:
        print(f"Maker-Checker unexpected error: {e}")
        return "Maker-Checker validation encountered an unexpected error."
