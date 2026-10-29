from app import config


def test_mysql_url_is_built_from_db_values_with_special_characters(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for k, v in {"DB_HOST": "localhost", "DB_PORT": "3306", "DB_USER": "root", "DB_PASSWORD": "p@ss:w/rd#1", "DB_NAME": "scanner_db"}.items():
        monkeypatch.setenv(k, v)
    url = config.database_url()
    assert (url.drivername, url.host, url.port, url.database, url.password) == ("mysql+pymysql", "localhost", 3306, "scanner_db", "p@ss:w/rd#1")
    assert "p@ss" not in url.render_as_string(hide_password=True)


def test_database_url_wins_and_sqlite_is_the_fallback(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./x.db")
    monkeypatch.setenv("DB_NAME", "ignored")
    assert config.database_url() == "sqlite:///./x.db"
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.delenv("DB_NAME")
    assert config.database_url() == "sqlite:///./crypto_scanner.db"
