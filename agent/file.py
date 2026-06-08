def list(path):
    return os.listdir(path)

def create(file_path):
    with open(file_path, 'w', encoding='utf-8') as file:
        file.write("")

def append(file_path, content, mode='append', position=None, chunk_size=8192):
    """
    mode:
        'append' - 追加到文件末尾(默认)
        'edit'   - 在指定 position 处插入 content
    position:
        edit 模式下必填,表示字节偏移位置(与字符串切片语义一致)
    """
    if mode == 'append':
        with open(file_path, 'a', encoding='utf-8') as file:
            for i in range(0, len(content), chunk_size):
                file.write(content[i:i + chunk_size])
    elif mode == 'edit':
        if position is None:
            raise ValueError("edit 模式需要指定 position")
        with open(file_path, 'r', encoding='utf-8') as file:
            existing = file.read()
        new_content = existing[:position] + content + existing[position:]
        with open(file_path, 'w', encoding='utf-8') as file:
            for i in range(0, len(new_content), chunk_size):
                file.write(new_content[i:i + chunk_size])
    else:
        raise ValueError(f"不支持的 mode: {mode},可选 'append' 或 'edit'")


