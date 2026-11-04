import sys
import os

# Ensure the root directory is on the path
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import messenger

class ForceWebhookPathMiddleware:
    """
    WSGI middleware that guarantees the incoming request is routed to /webhook
    in Flask, ensuring verify_webhook (GET) and handle_webhook_events (POST) execute cleanly.
    """
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        environ['PATH_INFO'] = '/webhook'
        return self.wsgi_app(environ, start_response)

app = messenger.app
app.wsgi_app = ForceWebhookPathMiddleware(app.wsgi_app)
application = app
handler = app
