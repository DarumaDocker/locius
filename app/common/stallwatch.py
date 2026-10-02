"""Event-loop stall watchdog: when the asyncio loop does not tick for STALL_S seconds (a blocking call in async code),
write the loop thread's stack to <data>/loop_stalls.log so the culprit can be found. While the loop is blocked the
HTTP server cannot accept connections, and Sentinel answers the UI with 503 "runtime unavailable"."""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import traceback

STALL_S = float(os.environ.get("LOOP_STALL_SECONDS", "3"))


def start(data_dir: str, name: str = "runtime") -> None:
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    beat = {"t": time.monotonic()}
    path = os.path.join(data_dir, "loop_stalls.log")

    async def heartbeat():
        while True:
            beat["t"] = time.monotonic()
            await asyncio.sleep(0.5)

    loop.create_task(heartbeat())

    def watch():
        reported = 0.0
        while True:
            time.sleep(1)
            lag = time.monotonic() - beat["t"]
            if lag < STALL_S or beat["t"] == reported:
                continue
            reported = beat["t"]   # one report per stall
            frame = sys._current_frames().get(loop_thread)
            stack = "".join(traceback.format_stack(frame)[-14:]) if frame else "(no frame)"
            line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {name} event loop blocked {lag:.1f}s+\n{stack}\n"
            try:
                with open(path, "a") as f:
                    f.write(line)
                if os.path.getsize(path) > 2_000_000:   # keep it small
                    os.replace(path, path + ".1")
            except OSError:
                pass
            print(line[:1500], file=sys.stderr, flush=True)

    threading.Thread(target=watch, name="stallwatch", daemon=True).start()
