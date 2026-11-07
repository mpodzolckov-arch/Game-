import asyncio
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

LOG = ["run.py started"]


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = "\n".join(LOG).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


port = int(os.environ.get("PORT", "10000"))
threading.Thread(target=HTTPServer(("0.0.0.0", port), H).serve_forever, daemon=True).start()
os.environ["PORT"] = "10001"

try:
    LOG.append("importing bot")
    import bot
    LOG.append("bot imported, starting")
    asyncio.run(bot.main())
except BaseException:
    LOG.append(traceback.format_exc())

while True:
    time.sleep(60)
