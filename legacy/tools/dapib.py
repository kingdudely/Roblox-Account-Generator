"""Shim — the dapib transform now lives in the package.

`roblox_creator/dapib.py` is the real implementation, because the creator needs
it at runtime and tools should not be on the import path of shipped code.
"""

from roblox_creator.dapib import (  # noqa: F401
    VerificationError,
    remap_char,
    transform,
    transform_one,
)

__all__ = ["transform", "transform_one", "remap_char", "VerificationError"]
