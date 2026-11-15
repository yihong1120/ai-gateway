"""Install host extensions once, rolling back partial in-process assembly."""
from __future__ import annotations
from contextlib import contextmanager


@contextmanager
def install_once(app, flag):
    if getattr(app.state, flag, False):
        yield False
        return
    if app.middleware_stack is not None:
        raise RuntimeError('Install application extensions before startup.')
    routes = list(app.router.routes)
    middleware = list(app.user_middleware)
    lifespan = app.router.lifespan_context
    state = dict(app.state._state)
    try:
        yield True
    except BaseException:
        app.router.routes[:] = routes
        app.user_middleware[:] = middleware
        app.router.lifespan_context = lifespan
        app.state._state.clear()
        app.state._state.update(state)
        raise
    else:
        setattr(app.state, flag, True)
