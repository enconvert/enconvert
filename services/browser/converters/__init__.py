"""Open-source fallback for the browser converter package.

Re-exports the basic headless-browser converters so open code importing
``from services.browser.converters import ...`` works unchanged.
"""

from .url_pdf import url_to_pdf
from .url_ss import url_to_png
from .url_markdown import url_to_markdown

__all__ = ['url_to_pdf', 'url_to_png', 'url_to_markdown']
