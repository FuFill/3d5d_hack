# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Run callables on the Qt main thread and wait for the result.

Scene, settings and slicing may only be touched from the main thread, while the
HTTP server and the agent run in worker threads.
"""

import threading


class MainThreadTimeout(Exception):
    pass


def run_on_main(app, func, *args, **kwargs):
    timeout = kwargs.pop("_timeout", 60)
    if threading.current_thread() is threading.main_thread():
        return func(*args, **kwargs)

    done = threading.Event()
    box = {}

    def call():
        try:
            box["value"] = func(*args, **kwargs)
        except BaseException as e:  # Re-raised in the calling thread.
            box["error"] = e
        finally:
            done.set()

    app.callLater(call)
    if not done.wait(timeout):
        raise MainThreadTimeout("Slicer main thread did not respond in %d s" % timeout)
    if "error" in box:
        raise box["error"]
    return box.get("value")
