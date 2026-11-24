"""Every tool an agent can call, one class each. See base.py for how a tool is defined, and
app/agents/registry.py for which agent gets which tools.

Phone agents also call the bridge's tools, defined in app/bridge/tools.py:
interact, type_and_submit, press_key, swipe, open_app, open_link, wait_for, set_setting,
set_location, grant, request_screenshot.

    screen.py        note
    device.py        adb_command, install_app
    subagent.py      report, ask_for_password_or_code
    verifier.py      verdict
    orchestrator.py  set_plan, run_step, verify_step, ask_user, finish
    gate.py          answer, delegate
    memory.py        remember
"""
