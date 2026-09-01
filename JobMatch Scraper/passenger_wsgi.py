"""
passenger_wsgi.py — cPanel "Setup Python App" entry point.

cPanel/Passenger imports the `application` callable from this file. We make sure the
app folder is on the path and is the working directory (so relative files like
idf.json / careers_us.md / sponsors.txt resolve), then expose the Flask app.

Set these in the Python App's "Environment variables" section (cPanel):
    PG_DSN    = host=127.0.0.1 port=5432 dbname=<db> user=<user> password=<pw>
    APP_SECRET = <a long random string; required, see web.py>
    APP_SECRET   = <any long random string>   (optional; signs the login cookie)
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
os.chdir(_HERE)

from web import app as application   # noqa: E402  (Passenger looks for `application`)
