from openai import OpenAI

client = OpenAI(
  base_url = "https://integrate.api.nvidia.com/v1",
  api_key = "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"
)

print("Отправка запроса...\n")
completion = client.chat.completions.create(
  model="qwen/qwen3-coder-480b-a35b-instruct",
  messages=[{"role":"user","content":"Привет. Это тестовый запрос к API."}],
  temperature=0.7,
  top_p=0.8,
  max_tokens=4096,
  stream=True
)

for chunk in completion:
  if chunk.choices and chunk.choices[0].delta.content is not None:
    print(chunk.choices[0].delta.content, end="")

