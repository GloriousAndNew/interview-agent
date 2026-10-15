"""Local text extraction; no Office installation, macros, or remote fetches."""
from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile


class DocumentImportError(ValueError):
    pass


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class _PlainXML(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise DocumentImportError("Word 文档包含不支持的 XML 声明，请另存为普通 .docx 后上传。")


def _paragraph(element):
    pieces, stack = [], [element]
    while stack:
        node = stack.pop()
        if node.tag == W + "del":
            continue
        if node.tag == W + "t":
            pieces.append(node.text or "")
        elif node.tag == W + "tab":
            pieces.append("\t")
        elif node.tag in (W + "br", W + "cr"):
            pieces.append("\n")
        else:
            stack.extend(reversed(list(node)))
    return "".join(pieces).strip()


def _blocks(element, depth=0):
    if depth > 32:
        raise DocumentImportError("Word 表格嵌套过深，请简化文档后上传。")
    for node in element:
        if node.tag == W + "p":
            text = _paragraph(node)
            if text:
                yield text
        elif node.tag == W + "tbl":
            for row in node.findall(W + "tr"):
                cells = ["；".join(_blocks(cell, depth + 1)) for cell in row.findall(W + "tc")]
                if any(cells):
                    yield " | ".join(cells)
        elif node.tag in (W + "sdt", W + "sdtContent", W + "customXml"):
            yield from _blocks(node, depth + 1)


def read_document(path: Path) -> str:
    if path.suffix.lower() == ".md":
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeError:
            raise DocumentImportError("Markdown 文件需使用 UTF-8 编码，请转换后上传。") from None
    else:
        try:
            with ZipFile(path) as archive:
                info = archive.getinfo("word/document.xml")
                if info.file_size > 20 * 1024 * 1024 or info.flag_bits & 1:
                    raise DocumentImportError("Word 正文过大或已加密，请缩小内容或解除密码后上传。")
                with archive.open(info) as source:
                    xml = source.read(20 * 1024 * 1024 + 1)
                if len(xml) > 20 * 1024 * 1024:
                    raise DocumentImportError("Word 正文超过读取上限，请分成多个文档上传。")
            root = ET.fromstring(xml, parser=ET.XMLParser(target=_PlainXML()))
            body = root.find(W + "body")
            if root.tag != W + "document" or body is None:
                raise DocumentImportError("未识别到标准 Word 正文，请另存为普通 .docx 后上传。")
            text = "\n".join(_blocks(body))
        except (BadZipFile, KeyError, ET.ParseError, RuntimeError, NotImplementedError):
            raise DocumentImportError("无法读取 Word 文档，文件可能损坏、受密码保护或格式不正确。请另存为 .docx 后上传。") from None
    if not text.strip():
        raise DocumentImportError("文档没有可读取的文字。图片或扫描件请先识别成文字，再上传。")
    if len(text) > 2_000_000:
        raise DocumentImportError("文档文字超过 200 万字符，请分成多个文档上传。")
    return text
