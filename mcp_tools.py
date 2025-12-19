"""
MCP tools for Telegram integration.

Provides custom tools that Claude can use to interact with Telegram,
such as sending files to the chat.
"""
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import create_sdk_mcp_server, tool

if TYPE_CHECKING:
    from session import ClaudeSession

# Telegram bot file size limit (50 MB)
FILE_SIZE_LIMIT = 50 * 1024 * 1024


def validate_file_path(file_path: str, cwd: str) -> tuple[bool, str, Path]:
    """Validate and resolve file path for security.

    Ensures the file:
    - Exists and is a regular file
    - Is within the session's cwd or system temp directory

    Args:
        file_path: Path provided by Claude (absolute or relative)
        cwd: Session's working directory

    Returns:
        Tuple of (is_valid, error_message, resolved_path)
    """
    path = Path(file_path)
    cwd_path = Path(cwd).resolve()

    # Resolve relative paths against cwd
    if not path.is_absolute():
        path = cwd_path / path

    resolved = path.resolve()

    # Check file exists
    if not resolved.exists():
        return False, f"File not found: {file_path}", resolved

    if not resolved.is_file():
        return False, f"Not a file: {file_path}", resolved

    # Security check: file must be within cwd or temp directory
    temp_dir = Path(tempfile.gettempdir()).resolve()

    if not (resolved.is_relative_to(cwd_path) or resolved.is_relative_to(temp_dir)):
        return False, "Access denied: file must be within project directory or temp folder", resolved

    return True, "", resolved


def create_telegram_mcp_server(session: "ClaudeSession"):
    """Create an MCP server with Telegram tools bound to a session.

    The server runs in-process and has access to the session's bot,
    chat_id, and thread_id via closure.

    Args:
        session: The ClaudeSession to bind tools to

    Returns:
        McpSdkServerConfig ready to use with ClaudeAgentOptions.mcp_servers
    """

    @tool(
        "send_to_telegram",
        "Send a file to the Telegram chat. Use this when the user asks you to share a file, "
        "send output as a file, or when a file would be more useful than inline text "
        "(e.g., large outputs, generated images, code files). "
        "The file must exist within the project directory.",
        {
            "file_path": str,
            "caption": str,
        }
    )
    async def send_to_telegram(args: dict[str, Any]) -> dict[str, Any]:
        """Send a file to the Telegram chat."""
        file_path = args.get("file_path", "")
        caption = args.get("caption", "")

        # Validate inputs
        if not file_path:
            return {
                "content": [{"type": "text", "text": "Error: file_path is required"}],
                "is_error": True
            }

        # Validate and resolve file path
        is_valid, error_msg, resolved_path = validate_file_path(file_path, session.cwd)
        if not is_valid:
            return {
                "content": [{"type": "text", "text": f"Error: {error_msg}"}],
                "is_error": True
            }

        # Check file size
        file_size = resolved_path.stat().st_size
        if file_size > FILE_SIZE_LIMIT:
            size_mb = file_size / 1024 / 1024
            return {
                "content": [{"type": "text", "text": f"Error: File size ({size_mb:.1f} MB) exceeds Telegram's 50 MB limit"}],
                "is_error": True
            }

        # Check bot reference
        if session.bot is None:
            return {
                "content": [{"type": "text", "text": "Error: No bot reference available"}],
                "is_error": True
            }

        # Log the tool usage
        if session.logger:
            session.logger.log_tool_call("send_to_telegram", {
                "file_path": str(resolved_path),
                "caption": caption,
                "size_bytes": file_size
            })

        # Send the document
        try:
            message = await session.bot.send_document(
                chat_id=session.chat_id,
                message_thread_id=session.thread_id,
                document=resolved_path,
                caption=caption if caption else None,
                filename=resolved_path.name
            )

            # Log success
            if session.logger:
                session.logger.log_tool_result(
                    "send_to_telegram",
                    f"Sent {resolved_path.name} (message_id={message.message_id})",
                    success=True
                )

            return {
                "content": [{
                    "type": "text",
                    "text": f"Successfully sent file '{resolved_path.name}' to the chat"
                }]
            }

        except Exception as e:
            error_text = f"Failed to send file: {str(e)}"

            if session.logger:
                session.logger.log_tool_result("send_to_telegram", error_text, success=False)
                session.logger.log_error("send_to_telegram", e)

            return {
                "content": [{"type": "text", "text": f"Error: {error_text}"}],
                "is_error": True
            }

    return create_sdk_mcp_server(
        name="telegram-tools",
        version="1.0.0",
        tools=[send_to_telegram]
    )
