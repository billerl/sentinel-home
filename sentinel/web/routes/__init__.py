"""
The web pages and API, one module per area. Each has register(app, ctx): it adds that area's routes to the
Flask app, closing over the shared WebContext (sentinel/web/context.py). Endpoint names are the plain function
names (no blueprints), so url_for("live_page") and the templates' links work across areas.
"""
