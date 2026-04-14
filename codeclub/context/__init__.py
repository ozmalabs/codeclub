"""
codeclub.context — Dynamic context assembly for LLM conversations.

Instead of sending entire conversation history every turn, this module:
1. Classifies request intent (what kind of context is needed)
2. Assembles minimal context from an indexed session store
3. Optionally uplifts vague specs before routing
4. Routes to the best-fit model based on context size

Use the proxy server to intercept any OpenAI-compatible API:
    python -m codeclub.context.proxy --upstream http://localhost:11434/v1
"""
