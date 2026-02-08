"""Tests for _format_plan_approval_message."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from session import _format_plan_approval_message


def _make_mock_platform(max_message_length: int = 4000) -> MagicMock:
    platform = MagicMock()
    platform.max_message_length = max_message_length
    return platform


@pytest.mark.asyncio
async def test_format_plan_found(tmp_path: Path) -> None:
    """When a plan file exists, it should be formatted and returned."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    plan_file = plan_dir / "test-plan.md"
    plan_file.write_text("# My Plan\n\nDo the thing.")

    platform = _make_mock_platform()
    result = await _format_plan_approval_message(platform, plan_dir=plan_dir)

    assert "📋 **Plan Approval**" in result
    assert "test-plan.md" in result
    assert "# My Plan" in result
    assert "Do the thing." in result


@pytest.mark.asyncio
async def test_format_no_plan_dir(tmp_path: Path) -> None:
    """When the plan directory doesn't exist, return fallback message."""
    nonexistent = tmp_path / "nonexistent"

    platform = _make_mock_platform()
    result = await _format_plan_approval_message(platform, plan_dir=nonexistent)

    assert "📋 **Plan Approval**" in result
    assert "Could not find plan file" in result


@pytest.mark.asyncio
async def test_format_empty_plan_dir(tmp_path: Path) -> None:
    """When the plan directory exists but is empty, return fallback message."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()

    platform = _make_mock_platform()
    result = await _format_plan_approval_message(platform, plan_dir=plan_dir)

    assert "Could not find plan file" in result


@pytest.mark.asyncio
async def test_format_picks_most_recent_plan(tmp_path: Path) -> None:
    """When multiple plans exist, the most recently modified is used."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()

    old_plan = plan_dir / "old-plan.md"
    old_plan.write_text("# Old Plan")

    # Ensure different mtime
    time.sleep(0.01)

    new_plan = plan_dir / "new-plan.md"
    new_plan.write_text("# New Plan")

    platform = _make_mock_platform()
    result = await _format_plan_approval_message(platform, plan_dir=plan_dir)

    assert "new-plan.md" in result
    assert "# New Plan" in result
    assert "old-plan.md" not in result


@pytest.mark.asyncio
async def test_format_truncates_long_plan(tmp_path: Path) -> None:
    """Plan content is truncated to fit platform message limits."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    plan_file = plan_dir / "long-plan.md"
    plan_file.write_text("x" * 5000)

    # max_message_length of 1000 means plan truncates at 800 (1000 - 200)
    platform = _make_mock_platform(max_message_length=1000)
    result = await _format_plan_approval_message(platform, plan_dir=plan_dir)

    assert "... (truncated)" in result
    # Header + filename + content should fit within limits
    # The truncated content portion should be around 800 chars
    content_portion = result.split("\n\n", 1)[1] if "\n\n" in result else result
    assert len(content_portion) < 1000
