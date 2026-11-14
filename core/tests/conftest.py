import asyncio
import os
import sys

# No daily update check from test apps (it would reach GitHub after 120 s); test_updates.py turns it on per app.
os.environ.setdefault("ET_UPDATE_CHECK", "off")

# The MQTT client (paho under aiomqtt) needs add_reader(), which Windows' default Proactor loop doesn't have.
# The app sets the same policy in __main__; Linux/Docker is unaffected.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_cmd_gap():
    """Some scenario helpers set the controller's command gap to 0 for the whole module (Night, the lab, the soak):
    put it back after each test, so a later test with a real-time app loop isn't flooded with bed commands."""
    import embertide.engine.controller as ctlmod
    keep = ctlmod.MIN_CMD_GAP_S
    yield
    ctlmod.MIN_CMD_GAP_S = keep
