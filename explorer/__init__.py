"""
explorer 包的对外门面。
list_dir 导出的是 file.list_dir(带 folders/files 结构的目录树),
不是裸的 os.listdir;需要纯名称列表请用 listdir_names。
"""
from explorer import file
from explorer import search as _search

list_dir      = file.list_dir
listdir_names = file.listdir_names
read          = file.read
read_raw      = file.read_raw
create        = file.create
change        = file.change
remove        = file.remove_file
write_text    = file.write_text
search        = _search.search

read_text_with_encoding = file.read_text_with_encoding
detect_newline          = file.detect_newline

MAX_TEXT_BYTES   = file.MAX_TEXT_BYTES
MAX_LIST_ENTRIES = file.MAX_LIST_ENTRIES

__all__ = [
    'list_dir', 'listdir_names', 'read', 'read_raw', 'create', 'change',
    'remove', 'write_text', 'search', 'read_text_with_encoding',
    'detect_newline', 'MAX_TEXT_BYTES', 'MAX_LIST_ENTRIES',
]
