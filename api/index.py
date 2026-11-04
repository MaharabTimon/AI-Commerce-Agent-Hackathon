import sys
import os
from urllib.parse import parse_qs, urlencode

# Add project root directory to sys.path so all application modules are found
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import messenger

class VercelPathFixer:
    """
    WSGI middleware to ensure the original requested path (e.g. /webhook, /setup-telegram)
    is cleanly delivered to Flask regardless of Vercel serverless rewrites.
    """
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        query_string = environ.get('QUERY_STRING', '')
        qs = parse_qs(query_string, keep_blank_values=True)
        if '__path__' in qs:
            original_path = qs.pop('__path__')[0]
            if not original_path.startswith('/'):
                original_path = '/' + original_path
            environ['PATH_INFO'] = original_path
            environ['QUERY_STRING'] = urlencode(qs, doseq=True)
        elif environ.get('PATH_INFO') in ('/api/index.py', '/api/index', '/api'):
            environ['PATH_INFO'] = '/'

        return self.wsgi_app(environ, start_response)

app = messenger.app
app.wsgi_app = VercelPathFixer(app.wsgi_app)
application = app
handler = app
