"""
L3: Regulation Interpreter

Main entry point for the legal reasoning layer.
"""
from .hybrid_retrieval import search_regulations
from .legal_reasoning import generate_legal_analysis

async def interpret_regulation(event, suspicion_score):
    """
    Orchestrates the regulation interpretation process.
    """
    # 1. Hybrid Retrieval
    print("L3: Performing hybrid retrieval from CockroachDB (BM25 + vector).")
    regulation_chunks = search_regulations(event)

    # 2. Legal Reasoning
    print("L3: Generating legal reasoning and sub-scores.")
    analysis = generate_legal_analysis(event, regulation_chunks)

    # TODO: Based on the final score, route to L4 (auto-file) or L5 (human review)
    final_score = analysis['final_score']
    print(f"L3: Final score is {final_score}.")

    if final_score >= 0.9:
        # Route to L4
        pass
    elif final_score >= 0.5:
        # Route to L5
        pass
    else:
        # Route to L5 with priority escalation
        pass
