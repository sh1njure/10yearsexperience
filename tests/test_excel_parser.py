"""Tests for robust CSV reading (delimiter + encoding auto-detection)."""
from pathlib import Path

from app import excel_parser as ep


def _write(tmp_path, name, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_semicolon_cp1252(tmp_path):
    p = _write(tmp_path, "s.csv",
               "SKU;Nom;Prix\nA1;Café;9,99\n".encode("cp1252"))
    table = ep.parse(p, header_row=0)
    assert table.headers == ["SKU", "Nom", "Prix"]
    assert table.rows[0]["Nom"] == "Café"


def test_comma_utf8(tmp_path):
    p = _write(tmp_path, "c.csv",
               "SKU,Name,Price\nX1,Blue,3.50\n".encode("utf-8"))
    table = ep.parse(p, header_row=0)
    assert table.headers == ["SKU", "Name", "Price"]
    assert table.rows[0]["Price"] == "3.50"


def test_tab_delimited(tmp_path):
    p = _write(tmp_path, "t.csv", b"SKU\tName\nT1\tThing\n")
    table = ep.parse(p, header_row=0)
    assert table.headers == ["SKU", "Name"]


def test_header_row_offset(tmp_path):
    # Two junk rows above the real header.
    p = _write(tmp_path, "o.csv",
               b"Supplier export\n\nSKU,Name\nX1,Blue\n")
    table = ep.parse(p, header_row=2)
    assert table.headers == ["SKU", "Name"]
    assert table.rows[0]["SKU"] == "X1"
