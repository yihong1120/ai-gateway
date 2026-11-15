"""Host installation is atomic and idempotent before and after startup."""
from __future__ import annotations

from contextlib import asynccontextmanager
import unittest
from fastapi import FastAPI, APIRouter
from starlette.middleware.base import BaseHTTPMiddleware
from examples.shared.runtime.installation import install_once


class InstallationTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeat_install_starts_one_listener_and_leaves_other_apps_independent(self):
        starts, stops = [], []
        def install(app):
            with install_once(app, 'feature_installed') as active:
                if not active:
                    return
                original = app.router.lifespan_context
                @asynccontextmanager
                async def lifespan(current):
                    async with original(current):
                        starts.append(current)
                        try:
                            yield
                        finally:
                            stops.append(current)
                app.router.lifespan_context = lifespan
                router = APIRouter()
                router.add_api_route('/feature', lambda: {'ok': True})
                app.router.routes[:0] = router.routes
                app.add_middleware(BaseHTTPMiddleware)
        one, two = FastAPI(), FastAPI()
        install(one)
        install(one)
        install(two)
        self.assertEqual(sum(route.path == '/feature' for route in one.routes), 1)
        self.assertEqual(len(one.user_middleware), 1)
        async with one.router.lifespan_context(one):
            one.middleware_stack = object()
            install(one)
        async with two.router.lifespan_context(two):
            pass
        self.assertEqual(starts, [one,two])
        self.assertEqual(stops, [one,two])

    async def test_partial_install_rolls_back_nested_flags_routes_and_middleware(self):
        app = FastAPI()
        app.state.marker = 'original'
        original = app.router.lifespan_context
        routes = list(app.routes)
        with self.assertRaises(ValueError):
            with install_once(app, 'root_installed'):
                app.state.marker = 'changed'
                with install_once(app, 'child_installed'):
                    app.add_middleware(BaseHTTPMiddleware)
                    app.router.lifespan_context = object()
                    app.router.routes.append(object())
                raise ValueError('injected assembly failure')
        self.assertEqual(app.routes, routes)
        self.assertEqual(app.user_middleware, [])
        self.assertIs(app.router.lifespan_context, original)
        self.assertEqual(app.state.marker, 'original')
        self.assertFalse(getattr(app.state, 'child_installed', False))
        with install_once(app, 'root_installed') as active:
            self.assertTrue(active)
        with install_once(app, 'root_installed') as active:
            self.assertFalse(active)

    async def test_first_install_after_startup_is_rejected_before_mutation(self):
        app = FastAPI()
        app.middleware_stack = object()
        before = list(app.routes)
        with self.assertRaises(RuntimeError):
            with install_once(app, 'late_feature'):
                app.router.routes.append(object())
        self.assertEqual(app.routes, before)
