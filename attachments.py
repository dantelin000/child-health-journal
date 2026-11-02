"""Bounded medical-document uploads, stored atomically with their record in SQLite.

Signatures reject common mistaken formats; they are not a malware scan or a full
document parser. Always download as octet-stream, never execute or embed uploads.
"""

import re
import unicodedata
from pathlib import PurePosixPath

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_UPLOADS = 3
MAX_RECORD_ATTACHMENTS = 20
MAX_UPLOAD_REQUEST = 32 * 1024 * 1024


def read_uploads(files):
    files = [file for file in files if file.filename]
    if len(files) > MAX_UPLOADS:
        raise ValueError("每次最多上传 3 个病例单，请分次补充。")
    result = []
    for file in files:
        content = file.stream.read(MAX_FILE_BYTES + 1)
        if not content:
            raise ValueError("病例单不能为空文件。")
        if len(content) > MAX_FILE_BYTES:
            raise ValueError("每个病例单不能超过 10 MB。")
        suffix = PurePosixPath(file.filename.replace("\\", "/")).suffix.lower()
        valid = (
            (suffix in {".jpg", ".jpeg"} and content.startswith(b"\xff\xd8\xff"))
            or (suffix == ".png" and content.startswith(b"\x89PNG\r\n\x1a\n"))
            or (suffix == ".webp" and content.startswith(b"RIFF") and content[8:12] == b"WEBP")
            or (suffix == ".pdf" and re.match(rb"%PDF-[12]\.[0-9]", content))
        )
        if not valid:
            raise ValueError("病例单仅支持 PDF、JPG、PNG 或 WebP，文件内容须与扩展名一致。")
        name = PurePosixPath(file.filename.replace("\\", "/")).stem
        name = "".join(char for char in name if not unicodedata.category(char).startswith("C"))
        name = name.strip(" .")[:100] or "病例单"
        result.append((name + suffix, content))
    return result
