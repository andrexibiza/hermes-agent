"""Hermes directory-plugin entry point for the Sprites terminal backend."""

if __package__:
    from .hermes_plugin_sprites import register
else:  # pytest/importlib may execute a repository-root __init__.py as a module
    from hermes_plugin_sprites import register

__all__ = ["register"]
