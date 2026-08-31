"""NeuroCut: an MLT-backed multi-track video editor exposed over MCP."""
from .server import main, mcp

__version__ = "0.1.0"
__all__ = ["mcp", "main"]
