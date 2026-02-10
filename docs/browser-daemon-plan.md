# Browser Daemon Architecture Plan

## Overview

Replace the flaky in-process MCP browser tools with a robust daemon + CLI architecture.
Each CLI invocation communicates with a long-running daemon that manages CDP connections and tabs.

```
┌─────────────┐     Unix Socket     ┌──────────────────┐     CDP      ┌─────────┐
│ browser-cli │ ◄─────────────────► │ browser-daemon   │ ◄──────────► │ Chrome  │
│ (stateless) │    JSON Protocol    │ (session owner)  │  WebSocket   │         │
└─────────────┘                     └──────────────────┘              └─────────┘
      ▲                                      │
      │                                      ▼
      │                             ┌──────────────────┐
  Skill invokes                     │ Session Registry │
                                    │ {111: Tab, ...}  │
                                    └──────────────────┘
```

---

## Components

### 1. Protocol Layer (`browser/protocol.py`)

**Responsibility:** Type definitions for all IPC communication. Single source of truth.

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Any
import uuid
from datetime import datetime

class Command(str, Enum):
    NAVIGATE = "navigate"
    SNAPSHOT = "snapshot"
    CLICK = "click"
    TYPE = "type"
    SCROLL = "scroll"
    FIND = "find"
    CLOSE = "close"
    STATUS = "status"  # Daemon introspection

@dataclass
class BrowserRequest:
    id: str                    # UUID for request correlation
    session_id: int            # Thread/session identifier
    command: Command
    args: dict[str, Any]
    timestamp: datetime

    @classmethod
    def create(cls, session_id: int, command: Command, args: dict) -> "BrowserRequest":
        return cls(
            id=str(uuid.uuid4()),
            session_id=session_id,
            command=command,
            args=args,
            timestamp=datetime.utcnow()
        )

@dataclass
class BrowserResponse:
    id: str                    # Matches request ID
    session_id: int
    success: bool
    result: Optional[dict[str, Any]]
    error: Optional[str]
    error_code: Optional[str]  # Machine-readable error type
    timestamp: datetime
    duration_ms: float         # How long the command took

class ErrorCode(str, Enum):
    CDP_CONNECTION_FAILED = "cdp_connection_failed"
    CDP_CONNECTION_LOST = "cdp_connection_lost"
    TAB_NOT_FOUND = "tab_not_found"
    ELEMENT_NOT_FOUND = "element_not_found"
    NAVIGATION_FAILED = "navigation_failed"
    TIMEOUT = "timeout"
    INVALID_ARGS = "invalid_args"
    CHROME_NOT_RUNNING = "chrome_not_running"
    INTERNAL_ERROR = "internal_error"

# Command-specific argument types
@dataclass
class NavigateArgs:
    url: str
    timeout: float = 30.0

@dataclass
class ClickArgs:
    backend_node_id: Optional[int] = None  # Preferred: from find results
    role: Optional[str] = None             # Fallback: accessibility role
    name: Optional[str] = None             # Fallback: accessibility name
    index: int = 0

@dataclass
class TypeArgs:
    text: str
    role: Optional[str] = None
    name: Optional[str] = None
    press_enter: bool = False
    clear_first: bool = True

@dataclass
class ScrollArgs:
    direction: str  # "up", "down", "top", "bottom"
    pixels: int = 500

@dataclass
class FindArgs:
    text: str
    exact: bool = False
    max_matches: int = 10

# Result types
@dataclass
class NavigateResult:
    url: str
    title: str
    screenshot_path: str

@dataclass
class SnapshotResult:
    url: str
    title: str
    screenshot_path: str
    accessibility_tree: str

@dataclass
class ClickResult:
    screenshot_path: str
    new_url: Optional[str] = None  # If navigation occurred

@dataclass
class TypeResult:
    screenshot_path: str

@dataclass
class ScrollResult:
    screenshot_path: str
    scroll_y: int
    scroll_height: int

@dataclass
class FindMatch:
    index: int
    matched_text: str
    bounds: dict[str, int]  # x, y, width, height
    element_tag: str
    element_backend_node_id: Optional[int]
    clickable_ancestor_tag: Optional[str]
    clickable_ancestor_backend_node_id: Optional[int]
    snippet: str

@dataclass
class FindResult:
    matches: list[FindMatch]
    total_found: int
    viewport_width: int
    viewport_height: int
```

### 2. Logger (`browser/logger.py`)

**Responsibility:** Structured, session-aware logging to files that agents can read.

```python
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Any, Optional
from dataclasses import asdict

LOG_DIR = Path.home() / ".browser-cli" / "logs"

class BrowserLogger:
    """Session-aware structured logger."""

    def __init__(self, component: str, session_id: Optional[int] = None):
        self.component = component  # "daemon", "cli", "cdp"
        self.session_id = session_id
        self._ensure_log_dir()

    def _ensure_log_dir(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    def _log_file(self) -> Path:
        if self.session_id:
            return LOG_DIR / f"session_{self.session_id}.log"
        return LOG_DIR / f"{self.component}.log"

    def _write(self, level: str, event: str, data: Optional[dict] = None, error: Optional[str] = None):
        entry = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "level": level,
            "component": self.component,
            "event": event,
        }
        if self.session_id:
            entry["session_id"] = self.session_id
        if data:
            entry["data"] = data
        if error:
            entry["error"] = error

        line = json.dumps(entry, default=str) + "\n"

        # Write to component/session log
        with open(self._log_file(), "a") as f:
            f.write(line)

        # Also write errors to aggregated error log
        if level == "ERROR":
            with open(LOG_DIR / "errors.log", "a") as f:
                f.write(line)

    def info(self, event: str, data: Optional[dict] = None):
        self._write("INFO", event, data)

    def error(self, event: str, error: str, data: Optional[dict] = None):
        self._write("ERROR", event, data, error)

    def debug(self, event: str, data: Optional[dict] = None):
        self._write("DEBUG", event, data)

    def command_start(self, request_id: str, command: str, args: dict):
        self.info("command_start", {"request_id": request_id, "command": command, "args": args})

    def command_complete(self, request_id: str, command: str, duration_ms: float, result: dict):
        self.info("command_complete", {
            "request_id": request_id,
            "command": command,
            "duration_ms": duration_ms,
            "result_keys": list(result.keys())  # Don't log full result, too verbose
        })

    def command_error(self, request_id: str, command: str, error_code: str, error: str):
        self.error("command_error", error, {
            "request_id": request_id,
            "command": command,
            "error_code": error_code
        })

def get_logger(component: str, session_id: Optional[int] = None) -> BrowserLogger:
    return BrowserLogger(component, session_id)
```

**Data directory structure:**
```
~/.browser-cli/
├── logs/
│   ├── daemon.log           # Daemon lifecycle, connection events
│   ├── session_12345.log    # All commands for session 12345
│   ├── session_67890.log    # All commands for session 67890
│   └── errors.log           # Aggregated errors from all sources
├── screenshots/
│   └── screenshot_{session}_{timestamp}.png
├── browser-daemon.sock      # Unix socket for IPC
└── browser-daemon.pid       # Daemon PID file
```

**Log format (JSON Lines):**
```json
{"ts": "2024-01-15T10:30:00.123Z", "level": "INFO", "component": "daemon", "event": "started", "data": {"pid": 12345, "socket": "/tmp/browser-daemon.sock"}}
{"ts": "2024-01-15T10:30:01.456Z", "level": "INFO", "component": "daemon", "session_id": 111, "event": "command_start", "data": {"request_id": "abc-123", "command": "navigate", "args": {"url": "https://example.com"}}}
{"ts": "2024-01-15T10:30:02.789Z", "level": "INFO", "component": "daemon", "session_id": 111, "event": "command_complete", "data": {"request_id": "abc-123", "command": "navigate", "duration_ms": 1333, "result_keys": ["url", "title", "screenshot_path"]}}
{"ts": "2024-01-15T10:30:05.000Z", "level": "ERROR", "component": "daemon", "session_id": 111, "event": "command_error", "data": {"request_id": "def-456", "command": "click", "error_code": "element_not_found"}, "error": "No element with role='button' name='Submit'"}
```

### 3. CDP Client (`browser/cdp_client.py`)

**Responsibility:** Thin wrapper around vendored cdp_browser. Handles connection lifecycle.

```python
from typing import Optional
from pathlib import Path
from vendor.cdp_browser import Browser, Page
from vendor.cdp_browser.errors import (
    ConnectionError as CDPConnectionError,
    ElementNotFoundError,
    NavigationError,
    TimeoutError as CDPTimeoutError
)
from .protocol import ErrorCode
from .logger import get_logger

class CDPClient:
    """Manages CDP connection and page lifecycle."""

    def __init__(self, endpoint: str, screenshot_dir: Path):
        self.endpoint = endpoint
        self.screenshot_dir = screenshot_dir
        self._browser: Optional[Browser] = None
        self._pages: dict[int, Page] = {}  # session_id -> Page
        self._logger = get_logger("cdp")

        screenshot_dir.mkdir(parents=True, exist_ok=True)

    async def connect(self) -> None:
        """Establish CDP connection."""
        self._logger.info("connecting", {"endpoint": self.endpoint})
        try:
            self._browser = await Browser(self.endpoint).connect()
            self._logger.info("connected")
        except Exception as e:
            self._logger.error("connection_failed", str(e))
            raise

    async def disconnect(self) -> None:
        """Close all pages and disconnect."""
        self._logger.info("disconnecting", {"active_sessions": list(self._pages.keys())})
        for session_id in list(self._pages.keys()):
            await self.close_page(session_id)
        if self._browser:
            await self._browser.close()
            self._browser = None
        self._logger.info("disconnected")

    async def ensure_connected(self) -> None:
        """Reconnect if connection lost."""
        if self._browser is None:
            await self.connect()
        # TODO: Health check - try a simple operation

    async def get_or_create_page(self, session_id: int) -> Page:
        """Get existing page for session or create new one."""
        if session_id in self._pages:
            page = self._pages[session_id]
            # Verify page is still alive
            try:
                await page.evaluate("1+1")
                return page
            except Exception:
                self._logger.info("page_stale", {"session_id": session_id})
                del self._pages[session_id]

        # Create new page
        await self.ensure_connected()
        assert self._browser is not None
        page = await self._browser.new_page()
        self._pages[session_id] = page
        self._logger.info("page_created", {"session_id": session_id})
        return page

    async def close_page(self, session_id: int) -> bool:
        """Close page for session."""
        if session_id not in self._pages:
            return False
        try:
            await self._pages[session_id].close()
        except Exception:
            pass
        del self._pages[session_id]
        self._logger.info("page_closed", {"session_id": session_id})
        return True

    def get_screenshot_path(self, session_id: int) -> Path:
        """Generate unique screenshot path."""
        import time
        return self.screenshot_dir / f"screenshot_{session_id}_{int(time.time() * 1000)}.png"

    @property
    def active_sessions(self) -> list[int]:
        return list(self._pages.keys())
```

### 4. Command Handlers (`browser/handlers.py`)

**Responsibility:** Execute browser commands. Pure logic, no IPC concerns.

```python
from typing import Any
from .protocol import (
    Command, NavigateArgs, ClickArgs, TypeArgs, ScrollArgs, FindArgs,
    NavigateResult, SnapshotResult, ClickResult, TypeResult, ScrollResult, FindResult, FindMatch,
    ErrorCode
)
from .cdp_client import CDPClient
from .logger import get_logger
import asyncio

class CommandError(Exception):
    def __init__(self, code: ErrorCode, message: str):
        self.code = code
        self.message = message
        super().__init__(message)

class CommandHandler:
    """Executes browser commands."""

    def __init__(self, cdp: CDPClient):
        self.cdp = cdp

    async def execute(self, session_id: int, command: Command, args: dict[str, Any]) -> dict[str, Any]:
        """Route command to appropriate handler."""
        logger = get_logger("handler", session_id)

        handlers = {
            Command.NAVIGATE: self._navigate,
            Command.SNAPSHOT: self._snapshot,
            Command.CLICK: self._click,
            Command.TYPE: self._type,
            Command.SCROLL: self._scroll,
            Command.FIND: self._find,
            Command.CLOSE: self._close,
            Command.STATUS: self._status,
        }

        handler = handlers.get(command)
        if not handler:
            raise CommandError(ErrorCode.INVALID_ARGS, f"Unknown command: {command}")

        return await handler(session_id, args)

    async def _navigate(self, session_id: int, args: dict) -> dict:
        page = await self.cdp.get_or_create_page(session_id)
        url = args.get("url")
        timeout = args.get("timeout", 30.0)

        if not url:
            raise CommandError(ErrorCode.INVALID_ARGS, "url is required")

        try:
            await page.goto(url, timeout=timeout)
        except Exception as e:
            raise CommandError(ErrorCode.NAVIGATION_FAILED, str(e))

        await asyncio.sleep(1)  # Brief wait for dynamic content

        screenshot_path = self.cdp.get_screenshot_path(session_id)
        await page.screenshot(path=str(screenshot_path))

        return {
            "url": page.url,
            "title": await page.title(),
            "screenshot_path": str(screenshot_path)
        }

    async def _snapshot(self, session_id: int, args: dict) -> dict:
        page = await self.cdp.get_or_create_page(session_id)

        screenshot_path = self.cdp.get_screenshot_path(session_id)
        await page.screenshot(path=str(screenshot_path))

        tree = await page.accessibility_tree()
        # Truncate if too large
        if len(tree) > 15000:
            tree = tree[:15000] + "\n... (truncated)"

        return {
            "url": page.url,
            "title": await page.title(),
            "screenshot_path": str(screenshot_path),
            "accessibility_tree": tree
        }

    async def _click(self, session_id: int, args: dict) -> dict:
        page = await self.cdp.get_or_create_page(session_id)

        backend_node_id = args.get("backend_node_id")
        role = args.get("role")
        name = args.get("name")
        index = args.get("index", 0)

        try:
            if backend_node_id is not None:
                await page.click_by_node_id(backend_node_id)
            elif role:
                await page.click(role, name=name, index=index)
            else:
                raise CommandError(ErrorCode.INVALID_ARGS, "backend_node_id or role required")
        except Exception as e:
            if "not found" in str(e).lower():
                raise CommandError(ErrorCode.ELEMENT_NOT_FOUND, str(e))
            raise CommandError(ErrorCode.INTERNAL_ERROR, str(e))

        await asyncio.sleep(0.5)

        screenshot_path = self.cdp.get_screenshot_path(session_id)
        await page.screenshot(path=str(screenshot_path))

        return {
            "screenshot_path": str(screenshot_path),
            "url": page.url
        }

    async def _type(self, session_id: int, args: dict) -> dict:
        page = await self.cdp.get_or_create_page(session_id)

        text = args.get("text", "")
        role = args.get("role")
        name = args.get("name")
        press_enter = args.get("press_enter", False)

        try:
            if role:
                await page.type(role, name=name, text=text)
            else:
                # Type into focused element
                for char in text:
                    await page.press(char)

            if press_enter:
                await page.press("Enter")
        except Exception as e:
            if "not found" in str(e).lower():
                raise CommandError(ErrorCode.ELEMENT_NOT_FOUND, str(e))
            raise CommandError(ErrorCode.INTERNAL_ERROR, str(e))

        await asyncio.sleep(0.5)

        screenshot_path = self.cdp.get_screenshot_path(session_id)
        await page.screenshot(path=str(screenshot_path))

        return {"screenshot_path": str(screenshot_path)}

    async def _scroll(self, session_id: int, args: dict) -> dict:
        page = await self.cdp.get_or_create_page(session_id)

        direction = args.get("direction", "down").lower()
        pixels = args.get("pixels", 500)

        if direction == "top":
            await page.evaluate("window.scrollTo(0, 0)")
        elif direction == "bottom":
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        elif direction == "up":
            await page.evaluate(f"window.scrollBy(0, -{pixels})")
        elif direction == "down":
            await page.evaluate(f"window.scrollBy(0, {pixels})")
        else:
            raise CommandError(ErrorCode.INVALID_ARGS, f"Invalid direction: {direction}")

        await asyncio.sleep(0.3)

        screenshot_path = self.cdp.get_screenshot_path(session_id)
        await page.screenshot(path=str(screenshot_path))

        scroll_pos = await page.evaluate("({y: window.scrollY, height: document.body.scrollHeight})")

        return {
            "screenshot_path": str(screenshot_path),
            "scroll_y": scroll_pos["y"],
            "scroll_height": scroll_pos["height"]
        }

    async def _find(self, session_id: int, args: dict) -> dict:
        page = await self.cdp.get_or_create_page(session_id)

        text = args.get("text")
        exact = args.get("exact", False)
        max_matches = args.get("max_matches", 10)

        if not text:
            raise CommandError(ErrorCode.INVALID_ARGS, "text is required")

        result = await page.find_by_text(text=text, exact=exact, max_matches=max_matches)

        matches = []
        for m in result.get("matches", []):
            matches.append({
                "index": m.get("index"),
                "matched_text": m.get("matched_text"),
                "bounds": m.get("bounds"),
                "element_tag": m.get("element", {}).get("tag"),
                "element_backend_node_id": m.get("element", {}).get("backend_node_id"),
                "clickable_ancestor_tag": m.get("clickable_ancestor", {}).get("tag") if m.get("clickable_ancestor") else None,
                "clickable_ancestor_backend_node_id": m.get("clickable_ancestor", {}).get("backend_node_id") if m.get("clickable_ancestor") else None,
                "snippet": m.get("snippet", "")
            })

        return {
            "matches": matches,
            "total_found": result.get("totalFound", 0),
            "viewport_width": result.get("viewport", {}).get("width", 0),
            "viewport_height": result.get("viewport", {}).get("height", 0)
        }

    async def _close(self, session_id: int, args: dict) -> dict:
        closed = await self.cdp.close_page(session_id)
        return {"closed": closed}

    async def _status(self, session_id: int, args: dict) -> dict:
        return {
            "active_sessions": self.cdp.active_sessions,
            "session_active": session_id in self.cdp.active_sessions
        }
```

### 5. Daemon Server (`browser/daemon.py`)

**Responsibility:** IPC server, request routing, lifecycle management.

```python
import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional

from .protocol import BrowserRequest, BrowserResponse, Command, ErrorCode
from .cdp_client import CDPClient
from .handlers import CommandHandler, CommandError
from .logger import get_logger

DATA_DIR = Path.home() / ".browser-cli"
SOCKET_PATH = DATA_DIR / "browser-daemon.sock"  # In data dir, not /tmp
PID_FILE = DATA_DIR / "browser-daemon.pid"
SCREENSHOT_DIR = DATA_DIR / "screenshots"

class BrowserDaemon:
    def __init__(self, cdp_endpoint: str):
        self.cdp_endpoint = cdp_endpoint
        self.cdp: Optional[CDPClient] = None
        self.handler: Optional[CommandHandler] = None
        self.logger = get_logger("daemon")
        self._server: Optional[asyncio.Server] = None
        self._shutdown = asyncio.Event()

    async def start(self):
        """Start the daemon server."""
        self.logger.info("starting", {"pid": os.getpid(), "socket": str(SOCKET_PATH)})

        # Clean up stale socket
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()

        # Write PID file
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()))

        # Initialize CDP
        self.cdp = CDPClient(self.cdp_endpoint, SCREENSHOT_DIR)
        try:
            await self.cdp.connect()
        except Exception as e:
            self.logger.error("cdp_connect_failed", str(e))
            raise

        self.handler = CommandHandler(self.cdp)

        # Start server
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(SOCKET_PATH)
        )
        SOCKET_PATH.chmod(0o600)  # Only owner can connect

        self.logger.info("started", {"socket": str(SOCKET_PATH)})

        # Handle shutdown signals
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.shutdown()))

        # Wait for shutdown
        await self._shutdown.wait()

    async def shutdown(self):
        """Graceful shutdown."""
        self.logger.info("shutting_down")

        if self._server:
            self._server.close()
            await self._server.wait_closed()

        if self.cdp:
            await self.cdp.disconnect()

        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()
        if PID_FILE.exists():
            PID_FILE.unlink()

        self.logger.info("shutdown_complete")
        self._shutdown.set()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle a single client connection."""
        try:
            while True:
                # Read length-prefixed message
                length_bytes = await reader.read(4)
                if not length_bytes:
                    break

                length = int.from_bytes(length_bytes, "big")
                data = await reader.read(length)
                if not data:
                    break

                # Parse request
                try:
                    request_dict = json.loads(data.decode())
                    request = BrowserRequest(
                        id=request_dict["id"],
                        session_id=request_dict["session_id"],
                        command=Command(request_dict["command"]),
                        args=request_dict.get("args", {}),
                        timestamp=datetime.fromisoformat(request_dict["timestamp"])
                    )
                except Exception as e:
                    response = self._error_response("", 0, ErrorCode.INVALID_ARGS, f"Invalid request: {e}")
                    await self._send_response(writer, response)
                    continue

                # Execute command
                response = await self._execute_request(request)
                await self._send_response(writer, response)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger.error("client_error", str(e))
        finally:
            writer.close()
            await writer.wait_closed()

    async def _execute_request(self, request: BrowserRequest) -> BrowserResponse:
        """Execute a request and return response."""
        session_logger = get_logger("daemon", request.session_id)
        session_logger.command_start(request.id, request.command.value, request.args)

        start = datetime.utcnow()

        try:
            result = await self.handler.execute(
                request.session_id,
                request.command,
                request.args
            )

            duration = (datetime.utcnow() - start).total_seconds() * 1000
            session_logger.command_complete(request.id, request.command.value, duration, result)

            return BrowserResponse(
                id=request.id,
                session_id=request.session_id,
                success=True,
                result=result,
                error=None,
                error_code=None,
                timestamp=datetime.utcnow(),
                duration_ms=duration
            )

        except CommandError as e:
            duration = (datetime.utcnow() - start).total_seconds() * 1000
            session_logger.command_error(request.id, request.command.value, e.code.value, e.message)

            return BrowserResponse(
                id=request.id,
                session_id=request.session_id,
                success=False,
                result=None,
                error=e.message,
                error_code=e.code.value,
                timestamp=datetime.utcnow(),
                duration_ms=duration
            )

        except Exception as e:
            duration = (datetime.utcnow() - start).total_seconds() * 1000
            session_logger.command_error(request.id, request.command.value, ErrorCode.INTERNAL_ERROR.value, str(e))

            return BrowserResponse(
                id=request.id,
                session_id=request.session_id,
                success=False,
                result=None,
                error=str(e),
                error_code=ErrorCode.INTERNAL_ERROR.value,
                timestamp=datetime.utcnow(),
                duration_ms=duration
            )

    def _error_response(self, request_id: str, session_id: int, code: ErrorCode, message: str) -> BrowserResponse:
        return BrowserResponse(
            id=request_id,
            session_id=session_id,
            success=False,
            result=None,
            error=message,
            error_code=code.value,
            timestamp=datetime.utcnow(),
            duration_ms=0
        )

    async def _send_response(self, writer: asyncio.StreamWriter, response: BrowserResponse):
        """Send length-prefixed response."""
        data = json.dumps({
            "id": response.id,
            "session_id": response.session_id,
            "success": response.success,
            "result": response.result,
            "error": response.error,
            "error_code": response.error_code,
            "timestamp": response.timestamp.isoformat(),
            "duration_ms": response.duration_ms
        }).encode()

        writer.write(len(data).to_bytes(4, "big"))
        writer.write(data)
        await writer.drain()


async def run_daemon(cdp_endpoint: str):
    daemon = BrowserDaemon(cdp_endpoint)
    await daemon.start()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Browser automation daemon")
    parser.add_argument("--cdp-endpoint", default=os.environ.get("BROWSER_CDP_ENDPOINT", "http://localhost:9222"))
    args = parser.parse_args()

    if not args.cdp_endpoint:
        print("Error: CDP endpoint required. Set BROWSER_CDP_ENDPOINT or use --cdp-endpoint", file=sys.stderr)
        sys.exit(1)

    asyncio.run(run_daemon(args.cdp_endpoint))


if __name__ == "__main__":
    main()
```

### 6. CLI Client (`browser/cli.py`)

**Responsibility:** Stateless CLI that sends commands to daemon.

```python
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .protocol import Command, BrowserRequest, BrowserResponse
from .logger import get_logger

DATA_DIR = Path.home() / ".browser-cli"
SOCKET_PATH = DATA_DIR / "browser-daemon.sock"
PID_FILE = DATA_DIR / "browser-daemon.pid"
DAEMON_START_TIMEOUT = 5.0

class CLIClient:
    def __init__(self):
        self.logger = get_logger("cli")

    async def send_command(self, session_id: int, command: Command, args: dict) -> BrowserResponse:
        """Send command to daemon, auto-starting if needed."""

        # Ensure daemon is running
        if not await self._daemon_running():
            self.logger.info("starting_daemon")
            await self._start_daemon()

        # Connect and send
        try:
            reader, writer = await asyncio.open_unix_connection(str(SOCKET_PATH))
        except Exception as e:
            self.logger.error("connect_failed", str(e))
            raise RuntimeError(f"Cannot connect to daemon: {e}")

        try:
            request = BrowserRequest.create(session_id, command, args)
            self.logger.info("sending", {"command": command.value, "session_id": session_id})

            # Send length-prefixed request
            data = json.dumps({
                "id": request.id,
                "session_id": request.session_id,
                "command": request.command.value,
                "args": request.args,
                "timestamp": request.timestamp.isoformat()
            }).encode()

            writer.write(len(data).to_bytes(4, "big"))
            writer.write(data)
            await writer.drain()

            # Read response
            length_bytes = await reader.read(4)
            length = int.from_bytes(length_bytes, "big")
            response_data = await reader.read(length)

            response_dict = json.loads(response_data.decode())
            return BrowserResponse(
                id=response_dict["id"],
                session_id=response_dict["session_id"],
                success=response_dict["success"],
                result=response_dict.get("result"),
                error=response_dict.get("error"),
                error_code=response_dict.get("error_code"),
                timestamp=datetime.fromisoformat(response_dict["timestamp"]),
                duration_ms=response_dict["duration_ms"]
            )

        finally:
            writer.close()
            await writer.wait_closed()

    async def _daemon_running(self) -> bool:
        """Check if daemon is running."""
        if not PID_FILE.exists():
            return False

        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)  # Check if process exists
            return SOCKET_PATH.exists()
        except (ProcessLookupError, ValueError):
            return False

    async def _start_daemon(self) -> None:
        """Start daemon process."""
        # Start daemon in background
        subprocess.Popen(
            [sys.executable, "-m", "browser.daemon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )

        # Wait for socket to appear
        start = time.time()
        while time.time() - start < DAEMON_START_TIMEOUT:
            if SOCKET_PATH.exists():
                await asyncio.sleep(0.1)  # Brief extra wait
                return
            await asyncio.sleep(0.1)

        raise RuntimeError("Daemon failed to start within timeout")


def format_output(response: BrowserResponse) -> str:
    """Format response for CLI output."""
    if not response.success:
        return f"Error [{response.error_code}]: {response.error}"

    result = response.result or {}
    lines = []

    for key, value in result.items():
        if key == "accessibility_tree":
            lines.append(f"\nAccessibility Tree:\n{value}")
        elif key == "matches":
            lines.append(f"\nFound {len(value)} matches:")
            for m in value:
                lines.append(f"  [{m['index']}] \"{m['matched_text']}\" @ ({m['bounds']['x']}, {m['bounds']['y']})")
                if m.get('clickable_ancestor_backend_node_id'):
                    lines.append(f"      Click: backend_node_id={m['clickable_ancestor_backend_node_id']}")
                elif m.get('element_backend_node_id'):
                    lines.append(f"      Click: backend_node_id={m['element_backend_node_id']}")
        else:
            lines.append(f"{key}: {value}")

    return "\n".join(lines)


async def async_main(args):
    client = CLIClient()

    command_map = {
        "navigate": Command.NAVIGATE,
        "snapshot": Command.SNAPSHOT,
        "click": Command.CLICK,
        "type": Command.TYPE,
        "scroll": Command.SCROLL,
        "find": Command.FIND,
        "close": Command.CLOSE,
        "status": Command.STATUS,
    }

    if args.command == "daemon":
        if args.daemon_action == "start":
            await client._start_daemon()
            print("Daemon started")
        elif args.daemon_action == "stop":
            if PID_FILE.exists():
                pid = int(PID_FILE.read_text().strip())
                os.kill(pid, 15)  # SIGTERM
                print("Daemon stopped")
            else:
                print("Daemon not running")
        elif args.daemon_action == "status":
            if await client._daemon_running():
                print("Daemon running")
            else:
                print("Daemon not running")
        return

    command = command_map.get(args.command)
    if not command:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        sys.exit(1)

    # Build args dict from CLI args
    cmd_args = {}
    if hasattr(args, 'url') and args.url:
        cmd_args['url'] = args.url
    if hasattr(args, 'text') and args.text:
        cmd_args['text'] = args.text
    if hasattr(args, 'role') and args.role:
        cmd_args['role'] = args.role
    if hasattr(args, 'name') and args.name:
        cmd_args['name'] = args.name
    if hasattr(args, 'backend_node_id') and args.backend_node_id:
        cmd_args['backend_node_id'] = args.backend_node_id
    if hasattr(args, 'direction') and args.direction:
        cmd_args['direction'] = args.direction
    if hasattr(args, 'pixels') and args.pixels:
        cmd_args['pixels'] = args.pixels
    if hasattr(args, 'press_enter') and args.press_enter:
        cmd_args['press_enter'] = args.press_enter
    if hasattr(args, 'exact') and args.exact:
        cmd_args['exact'] = args.exact
    if hasattr(args, 'max_matches') and args.max_matches:
        cmd_args['max_matches'] = args.max_matches

    response = await client.send_command(args.session, command, cmd_args)

    if args.json:
        print(json.dumps({
            "success": response.success,
            "result": response.result,
            "error": response.error,
            "error_code": response.error_code,
            "duration_ms": response.duration_ms
        }, indent=2))
    else:
        print(format_output(response))

    sys.exit(0 if response.success else 1)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Browser automation CLI")
    parser.add_argument("--session", "-s", type=int, required=True, help="Session ID")
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # navigate
    nav = subparsers.add_parser("navigate", help="Navigate to URL")
    nav.add_argument("url", help="URL to navigate to")

    # snapshot
    subparsers.add_parser("snapshot", help="Get page snapshot")

    # click
    click = subparsers.add_parser("click", help="Click element")
    click.add_argument("--backend-node-id", type=int, help="Backend node ID from find")
    click.add_argument("--role", help="Accessibility role")
    click.add_argument("--name", help="Accessibility name")

    # type
    type_cmd = subparsers.add_parser("type", help="Type text")
    type_cmd.add_argument("text", help="Text to type")
    type_cmd.add_argument("--role", help="Target element role")
    type_cmd.add_argument("--name", help="Target element name")
    type_cmd.add_argument("--press-enter", action="store_true", help="Press enter after typing")

    # scroll
    scroll = subparsers.add_parser("scroll", help="Scroll page")
    scroll.add_argument("direction", choices=["up", "down", "top", "bottom"])
    scroll.add_argument("--pixels", type=int, default=500)

    # find
    find = subparsers.add_parser("find", help="Find elements by text")
    find.add_argument("text", help="Text to search for")
    find.add_argument("--exact", action="store_true", help="Exact match")
    find.add_argument("--max-matches", type=int, default=10)

    # close
    subparsers.add_parser("close", help="Close session tab")

    # status
    subparsers.add_parser("status", help="Get daemon status")

    # daemon management
    daemon = subparsers.add_parser("daemon", help="Manage daemon")
    daemon.add_argument("daemon_action", choices=["start", "stop", "status"])

    args = parser.parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
```

### 7. Skill Definition (`~/.claude/skills/browser/skill.md`)

```markdown
---
name: browser
description: Browse websites, click buttons, fill forms, take screenshots. Use when user asks to interact with any web page.
---

# Browser Automation CLI

Control browser via CDP connection to running Chrome.

**Prerequisites:** Chrome running with `--remote-debugging-port=9222`

## Commands

### Navigate to URL
```bash
browser-cli --session {{thread_id}} navigate "{{url}}"
```

### Take Snapshot (screenshot + accessibility tree)
```bash
browser-cli --session {{thread_id}} snapshot
```

### Click Element
By backend_node_id (preferred, from find results):
```bash
browser-cli --session {{thread_id}} click --backend-node-id {{node_id}}
```

By role/name:
```bash
browser-cli --session {{thread_id}} click --role "{{role}}" --name "{{name}}"
```

### Type Text
```bash
browser-cli --session {{thread_id}} type "{{text}}" --role "{{role}}" --name "{{name}}"
```

With enter:
```bash
browser-cli --session {{thread_id}} type "{{text}}" --role "{{role}}" --press-enter
```

### Scroll
```bash
browser-cli --session {{thread_id}} scroll {{direction}}
browser-cli --session {{thread_id}} scroll down --pixels 1000
```

### Find Elements by Text
```bash
browser-cli --session {{thread_id}} find "{{search_text}}"
```

Returns backend_node_id values for precise clicking.

### Close Session Tab
```bash
browser-cli --session {{thread_id}} close
```

### Daemon Management
```bash
browser-cli daemon status
browser-cli daemon stop
```

## Workflow

1. Navigate to page
2. Use `snapshot` or `find` to locate elements
3. Use `click`/`type` to interact
4. Screenshots in results show current state

## Output

All commands return:
- `screenshot_path` - Path to PNG screenshot
- Command-specific data (url, title, matches, etc.)

Use `--json` flag for machine-readable output.

## Logs

Session logs: `~/.browser-cli/logs/session_{{thread_id}}.log`
Daemon logs: `~/.browser-cli/logs/daemon.log`
Errors: `~/.browser-cli/logs/errors.log`
```

---

## Edge Cases

| Scenario | Handling |
|----------|----------|
| Chrome not running | `ErrorCode.CHROME_NOT_RUNNING` with clear message |
| CDP endpoint unreachable | Retry once, then `ErrorCode.CDP_CONNECTION_FAILED` |
| Tab closed externally | Detect on next command, auto-create new tab |
| Daemon crashes | CLI detects socket gone, restarts daemon, retries once |
| Stale PID file | Check process exists before trusting PID |
| Socket permission denied | Socket created with 0600 permissions |
| Multiple simultaneous commands same session | Sequential execution (daemon handles one at a time per connection) |
| Very long accessibility tree | Truncate at 15KB |
| Screenshot dir full | Error with clear message about disk space |
| Daemon idle timeout | Optional: close after 30min inactivity |
| Session not closed on bot shutdown | Daemon tracks activity, can close stale tabs |

---

## Testing Strategy

### Unit Tests (`tests/browser/`)

```
test_protocol.py      # Serialization/deserialization
test_logger.py        # Log format, file creation
test_handlers.py      # Command handlers with mocked CDP
```

### Integration Tests

```
test_daemon.py        # Daemon start/stop/restart
test_cli.py           # CLI → Daemon round-trip
test_cdp_client.py    # Real Chrome connection (requires Chrome)
```

### Test Structure

```python
# tests/browser/test_protocol.py
import pytest
from browser.protocol import BrowserRequest, Command

def test_request_serialization():
    req = BrowserRequest.create(123, Command.NAVIGATE, {"url": "https://example.com"})
    assert req.session_id == 123
    assert req.command == Command.NAVIGATE
    assert req.args["url"] == "https://example.com"

# tests/browser/test_handlers.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from browser.handlers import CommandHandler, CommandError
from browser.protocol import Command, ErrorCode

@pytest.fixture
def mock_cdp():
    cdp = MagicMock()
    cdp.get_or_create_page = AsyncMock()
    cdp.get_screenshot_path = MagicMock(return_value="/tmp/test.png")
    return cdp

@pytest.mark.asyncio
async def test_navigate_success(mock_cdp):
    page = AsyncMock()
    page.url = "https://example.com"
    page.title = AsyncMock(return_value="Example")
    page.goto = AsyncMock()
    page.screenshot = AsyncMock()
    mock_cdp.get_or_create_page.return_value = page

    handler = CommandHandler(mock_cdp)
    result = await handler.execute(123, Command.NAVIGATE, {"url": "https://example.com"})

    assert result["url"] == "https://example.com"
    assert result["title"] == "Example"
    page.goto.assert_called_once()

@pytest.mark.asyncio
async def test_navigate_missing_url(mock_cdp):
    handler = CommandHandler(mock_cdp)

    with pytest.raises(CommandError) as exc:
        await handler.execute(123, Command.NAVIGATE, {})

    assert exc.value.code == ErrorCode.INVALID_ARGS

# tests/browser/test_cli.py (subprocess tests)
import subprocess
import json

def test_cli_json_output():
    result = subprocess.run(
        ["python", "-m", "browser.cli", "--session", "999", "--json", "status"],
        capture_output=True,
        text=True
    )
    output = json.loads(result.stdout)
    assert "success" in output
```

---

## File Structure

**Standalone CLI project** (matches gphotos-cli, gdoc-cli pattern):

```
~/Projects/browser-cli/
├── browser_cli.py        # CLI entry point (installed as `browser-cli`)
├── browser_daemon.py     # Unix socket server
├── protocol.py           # Types, request/response, error codes
├── logger.py             # Structured logging
├── cdp_client.py         # CDP connection management
├── handlers.py           # Command execution logic
├── requirements.txt      # websockets, etc.
├── setup.py              # For `pip install -e .` → `browser-cli` command
├── tests/
│   ├── __init__.py
│   ├── test_protocol.py
│   ├── test_logger.py
│   ├── test_handlers.py
│   ├── test_daemon.py
│   ├── test_cli.py
│   └── conftest.py
└── README.md

~/.claude/skills/browser/
└── skill.md              # Skill definition (user-level, works across projects)
```

**Note:** This is a standalone project, NOT part of tele-bot. The vendored `cdp_browser` code
from tele-bot will be copied/moved here since it's the only consumer.

---

## Migration Path

1. Create `~/Projects/browser-cli/` project
2. Move `vendor/cdp_browser/` from tele-bot → browser-cli
3. Build daemon + CLI
4. Test CLI independently: `browser-cli --session 999 navigate https://example.com`
5. Create skill at `~/.claude/skills/browser/skill.md`
6. In tele-bot:
   - Remove `browser_tools.py`
   - Remove `vendor/cdp_browser/`
   - Remove MCP registration from `session.py`
   - Remove browser-related entries from `tool_allowlist.json`
   - Clean up imports in `config.py` (BROWSER_* vars no longer needed there)

---

## What I May Have Missed

1. **Screenshot cleanup** - Old screenshots accumulate. Add periodic cleanup?
2. **Max sessions limit** - Prevent runaway tab creation?
3. **Healthcheck endpoint** - For monitoring?
4. **Config file** - Instead of just env vars?
5. **Graceful degradation** - If daemon dies mid-command, should CLI retry?

Let me know what else you want addressed.
