import os

def list(path):
    return os.listdir(path)


def create(file_path):
    with open(file_path, 'w', encoding='utf-8') as file:
        file.write("")


    """
    读取文件内容,默认带行号。

    path:
        文件路径,相对路径以本文件所在目录为基准
    start_line:
        起始行号(1-indexed,包含),None 表示从第 1 行开始,读取到末尾
    返回:
        字符串,格式 "  N | content"(N 为行号,右对齐宽度自适应)
    """
def read(path, start_line=None):

    base_dir = os.path.dirname(os.path.abspath(__file__))
    full_path = path if os.path.isabs(path) else os.path.join(base_dir, path)

    with open(full_path, 'r', encoding='utf-8') as file:
        lines = file.readlines()

    s = 0 if start_line is None else max(0, start_line - 1)
    selected = lines[s:]
    total = s + len(selected)
    width = len(str(total)) if total > 0 else 1
    return ''.join(f"{s + i + 1:>{width}} | {line}" for i, line in enumerate(selected))
    

"""
    mode:
        'append' - 追加到文件末尾(默认)
        'edit'   - 按行编辑
    position:
        edit 模式下必填,起始行号(0-indexed,包含)
    end_line:
        结束行号(不包含),默认 position + 1,即只替换 position 这一行
        end_line == position 时为纯插入(不替换任何行)
    """
def append(file_path, content, mode='append', position=None, end_line=None, chunk_size=4096):

    if mode == 'append':
        with open(file_path, 'a', encoding='utf-8') as file:
            for i in range(0, len(content), chunk_size):
                file.write(content[i:i + chunk_size])
    elif mode == 'edit':
        if position is None:
            raise ValueError("edit 模式需要指定 position(行号)")
        if end_line is None:
            end_line = position + 1
        with open(file_path, 'r', encoding='utf-8') as file:
            lines = file.readlines()
        segment = content if content.endswith('\n') else content + '\n'
        lines[position:end_line] = [segment]
        with open(file_path, 'w', encoding='utf-8') as file:
            file.writelines(lines)
    else:
        raise ValueError(f"不支持的 mode: {mode},可选 'append' 或 'edit'")


def list_tree(path, show_hidden=False):
    """
    列出指定目录下的一级内容,分文件夹与文件两类返回。
    供前端资源管理器渲染文件树使用。

    Args:
        path: 目录绝对路径
        show_hidden: 是否包含以 "." 开头的隐藏项,默认 False

    Returns:
        dict,结构如下:
        {
            "path": "<绝对路径>",
            "folders": [{"name": "...", "path": "..."}, ...],
            "files":   [{"name": "...", "path": "...", "size": <int>}, ...]
        }
        各类内部按名称升序排序;路径不存在或不是目录时抛 FileNotFoundError / NotADirectoryError。
    """
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        raise FileNotFoundError(f"路径不存在: {abs_path}")
    if not os.path.isdir(abs_path):
        raise NotADirectoryError(f"不是目录: {abs_path}")

    folders, files = [], []
    for name in os.listdir(abs_path):
        if not show_hidden and name.startswith('.'):
            continue
        full = os.path.join(abs_path, name)
        if os.path.isdir(full):
            folders.append({"name": name, "path": full})
        elif os.path.isfile(full):
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            files.append({"name": name, "path": full, "size": size})

    folders.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())
    return {"path": abs_path, "folders": folders, "files": files}


