"""
DocIngest GUI — pywebview shell + hand-written frontend, decoupled in three
layers (GUI_DESIGN.md):

  gui_app.py    shell      — launches the pywebview window (swappable)
  gui_api.py    bridge     — js_api object exposed to JS (dict/native only)
  gui_logic.py  adapter    — translates intents → docingest.api (no UI types)
  web/          frontend   — hand-written HTML/CSS/JS, restores GUI_DRAFT.pen

Launch: ``python -m docingest.gui``
The core (docingest.api) is never modified by this package.
"""

from .gui_app import main

__all__ = ["main"]
