"""转换器共享的纯本地工具函数。"""

from __future__ import annotations

import codecs
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from ._exceptions import (
    DocumentConversionError,
    DocumentTooLargeError,
    MissingOptionalDependencyError,
    UnsafeInputError,
)


def optional_import(module: str, package: str, format_name: str):
    """延迟导入格式依赖，并将 ImportError 转成可读异常。"""

    try:
        return __import__(module, fromlist=["*"])
    except ImportError as exc:
        raise MissingOptionalDependencyError(package, format_name) from exc


def validate_local_path(
    value: Path | str,
    *,
    max_input_bytes: int,
) -> Path:
    """只接受本地文件，不接受 URL、URI 或目录。"""

    if not isinstance(value, (str, Path)):
        raise TypeError("输入必须是文件路径")
    raw = str(value).strip()
    if not raw:
        raise ValueError("输入路径不能为空")
    parsed = urlparse(raw)
    windows_drive_path = len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha()
    if (parsed.scheme and not windows_drive_path) or raw.startswith(("//", "\\\\")):
        raise UnsafeInputError(
            "文档归一化层只处理本地普通文件；URL、网络链接和远程资源请先由上游智能体处理。"
        )
    if "\x00" in raw:
        raise UnsafeInputError("输入路径包含非法空字节")
    path = Path(raw).expanduser().resolve(strict=False)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{path}")
    if not path.is_file():
        raise DocumentConversionError(f"路径不是普通文件：{path}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise DocumentConversionError(f"无法读取文件信息：{path}: {exc}") from exc
    if max_input_bytes > 0 and size > max_input_bytes:
        raise DocumentTooLargeError(
            f"文档超过转换层大小上限：{size} bytes > {max_input_bytes} bytes：{path}"
        )
    try:
        with path.open("rb") as stream:
            stream.read(1)
    except OSError as exc:
        raise DocumentConversionError(f"文件不可读：{path}: {exc}") from exc
    return path


def read_text(path: Path) -> str:
    """读取常见文本编码，并拒绝明显的二进制/乱码结果。"""

    try:
        data = path.read_bytes()
    except OSError as exc:
        raise DocumentConversionError(f"无法读取文件：{path}: {exc}") from exc
    if not data:
        return ""

    bom_candidates = (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    )
    for bom, encoding in bom_candidates:
        if data.startswith(bom):
            try:
                return data.decode(encoding)
            except UnicodeError:
                break

    try:
        return data.decode("utf-8")
    except UnicodeError:
        pass

    candidates: list[tuple[float, int, str, str]] = []
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(data).best()
        if best is not None and best.encoding:
            guessed = best.encoding.casefold().replace("-", "_")
            if not guessed.startswith(("utf_16", "utf_32")):
                candidates.append(
                    (_decoded_text_penalty(str(best)), 0, guessed, str(best))
                )
    except Exception:
        pass

    for priority, encoding in enumerate(
        ("gb18030", "big5", "shift_jis", "cp1250", "cp1252"),
        start=1,
    ):
        try:
            decoded = data.decode(encoding)
        except UnicodeError:
            continue
        candidates.append(
            (_decoded_text_penalty(decoded), priority, encoding, decoded)
        )
    if not candidates:
        raise DocumentConversionError(f"无法识别文本编码：{path}")
    penalty, _priority, _encoding, decoded = _select_text_candidate(
        data,
        candidates,
    )
    if penalty >= 0.35:
        raise DocumentConversionError(f"文件疑似二进制或编码已损坏：{path}")
    return decoded


def decoded_text_penalty(text: str) -> float:
    return _decoded_text_penalty(text)


def _decoded_text_penalty(text: str) -> float:
    if not text:
        return 0.0
    controls = sum(
        1
        for char in text
        if unicodedata.category(char) == "Cc" and char not in "\n\r\t"
    )
    replacement = text.count("\ufffd")
    mojibake = sum(text.count(marker) for marker in ("锟斤拷", "ï»¿", "Ã", "Â"))
    private_use = sum("\ue000" <= char <= "\uf8ff" for char in text)
    unassigned = sum(unicodedata.category(char) == "Cn" for char in text)
    score = (
        controls * 5
        + replacement * 10
        + mojibake * 3
        + private_use * 4
        + unassigned * 5
    )
    return score / max(len(text), 1)


def _select_text_candidate(
    data: bytes,
    candidates: list[tuple[float, int, str, str]],
) -> tuple[float, int, str, str]:
    """在无 BOM 的兼容编码候选中做稳定、保守的选择。"""

    ordered = sorted(candidates, key=lambda item: (item[0], item[1]))
    usable = [candidate for candidate in ordered if candidate[0] < 0.35]
    if not usable:
        return ordered[0]

    best_penalty = usable[0][0]
    plausible = [
        candidate
        for candidate in usable
        if candidate[0] <= best_penalty + 0.02
    ]
    by_encoding: dict[str, tuple[float, int, str, str]] = {}
    for candidate in plausible:
        by_encoding.setdefault(candidate[2], candidate)

    shift_jis = by_encoding.get("shift_jis")
    if shift_jis is not None and _is_halfwidth_shift_jis_stream(data):
        return shift_jis

    detected = min(plausible, key=lambda item: item[1])
    if detected[2] in {"cp932", "shift_jis"}:
        big5 = by_encoding.get("big5")
        if big5 is not None and _looks_like_big5_cp932_mojibake(
            detected[3],
            big5[3],
        ):
            return big5
        return detected

    if detected[2] in {"cp1250", "cp1252"}:
        cp1250 = by_encoding.get("cp1250")
        cp1252 = by_encoding.get("cp1252")
        if cp1250 is not None and cp1252 is not None:
            return (
                cp1250
                if _contains_central_european_signal(cp1250[3])
                else cp1252
            )

    return ordered[0]


def _is_halfwidth_shift_jis_stream(data: bytes) -> bool:
    """识别仅由 ASCII 与 Shift-JIS 半角片假名字节组成的文本。"""

    has_halfwidth = False
    for value in data:
        if value <= 0x7F:
            continue
        if 0xA1 <= value <= 0xDF:
            has_halfwidth = True
            continue
        return False
    return has_halfwidth


def _looks_like_big5_cp932_mojibake(cp932_text: str, big5_text: str) -> bool:
    halfwidth = sum("\uff61" <= char <= "\uff9f" for char in cp932_text)
    cp932_cjk = sum("\u3400" <= char <= "\u9fff" for char in cp932_text)
    big5_cjk = sum("\u3400" <= char <= "\u9fff" for char in big5_text)
    return halfwidth >= 8 and halfwidth > cp932_cjk * 2 and big5_cjk >= 4


def _contains_central_european_signal(text: str) -> bool:
    signals = frozenset(
        "ĄĆČĎĘĚŁŃŇŐŘŚŠŤŮŰŹŻŽąćčďęěłńňőřśšťůűźżž"
    )
    return any(character in signals for character in text)


def finalize_markdown(markdown: str, source: Path) -> str:
    text = unicodedata.normalize("NFC", str(markdown))
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = "".join(
        char for char in text if unicodedata.category(char) != "Cc" or char in "\n\t"
    )
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
    if not text:
        raise DocumentConversionError(f"转换结果为空：{source}")
    return text + "\n"


def title_from_path(path: Path) -> str:
    return path.stem.replace("_", " ").replace("-", " ").strip() or path.name


def markdown_table(rows: Iterable[Iterable[object]]) -> str:
    normalized_rows = [
        [escape_markdown_cell(str(value)) for value in row] for row in rows
    ]
    normalized_rows = [row for row in normalized_rows if any(cell for cell in row)]
    if not normalized_rows:
        return ""
    width = max(len(row) for row in normalized_rows)
    header = normalized_rows[0] + [""] * (width - len(normalized_rows[0]))
    body = [row + [""] * (width - len(row)) for row in normalized_rows[1:]]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def escape_markdown_cell(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", "")
        .replace("\n", "<br>")
        .strip()
    )


def escape_heading(value: str) -> str:
    return str(value).replace("\n", " ").strip() or "未命名工作表"


def spreadsheet_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def trim_trailing_empty(row: list[str]) -> list[str]:
    while row and not row[-1].strip():
        row.pop()
    return row


def repeated_pdf_edge_lines(pages: list[str]) -> set[str]:
    if len(pages) < 3:
        return set()
    candidates: Counter[str] = Counter()
    for page in pages:
        lines = [line.strip() for line in page.splitlines() if line.strip()]
        for line in set(lines[:2] + lines[-2:]):
            if 2 <= len(line) <= 160:
                candidates[line] += 1
    threshold = max(3, (len(pages) + 1) // 2)
    return {line for line, count in candidates.items() if count >= threshold}


def clean_pdf_page(page: str, repeated_edges: set[str]) -> str:
    lines = [line.strip() for line in page.replace("\u00a0", " ").splitlines()]
    lines = [line for line in lines if line and line not in repeated_edges]
    text = "\n".join(lines)
    text = re.sub(r"(?<=[A-Za-z])-[ \t]*\n(?=[a-z])", "", text)
    return text.strip()


__all__ = [
    "clean_pdf_page",
    "escape_heading",
    "escape_markdown_cell",
    "finalize_markdown",
    "markdown_table",
    "optional_import",
    "read_text",
    "repeated_pdf_edge_lines",
    "spreadsheet_value",
    "title_from_path",
    "trim_trailing_empty",
    "validate_local_path",
]
