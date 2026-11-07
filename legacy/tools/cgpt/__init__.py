"""Vendored anonymous-ChatGPT sentinel toolkit.

Source: https://github.com/realasfngl/ChatGPT (decompiler + VM + PoW solver).
Only the pieces needed to satisfy /backend-anon sentinel checks live here; the
HTTP client, image path and SSE parser are ours and adapted to the 2026 wire
format.
"""

from .challenges import Challenges
from .decompiler import Decompiler
from .parse import Parser
from .vm import VM
from .headers import Headers

__all__ = ["Challenges", "Decompiler", "Parser", "VM", "Headers"]
