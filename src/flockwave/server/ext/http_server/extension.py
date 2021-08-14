"""Extension that extends the Skybrush server with an HTTP server listening
on a specific port.

This server can then be used by other extensions to implement HTTP-specific
functionality such as a web-based debug page (`flockwave.ext.debug`) or a
Socket.IO-based channel.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from heapq import heapify, heappush
from hypercorn.config import Config as HyperConfig
from hypercorn.trio import serve
from quart import Blueprint, abort, redirect, request, url_for
from quart_trio import QuartTrio
from trio import current_time, sleep
from typing import Iterable, Optional

import logging

from flockwave.networking import format_socket_address
from flockwave.server.ports import get_port_number_for_service
from flockwave.server.types import Disposer

from .routing import RoutingMiddleware

__all__ = ("exports", "load", "unload", "schema")

PACKAGE_NAME = __name__.rpartition(".")[0]

############################################################################

proposed_index_pages = []
quart_app = None

############################################################################


@dataclass(order=True)
class ProposedIndexPage:
    priority: int = 0
    route: str = "/"


############################################################################


def create_app():
    """Creates the ASGI web application provided by this extension."""

    global quart_app

    if quart_app is not None:
        raise RuntimeError("App is already created")

    quart_app = app = QuartTrio(PACKAGE_NAME)

    # Set up the default index route
    @app.route("/")
    async def index():
        index_url = get_index_url()
        if index_url:
            if request.query_string:
                index_url += "?" + request.query_string.decode("utf-8")
            return redirect(index_url)
        else:
            abort(404)

    router = RoutingMiddleware()
    router.add(app, scopes=("http", "websocket"))
    return router


def get_index_url() -> Optional[str]:
    """Get the index URL with the highest priority that was proposed by
    other extensions.

    Returns:
        the URL of the best proposed index page or ``None`` if no index page has
        been proposed
    """
    global proposed_index_pages
    if proposed_index_pages:
        return url_for(proposed_index_pages[0].route)
    else:
        return None


def mount(
    app, *, path: str, scopes: Optional[Iterable[str]] = None, priority: int = 0
) -> Optional[Disposer]:
    """Mounts the given ASGI web application or Quart blueprint at the
    given path.

    Parameters:
        app: the ASGI web application or Quart blueprint to mount
        path: the path to mount the web application or blueprint at
        scopes: when the app is an ASGI web application, specifies the ASGI
            scopes that the web application should respond to. `None` means
            to respond to all scopes. Ignored for blueprints.
        priority: the priority of the route if the app is an ASGI web
            application. Web applications with higher priorities take precedence
            over lower ones. Ignored for blueprints.

    Returns:
        a function that can be called to unmount the application. Unmounting
        of blueprints is not supported yet.
    """
    global exports, quart_app

    if isinstance(app, Blueprint):
        quart_app.register_blueprint(app, url_prefix=path)
    else:
        return exports["asgi_app"].add(app, scopes=scopes, path=path, priority=priority)


@contextmanager
def mounted(
    app, *, path: str, scopes: Optional[Iterable[str]] = None, priority: int = 0
):
    """Context manager that mounts the given ASGI web application or Quart
    blueprint at the given path, and unmounts it when the context is exited.

    Parameters:
        app: the ASGI web application or Quart blueprint to mount
        path: the path to mount the web application or blueprint at
        scopes: when the app is an ASGI web application, specifies the ASGI
            scopes that the web application should respond to. `None` means
            to respond to all scopes. Ignored for blueprints.
        priority: the priority of the route if the app is an ASGI web
            application. Web applications with higher priorities take precedence
            over lower ones. Ignored for blueprints.
    """
    remover = mount(app, path=path, scopes=scopes, priority=priority)
    try:
        yield
    finally:
        if remover:
            remover()


def propose_index_page(route: str, priority: int = 0) -> Disposer:
    """Proposes the given route as a potential index page for the
    Skybrush server. This method can be called from the ``load()``
    functions of extensions when they want to propose one of their own
    routes as an index page. The server will select the index page with
    the highest priority when all the extensions have been loaded.

    Parameters:
        route: name of a route to propose as the index page, in the form of
            ``blueprint.route`` (e.g., ``debug.index``)
        priority: the priority of the proposed route.

    Returns:
        a function that can be called with no arguments to reovke the proposed
        index page
    """
    global proposed_index_pages

    page = ProposedIndexPage(priority=-priority, route=route)

    def disposer():
        index = proposed_index_pages.index(page)
        if index >= 0:
            proposed_index_pages[index] = proposed_index_pages[-1]
            proposed_index_pages.pop()
            heapify(proposed_index_pages)

    heappush(proposed_index_pages, page)

    return disposer


@contextmanager
def proposed_index_page(route: str, priority: int = 0):
    """Context manager that adds the given route as a potential index page for
    the Skybrush server as long as the execution is within the context, and
    revokes it when the context is exited.

    Parameters:
        route: name of a route to propose as the index page, in the form of
            ``blueprint.route`` (e.g., ``debug.index``)
        priority: the priority of the proposed route.
    """
    disposer = propose_index_page(route, priority)
    try:
        yield
    finally:
        disposer()


############################################################################


def load(app, configuration):
    """Loads the extension."""
    global exports

    address = (
        configuration.get("host", "localhost"),
        configuration.get("port", get_port_number_for_service("http")),
    )

    exports.update(address=address, asgi_app=create_app())


def unload(app):
    """Unloads the extension."""
    global exports, quart_app

    quart_app = None
    exports.update(address=None, asgi_app=None)


async def run(app, configuration, logger):
    global exports

    address = exports.get("address")
    if address is None:
        logger.warn("HTTP server address is not specified in configuration")
        return

    host, port = address

    # Don't show info messages by default (unless the app is in debug mode),
    # show warnings and errors only
    server_log = logger.getChild("hypercorn")
    if not app.debug:
        server_log.setLevel(logging.WARNING)

    # Create configuration for Hypercorn
    config = HyperConfig()
    config.accesslog = server_log
    config.bind = [f"{host}:{port}"]
    config.certfile = configuration.get("certfile") or None
    config.errorlog = server_log
    config.keyfile = configuration.get("keyfile") or None
    config.use_reloader = False

    secure = bool(config.ssl_enabled)

    retries = 0
    max_retries = 3

    while True:
        logger.info(
            "Starting {1} server on {0}".format(
                format_socket_address(address), "HTTPS" if secure else "HTTP"
            )
        )

        started_at = current_time()

        try:
            await serve(exports["asgi_app"], config)
        except Exception:
            # Server crashed -- maybe a change in IP address? Let's try again
            # if we have not reached the maximum retry count.
            if current_time() - started_at >= 5:
                retries = 0

            if retries < max_retries:
                logger.error(
                    "Server stopped unexpectedly, retrying...",
                    extra={"sentry_ignore": True},
                )
                await sleep(1)
                retries += 1
            else:
                # Re-raise the exception; the extension manager will take care
                # of logging it nicely
                raise
        else:
            break


exports = {
    "address": None,
    "asgi_app": None,
    "mount": mount,
    "mounted": mounted,
    "propose_index_page": propose_index_page,
    "proposed_index_page": proposed_index_page,
}

schema = {
    "properties": {
        "host": {
            "type": "string",
            "title": "Host",
            "description": (
                "IP address of the host that the server should listen on. Use "
                "an empty string to listen on all interfaces, or 127.0.0.1 to "
                "listen on localhost only"
            ),
            "default": "127.0.0.1",
            "propertyOrder": 10,
        },
        "port": {
            "type": "number",
            "title": "Port",
            "description": (
                "Port that the server should listen on. Untick the checkbox to "
                "let the server derive the port number from its own base port."
            ),
            "minValue": 1,
            "maxValue": 65535,
            "default": get_port_number_for_service("http"),
            "required": False,
            "propertyOrder": 20,
        },
        "certfile": {
            "type": "string",
            "title": "Certificate file",
            "description": "Full path to the certificate file that the server should use for HTTPS connections",
            "required": False,
        },
        "keyfile": {
            "type": "string",
            "title": "Private key file",
            "description": "Full path to the private key file corresopnding to the certificate",
            "required": False,
        },
    }
}
