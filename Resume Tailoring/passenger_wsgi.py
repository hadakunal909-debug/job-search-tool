"""
passenger_wsgi.py — cPanel "Setup Python App" entry point for Resume Brain.

cPanel/Passenger imports the `application` callable from this file. We put the app folder
on the path and make it the working directory (so data/ and templates resolve), then
expose the Flask app.

Set in the Python App's "Environment variables" (cPanel):
    APP_SECRET           = <any long random string>   (signs the session cookie)
    GEMINI_API_KEY       = <optional; else each user pastes their own key>
    SESSION_COOKIE_SECURE = 1                          (production is https)
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
os.chdir(_HERE)

from web import app as application   # noqa: E402  (Passenger looks for `application`)
