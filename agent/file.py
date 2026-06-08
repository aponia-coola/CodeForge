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


