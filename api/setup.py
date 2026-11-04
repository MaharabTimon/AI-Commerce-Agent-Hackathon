import sys
import os

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import messenger

class ForceSetupPathMiddleware:
    """
    WSGI middleware that guarantees the incoming request is routed to /setup-telegram
    in Flask for one-click Telegram webhook registration.
    """
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        environ['PATH_INFO'] = '/setup-telegram'
        return self.wsgi_app(environ, start_response)

app = messenger.app
app.wsgi_app = ForceSetupPathMiddleware(app.wsgi_app)
application = app
handler = app
