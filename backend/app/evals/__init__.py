"""Offline evaluation harness. Runs with no database: its own gateway (recorded LLM responses), audit calls captured in memory.

Run:  python -m app.evals.run            (replay: re-scores recorded model output, makes zero API calls)
      python -m app.evals.run --live     (calls Gemini once for any prompt not yet recorded, then records it)
"""
