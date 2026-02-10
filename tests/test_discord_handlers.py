"""Tests for Discord handler functions."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from platforms.discord.listener import _normalize_name, resolve_project_for_channel


class TestNormalizeName:
    """Tests for _normalize_name function."""

    def test_lowercase(self):
        assert _normalize_name("MyProject") == "myproject"

    def test_underscore_to_dash(self):
        assert _normalize_name("my_project") == "my-project"

    def test_space_to_dash(self):
        assert _normalize_name("my project") == "my-project"

    def test_combined(self):
        assert _normalize_name("My_Cool Project") == "my-cool-project"

    def test_already_normalized(self):
        assert _normalize_name("my-project") == "my-project"

    def test_multiple_underscores(self):
        assert _normalize_name("my__project") == "my--project"


class TestResolveProjectForChannel:
    """Tests for resolve_project_for_channel function."""

    def test_exact_match(self, tmp_path: Path):
        """Channel name exactly matches folder name."""
        (tmp_path / "my-project").mkdir()
        with patch("platforms.discord.listener.PROJECTS_DIR", tmp_path):
            result = resolve_project_for_channel("my-project")
            assert result == str(tmp_path / "my-project")

    def test_underscore_folder_matches_dash_channel(self, tmp_path: Path):
        """Folder with underscores matches channel with dashes."""
        (tmp_path / "my_project").mkdir()
        with patch("platforms.discord.listener.PROJECTS_DIR", tmp_path):
            result = resolve_project_for_channel("my-project")
            assert result == str(tmp_path / "my_project")

    def test_case_insensitive(self, tmp_path: Path):
        """Matching is case-insensitive."""
        (tmp_path / "MyProject").mkdir()
        with patch("platforms.discord.listener.PROJECTS_DIR", tmp_path):
            result = resolve_project_for_channel("myproject")
            assert result == str(tmp_path / "MyProject")

    def test_no_match_returns_none(self, tmp_path: Path):
        """Returns None when no matching folder exists."""
        (tmp_path / "other-project").mkdir()
        with patch("platforms.discord.listener.PROJECTS_DIR", tmp_path):
            result = resolve_project_for_channel("my-project")
            assert result is None

    def test_hidden_folders_ignored(self, tmp_path: Path):
        """Folders starting with . are ignored."""
        (tmp_path / ".my-project").mkdir()
        with patch("platforms.discord.listener.PROJECTS_DIR", tmp_path):
            result = resolve_project_for_channel("my-project")
            assert result is None

    def test_files_ignored(self, tmp_path: Path):
        """Files are ignored, only directories match."""
        (tmp_path / "my-project").touch()  # file, not directory
        with patch("platforms.discord.listener.PROJECTS_DIR", tmp_path):
            result = resolve_project_for_channel("my-project")
            assert result is None

    def test_nonexistent_projects_dir(self, tmp_path: Path):
        """Returns None if PROJECTS_DIR doesn't exist."""
        nonexistent = tmp_path / "nonexistent"
        with patch("platforms.discord.listener.PROJECTS_DIR", nonexistent):
            result = resolve_project_for_channel("my-project")
            assert result is None

    def test_space_in_folder_name(self, tmp_path: Path):
        """Folder with spaces matches channel with dashes."""
        (tmp_path / "my project").mkdir()
        with patch("platforms.discord.listener.PROJECTS_DIR", tmp_path):
            result = resolve_project_for_channel("my-project")
            assert result == str(tmp_path / "my project")
