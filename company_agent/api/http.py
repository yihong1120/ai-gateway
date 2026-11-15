"""Shared private browser contract and the host authentication dependency."""
from open_webui.utils.auth import get_verified_user as active
from examples.shared.runtime.browser import PrivateRoute

__all__ = ['PrivateRoute', 'active']
