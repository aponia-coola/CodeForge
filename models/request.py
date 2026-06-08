import os
from openai import OpenAI


def request(message,modelname,action,):
    client = OpenAI(
        api_key=os.environ.get('DEEPSEEK_API_KEY'),
        base_url="https://api.deepseek.com"
    )

    response = client.chat.completions.create(
        model=modelname,
        messages=[
            {"role": "system", "content": action},
            {"role": "user", "content": message},
        ],
        stream=False,
        reasoning_effort="high",
        extra_body={"thinking": {"type": "enabled"}}
    )
    return response.choices[0].message.content
