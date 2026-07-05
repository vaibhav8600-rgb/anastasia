"""Tool tests that don't touch the real GUI/keyboard/screen."""

from pathlib import Path

from app.config import AppConfig
from app.tools import ToolContext
from app.tools.browser import build_target
from app.tools.file_tools import delete_files, resolve_safe_folder, search_files
from app.tools.open_app import resolve_app


def cfg(**overrides) -> AppConfig:
    return AppConfig(**overrides)


# ---- open_app alias resolution ------------------------------------------

def test_resolve_known_aliases():
    c = cfg()
    assert resolve_app("chrome", c).endswith("chrome.exe")
    assert resolve_app("vs code", c) == "code"
    assert resolve_app("VS Code", c) == "code"
    assert resolve_app("file explorer", c) == "explorer.exe"
    assert resolve_app("calc", c) == "calc.exe"
    assert resolve_app("paint", c) == "mspaint.exe"
    assert resolve_app("mspaint", c) == "mspaint.exe"


def test_resolve_unknown_app():
    assert resolve_app("definitely-not-an-app", cfg()) is None
    assert resolve_app("", cfg()) is None


# ---- safe folder resolution ------------------------------------------------

def test_resolve_safe_folder_by_name(tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    c = cfg(safe_folders=[str(downloads)])
    assert resolve_safe_folder("downloads", c) == downloads
    assert resolve_safe_folder("Downloads folder", c) == downloads


def test_resolve_safe_folder_rejects_outside(tmp_path):
    c = cfg(safe_folders=[str(tmp_path / "Downloads")])
    assert resolve_safe_folder("C:/Windows/System32", c) is None
    assert resolve_safe_folder("secret place", c) is None


def test_resolve_safe_folder_accepts_subpath(tmp_path):
    downloads = tmp_path / "Downloads"
    sub = downloads / "invoices"
    sub.mkdir(parents=True)
    c = cfg(safe_folders=[str(downloads)])
    assert resolve_safe_folder(str(sub), c) == sub


# ---- search_files -------------------------------------------------------------

def test_search_files_finds_matches(tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "invoice_march.pdf").write_text("x")
    (downloads / "photo.png").write_text("x")
    (downloads / "sub").mkdir()
    (downloads / "sub" / "old_invoice.docx").write_text("x")

    ctx = ToolContext(config=cfg(safe_folders=[str(downloads)]))
    result = search_files({"folder": "downloads", "query": "invoice"}, ctx)
    assert result.success
    assert len(result.data) == 2


def test_search_files_outside_safe_folder_fails(tmp_path):
    ctx = ToolContext(config=cfg(safe_folders=[str(tmp_path)]))
    result = search_files({"folder": "C:/Windows", "query": "kernel"}, ctx)
    assert not result.success


def test_search_files_requires_query(tmp_path):
    ctx = ToolContext(config=cfg(safe_folders=[str(tmp_path)]))
    assert not search_files({"folder": str(tmp_path)}, ctx).success


# ---- delete stub ----------------------------------------------------------------

def test_delete_files_is_disabled_stub(tmp_path):
    victim = tmp_path / "precious.txt"
    victim.write_text("do not delete")
    ctx = ToolContext(config=cfg(safe_folders=[str(tmp_path)]))
    result = delete_files({"target_path": str(victim)}, ctx)
    assert not result.success
    assert victim.exists()  # nothing was deleted


# ---- browser URL building ----------------------------------------------------------

def test_build_target_url():
    assert build_target({"url": "youtube.com"}) == "https://youtube.com"
    assert build_target({"url": "https://github.com"}) == "https://github.com"


def test_build_target_query_becomes_search():
    t = build_target({"query": "python virtual environment"})
    assert t.startswith("https://www.google.com/search?q=")
    assert "python+virtual+environment" in t


def test_build_target_domain_query_becomes_url():
    assert build_target({"query": "example.com"}) == "https://example.com"


def test_build_target_empty():
    assert build_target({}) is None
