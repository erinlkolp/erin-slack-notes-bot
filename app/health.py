import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from .config import HEALTH_CHECK_PORT
from .database import get_db_connection

logger = logging.getLogger(__name__)


def check_health():
    """Check database connectivity. Returns (is_healthy: bool, message: str)."""
    connection = None
    try:
        connection = get_db_connection()
        if connection is None:
            return False, "database connection failed"
        return True, "ok"
    except Exception as e:
        return False, str(e)
    finally:
        if connection:
            try:
                connection.close()
            except Exception:
                pass


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler exposing a /healthz endpoint."""

    def do_GET(self):
        if self.path == "/healthz":
            healthy, message = check_health()
            status = 200 if healthy else 503
            body = json.dumps(
                {"status": "healthy" if healthy else "unhealthy", "message": message}
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress default request logging to keep output clean.
        pass


def start_health_check_server():
    """Start the health check HTTP server on a daemon thread."""
    server = HTTPServer(("0.0.0.0", HEALTH_CHECK_PORT), HealthCheckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health check server started on port {HEALTH_CHECK_PORT}")
    return server
