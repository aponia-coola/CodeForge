# 读取文件
import os


def read_file(file_path):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    full_path = os.path.join(base_dir, file_path)
    with open(full_path, 'r', encoding='utf-8') as file:
        content = file.read()
    return content

content = read_file("test.txt")
print(content)
