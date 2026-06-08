import sys
import os

# 将项目根目录加入 sys.path,使 agent 可作为包导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import file
from agent.file import read, list, create, append


file.create('test.txt')
file.append('test.txt', 'hello world')
print(file.read('test.txt'))
