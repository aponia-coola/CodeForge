"""
OpenAI 兼容的工具调用框架。
适用于 DeepSeek、OpenAI、其他兼容 OpenAI Chat Completions 协议的 API。
"""
import json
from typing import Callable
from openai import OpenAI


class ToolCallAgent:
    """
    工具调用 Agent。
    用法:
        agent = ToolCallAgent()

        @agent.tool(name="...", description="...", parameters={...})
        def my_tool(...):
            ...

        answer = agent.chat(messages)
    """

    def __init__(
        self,
        api_key: str = "sk-a32dc24893fc478782451979668eb124",
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-flash",
    ):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.tools: list[dict] = []
        self.handlers: dict[str, Callable] = {}

    def tool(self, name: str, description: str, parameters: dict):
        """
        装饰器:注册一个工具。

        Args:
            name: 工具名称(英文/拼音,全局唯一)
            description: 工具描述(给 AI 看的,要清晰说明何时调用)
            parameters: JSON Schema 格式的参数定义
        """
        def decorator(func: Callable) -> Callable:
            self.tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                }
            })
            self.handlers[name] = func
            return func
        return decorator

    def call_tool(self, name: str, arguments: str) -> str:
        """执行一个工具调用,异常转字符串返回,不会中断对话"""
        if name not in self.handlers:
            return f"错误: 未知工具 {name}"
        try:
            args = json.loads(arguments) if arguments else {}
            result = self.handlers[name](**args)
            return str(result)
        except Exception as e:
            return f"工具 {name} 执行失败: {type(e).__name__}: {e}"

    def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        max_rounds: int = 8,
        temperature: float | None = None,
    ) -> str:
        """
        多轮工具调用对话。
        遇到 tool_calls 则执行并把结果回填 messages,直到 AI 返回纯文本或达到 max_rounds。

        Args:
            messages: 消息历史(就地累积)
            model: 本次使用的模型,None 时使用默认
            max_rounds: 最大工具调用轮次(防死循环)
            temperature: 温度参数,None 走模型默认

        Returns:
            AI 的最终文本回答
        """
        use_model = model or self.model
        kwargs = {
            "model": use_model,
            "messages": messages,
            "tools": self.tools,
            "tool_choice": "auto",
        }
        if temperature is not None:
            kwargs["temperature"] = temperature

        for _ in range(max_rounds):
            resp = self.client.chat.completions.create(**kwargs)
            msg = resp.choices[0].message

            # 无工具调用 → 收尾,直接返回文本
            if not msg.tool_calls:
                return msg.content or ""

            # 关键:AI 的 tool_call 消息必须原样追加,否则 API 报错
            messages.append(msg)

            for call in msg.tool_calls:
                result = self.call_tool(call.function.name, call.function.arguments)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                })

        return f"达到最大轮次({max_rounds})仍未收敛"


# 便捷:默认 agent(指向 DeepSeek)
default_agent = ToolCallAgent()


# ============ 示例 / 烟雾测试 ============
if __name__ == "__main__":
    from agent.file import read as _read

    @default_agent.tool(
        name="read_file",
        description="按行号读取文件内容,行号从 1 开始;start_line 缺省则从第 1 行读到末尾",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对 agent/ 的文件路径"},
                "start_line": {"type": "integer", "description": "起始行号(1-indexed,包含)"},
            },
            "required": ["path"],
        },
    )
    def read_file(path: str, start_line: int | None = None):
        return _read(path, start_line)

    messages = [
        {"role": "system", "content": "你是代码助手,需要查文件时主动调用 read_file,不要瞎猜。"},
        {"role": "user", "content": "agent/file.py 的 read 函数是怎么实现的?"},
    ]
    print(default_agent.chat(messages))
