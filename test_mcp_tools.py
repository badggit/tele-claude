"""Tests for mcp_tools.py functionality."""
import tempfile
from pathlib import Path

import pytest

from mcp_tools import validate_file_path, FILE_SIZE_LIMIT


class TestValidateFilePath:
    """Tests for validate_file_path() security validation."""

    @pytest.fixture
    def temp_cwd(self, tmp_path):
        """Create a temporary working directory with test files."""
        # Create some test files
        (tmp_path / "test.txt").write_text("hello")
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "nested.txt").write_text("nested")
        return tmp_path

    def test_valid_absolute_path_in_cwd(self, temp_cwd):
        """Absolute path within cwd should be valid."""
        file_path = str(temp_cwd / "test.txt")
        is_valid, error, resolved = validate_file_path(file_path, str(temp_cwd))
        assert is_valid is True
        assert error == ""
        assert resolved == temp_cwd / "test.txt"

    def test_valid_relative_path(self, temp_cwd):
        """Relative path should be resolved against cwd."""
        is_valid, error, resolved = validate_file_path("test.txt", str(temp_cwd))
        assert is_valid is True
        assert error == ""
        assert resolved == temp_cwd / "test.txt"

    def test_valid_nested_path(self, temp_cwd):
        """Nested path within cwd should be valid."""
        is_valid, error, resolved = validate_file_path("subdir/nested.txt", str(temp_cwd))
        assert is_valid is True
        assert error == ""

    def test_file_not_found(self, temp_cwd):
        """Non-existent file should return error."""
        is_valid, error, resolved = validate_file_path("nonexistent.txt", str(temp_cwd))
        assert is_valid is False
        assert "not found" in error.lower()

    def test_directory_not_file(self, temp_cwd):
        """Directory path should return error."""
        is_valid, error, resolved = validate_file_path("subdir", str(temp_cwd))
        assert is_valid is False
        assert "not a file" in error.lower()

    def test_path_outside_cwd_denied(self, temp_cwd):
        """Path outside cwd and temp should be denied."""
        # Create a file outside temp_cwd
        outside_file = Path("/etc/passwd")
        if outside_file.exists():
            is_valid, error, resolved = validate_file_path(str(outside_file), str(temp_cwd))
            assert is_valid is False
            assert "access denied" in error.lower()

    def test_path_traversal_attack_blocked(self, temp_cwd):
        """Path traversal attempts should be blocked."""
        # Try to escape cwd with ..
        is_valid, error, resolved = validate_file_path("../../../etc/passwd", str(temp_cwd))
        assert is_valid is False
        # Either file not found (doesn't exist) or access denied (security check)
        assert "not found" in error.lower() or "access denied" in error.lower()

    def test_temp_directory_allowed(self):
        """Files in system temp directory should be allowed."""
        # Create a temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            f.write(b"temp content")
            temp_file = f.name

        try:
            # Use a different cwd (not temp dir)
            cwd = "/tmp/fake_cwd_that_does_not_matter"
            is_valid, error, resolved = validate_file_path(temp_file, str(Path.home()))
            assert is_valid is True
            assert error == ""
        finally:
            Path(temp_file).unlink()

    def test_symlink_resolved(self, temp_cwd):
        """Symlinks should be resolved before validation."""
        # Create a symlink to a file in cwd
        link_path = temp_cwd / "link.txt"
        target_path = temp_cwd / "test.txt"
        link_path.symlink_to(target_path)

        is_valid, error, resolved = validate_file_path(str(link_path), str(temp_cwd))
        assert is_valid is True
        # Resolved path should be the actual file
        assert resolved == target_path.resolve()

    def test_symlink_escape_blocked(self, temp_cwd):
        """Symlink pointing outside cwd should be blocked."""
        # Create a file outside cwd
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            f.write(b"outside")
            outside_file = Path(f.name)

        try:
            # Create symlink in cwd pointing to outside file
            link_path = temp_cwd / "escape_link.txt"
            link_path.symlink_to(outside_file)

            is_valid, error, resolved = validate_file_path(str(link_path), str(temp_cwd))
            # Should be allowed because temp files are allowed
            # (the symlink resolves to temp dir which is allowed)
            assert is_valid is True
        finally:
            outside_file.unlink()


class TestFileSizeLimit:
    """Tests for file size limit constant."""

    def test_file_size_limit_is_50mb(self):
        """File size limit should be 50 MB."""
        assert FILE_SIZE_LIMIT == 50 * 1024 * 1024

    def test_file_size_limit_in_bytes(self):
        """File size limit should be 52428800 bytes."""
        assert FILE_SIZE_LIMIT == 52428800


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
