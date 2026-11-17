"""Error handlers: every failure becomes a page that says what happened and what to do next."""
from __future__ import annotations

from flask import Flask, render_template, request, url_for
from werkzeug.exceptions import RequestEntityTooLarge

from . import services as S
from .database import StorageError

ERROR_PAGE = "pages/errors/error.html"


def _page(code: int, title: str, message: str, actions: list[tuple[str, str]] | None = None):
    return render_template(ERROR_PAGE, code=code, title=title, message=message, actions=actions or []), code


def _home() -> list[tuple[str, str]]:
    return [("Back to dashboard", url_for("dashboard.index"))]


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(S.Forbidden)
    def forbidden(e):
        return _page(403, "You can’t do that here", str(e), _home())

    @app.errorhandler(S.NotFound)
    def not_found(e):
        return _page(404, "Not found", str(e), _home())

    @app.errorhandler(S.ValidationFailed)
    def invalid(e):
        return _page(400, "Something about that request wasn’t right", str(e), _home())

    @app.errorhandler(404)
    def http_404(e):
        return _page(404, "Page not found", "That address doesn’t match any page. Check the link or use the menu.",
                     [("Go to dashboard", url_for("dashboard.index"))])

    @app.errorhandler(400)
    def http_400(e):
        return _page(400, "Something about that request wasn’t right", e.description, _home())

    @app.errorhandler(StorageError)
    def storage(e):
        return _page(503, "Your change was not saved",
                     f"{e} Nothing was changed: your data is as it was before. You can try again in a moment.",
                     [("Try again", request.referrer or url_for("dashboard.index")),
                      ("Storage settings", url_for("settings.index") + "#storage")])

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(e):
        return _page(413, "That file is too large", "Files up to 2 MB can be imported. Export a shorter date "
                     "range from your bank and try again.", [("Back to import", url_for("imports.start"))])

    @app.errorhandler(S.EditConflict)
    def conflict(e):
        return render_template("pages/errors/conflict.html", e=e), 409
