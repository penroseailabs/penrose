"""The loops that run the agents.

    runner.py        one run: the gate, then the orchestrator; model calls, the phone's lease
    orchestrator.py  plan, run a step, read its report, verify, ask, finish
    phone.py         a phone agent's session: look, think, act (subagent, verifier)
    memory.py        long-term memory: recall for each agent, learning after each run
"""
