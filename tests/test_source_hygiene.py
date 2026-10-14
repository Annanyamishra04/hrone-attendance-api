"""Source-file hygiene. A string literal containing the escape \\ud800 in a NON-raw string is an actual lone surrogate character:
it cannot be encoded as UTF-8, which breaks tools that read docstrings or write the source back out. To talk about the
escape in a docstring, double the backslash ("\\ud800"). Tests that need the hostile input send it as BYTES
(b'{"emp_code": "\\ud800"}') so that the JSON parser, not Python's source parser, creates the surrogate."""
import ast
import pathlib
import py_compile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCES = sorted(p for d in ("app", "tests") for p in (ROOT / d).rglob("*.py"))


def _has_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_string_constant_contains_a_lone_surrogate(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad = [n.lineno for n in ast.walk(tree)
           if isinstance(n, ast.Constant) and isinstance(n.value, str) and _has_surrogate(n.value)]
    assert not bad, f"{path.name}: lone surrogate in a string literal at line(s) {bad}; double the backslash"


def test_main_py_compiles_and_its_docstrings_are_utf8_encodable(tmp_path):
    py_compile.compile(str(ROOT / "app" / "main.py"), cfile=str(tmp_path / "main.pyc"), doraise=True)
    tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            (ast.get_docstring(node, clean=False) or "").encode("utf-8")   # raises UnicodeEncodeError on a surrogate
