def get_filelist(path):
    return os.listdir(path)

def create_file(file_path):
    with open(file_path, 'w', encoding='utf-8') as file:
        file.write("")

def append_file(file_path, content, chunk_size=8192):
    with open(file_path, 'a', encoding='utf-8') as file:
        for i in range(0, len(content), chunk_size):
            file.write(content[i:i + chunk_size])


