from pathlib import Path

from gridcast.config import load_settings


def test_load_settings_prefers_environment_over_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("FINGRID_API_KEY=file-value\n", encoding="utf-8")

    settings = load_settings(tmp_path, {"FINGRID_API_KEY": "process-value"})

    assert settings.api_key == "process-value"
    assert settings.database_path == tmp_path / "data" / "gridcast.duckdb"


def test_load_settings_supports_windows_text_extension(tmp_path: Path) -> None:
    (tmp_path / ".env.txt").write_text("FINGRID_API_KEY=test-value\n", encoding="utf-8")

    settings = load_settings(tmp_path, {})

    assert settings.api_key == "test-value"
