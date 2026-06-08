import requests
import os


def call_deepseek_api(messages, api_key=None):
    """
    调用 DeepSeek API
    
    Args:
        messages: 消息列表，格式为 [{"role": "user/system", "content": "..."}]
        api_key: DeepSeek API 密钥，如果为 None 则从环境变量 DEEPSEEK_API_KEY 读取
    
    Returns:
        API 响应数据（字典）
    """
    if api_key is None:
        api_key = os.getenv("DEEPSEEK_API_KEY")
    
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY 未设置")
    
    url = "https://api.deepseek.com/chat/completions"
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    
    data = {
        "model": "deepseek-v4-pro",
        "messages": messages,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
        "stream": False
    }
    
    response = requests.post(url, headers=headers, json=data)
    response.raise_for_status()
    
    return response.json()


# 使用示例
if __name__ == "__main__":
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello!"}
    ]
    
    result = call_deepseek_api(messages)
    print(result)
