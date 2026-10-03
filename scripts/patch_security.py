import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
BOT_PY = ROOT / "bot.py"

def patch_bot_health_server():
    with open(BOT_PY, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # Find def start_health_check_server()
    start_idx = None
    end_idx = None
    for idx, line in enumerate(lines):
        if line.startswith("def start_health_check_server():"):
            start_idx = idx
        if start_idx is not None and "port = int(os.getenv(\"PORT\", \"8080\"))" in line:
            end_idx = idx
            break

    if start_idx is not None and end_idx is not None:
        new_block = [
            "def start_health_check_server():\n",
            "    \"\"\"Starts a hardened HTTP server in a background thread to satisfy Render Web Service port checks.\"\"\"\n",
            "    import threading\n",
            "    from http.server import HTTPServer, BaseHTTPRequestHandler\n",
            "    from skills.security import get_security_headers, check_http_rate_limit\n",
            "\n",
            "    class HealthCheckHandler(BaseHTTPRequestHandler):\n",
            "        server_version = \"SuperMartOps/2026\"\n",
            "        sys_version = \"\"\n",
            "\n",
            "        def _send_headers(self, status_code: int, content_type: str = \"text/plain; charset=utf-8\"):\n",
            "            self.send_response(status_code)\n",
            "            self.send_header(\"Content-Type\", content_type)\n",
            "            for header, value in get_security_headers().items():\n",
            "                self.send_header(header, value)\n",
            "            self.end_headers()\n",
            "\n",
            "        def do_OPTIONS(self):\n",
            "            \"\"\"Handle CORS pre-flight checks cleanly.\"\"\"\n",
            "            self._send_headers(204)\n",
            "\n",
            "        def do_HEAD(self):\n",
            "            \"\"\"Handle HEAD requests.\"\"\"\n",
            "            client_ip = self.client_address[0] if self.client_address else \"127.0.0.1\"\n",
            "            if not check_http_rate_limit(client_ip):\n",
            "                self._send_headers(429)\n",
            "                return\n",
            "            if self.path in (\"/\", \"/health\", \"/healthz\"):\n",
            "                self._send_headers(200)\n",
            "            else:\n",
            "                self._send_headers(404)\n",
            "\n",
            "        def do_GET(self):\n",
            "            client_ip = self.client_address[0] if self.client_address else \"127.0.0.1\"\n",
            "            if not check_http_rate_limit(client_ip):\n",
            "                self._send_headers(429)\n",
            "                self.wfile.write(b\"429 Too Many Requests - Rate limit exceeded\\n\")\n",
            "                return\n",
            "\n",
            "            if self.path in (\"/\", \"/health\", \"/healthz\"):\n",
            "                self._send_headers(200)\n",
            "                self.wfile.write(b\"SuperMarket Ops Agent is healthy!\\n\")\n",
            "            else:\n",
            "                self._send_headers(404)\n",
            "                self.wfile.write(b\"404 Not Found\\n\")\n",
            "\n",
            "        def do_POST(self):\n",
            "            self._send_headers(405)\n",
            "            self.wfile.write(b\"405 Method Not Allowed\\n\")\n",
            "\n",
            "        def do_PUT(self):\n",
            "            self.do_POST()\n",
            "\n",
            "        def do_DELETE(self):\n",
            "            self.do_POST()\n",
            "\n",
            "        def log_message(self, format, *args):\n",
            "            return  # Suppress HTTP server access logs\n",
            "\n",
        ]
        lines[start_idx:end_idx] = new_block
        with open(BOT_PY, "w", encoding="utf-8") as f:
            f.writelines(lines)
        print("✅ Successfully patched HealthCheckHandler in bot.py!")
    else:
        print("⚠️ Could not find start_health_check_server markers.")
    
    old_code = '''def start_health_check_server():
    """Starts a minimal HTTP server in a background thread to satisfy Render Web Service port checks."""
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class HealthCheckHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/health", "/healthz"):
                self.send_response(200)
                self.send_header("Content-type", "text/plain; charset=utf-8")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(b"Bot is healthy!")
            else:
                self.send_response(404)
                self.send_header("Content-type", "text/plain; charset=utf-8")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(b"Not Found")

        def do_POST(self):
            self.send_response(405)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Method Not Allowed")

        def do_PUT(self):
            self.do_POST()

        def do_DELETE(self):
            self.do_POST()

        def log_message(self, format, *args):
            return  # Suppress HTTP server access logs'''

    new_code = '''def start_health_check_server():
    """Starts a hardened HTTP server in a background thread to satisfy Render Web Service port checks."""
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from skills.security import get_security_headers, check_http_rate_limit

    class HealthCheckHandler(BaseHTTPRequestHandler):
        server_version = "SuperMartOps/2026"
        sys_version = ""

        def _send_headers(self, status_code: int, content_type: str = "text/plain; charset=utf-8"):
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            for header, value in get_security_headers().items():
                self.send_header(header, value)
            self.end_headers()

        def do_OPTIONS(self):
            """Handle CORS pre-flight checks cleanly."""
            self._send_headers(204)

        def do_HEAD(self):
            """Handle HEAD requests."""
            client_ip = self.client_address[0] if self.client_address else "127.0.0.1"
            if not check_http_rate_limit(client_ip):
                self._send_headers(429)
                return
            if self.path in ("/", "/health", "/healthz"):
                self._send_headers(200)
            else:
                self._send_headers(404)

        def do_GET(self):
            client_ip = self.client_address[0] if self.client_address else "127.0.0.1"
            if not check_http_rate_limit(client_ip):
                self._send_headers(429)
                self.wfile.write(b"429 Too Many Requests - Rate limit exceeded\\n")
                return

            if self.path in ("/", "/health", "/healthz"):
                self._send_headers(200)
                self.wfile.write(b"SuperMarket Ops Agent is healthy!\\n")
            else:
                self._send_headers(404)
                self.wfile.write(b"404 Not Found\\n")

        def do_POST(self):
            self._send_headers(405)
            self.wfile.write(b"405 Method Not Allowed\\n")

        def do_PUT(self):
            self.do_POST()

        def do_DELETE(self):
            self.do_POST()

        def log_message(self, format, *args):
            return  # Suppress HTTP server access logs'''

    if old_code in content:
        content = content.replace(old_code, new_code, 1)
        BOT_PY.write_text(content, encoding="utf-8")
        print("✅ Successfully patched HealthCheckHandler in bot.py!")
    else:
        print("ℹ️ HealthCheckHandler already patched or modified.")

if __name__ == "__main__":
    patch_bot_health_server()
