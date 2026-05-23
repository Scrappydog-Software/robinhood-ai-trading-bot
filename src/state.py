"""In-process shared state for the unified trading bot application.

Replaces JSON-file IPC between main.py and webui.py. The TradingState
dataclass holds the latest cycle results in memory so the web UI can
read them without disk round-trips. The JSON file is still written as
a side effect (for debugging / external tools) but the web UI reads
from this module first.

Thread safety: all mutations go through the update() method which
acquires a threading.Lock. Reads of individual scalar attributes are
atomic in CPython (GIL), but callers that need a consistent snapshot
of multiple fields should use snapshot().
"""

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class TradingState:
    """Shared mutable state for the trading loop and web UI."""

    # --- Loop lifecycle ---
    loop_running: bool = False
    last_cycle_time: Optional[str] = None   # ISO-8601 UTC timestamp
    last_cycle_error: Optional[str] = None  # error message if last cycle failed
    market_open: Optional[bool] = None

    # --- AI decisions (from the most recent cycle) ---
    decisions: List[Dict[str, Any]] = field(default_factory=list)

    # --- Auth ---
    logged_in: bool = False
    token_expiry: float = 0.0  # epoch seconds

    # --- Internal lock ---
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, **kwargs):
        """Atomically update one or more fields."""
        with self._lock:
            for key, value in kwargs.items():
                if not hasattr(self, key) or key.startswith('_'):
                    raise AttributeError(f"TradingState has no field '{key}'")
                setattr(self, key, value)

    def snapshot(self) -> dict:
        """Return a consistent dict copy of all public fields."""
        with self._lock:
            return {
                'loop_running': self.loop_running,
                'last_cycle_time': self.last_cycle_time,
                'last_cycle_error': self.last_cycle_error,
                'market_open': self.market_open,
                'decisions': list(self.decisions),
                'logged_in': self.logged_in,
                'token_expiry': self.token_expiry,
            }


# Module-level singleton — imported by app.py, webui.py, and the trading loop.
trading_state = TradingState()
