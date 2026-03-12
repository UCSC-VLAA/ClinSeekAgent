import json
from openai import OpenAI
import torch
import copy
from types import SimpleNamespace
from vllm import LLM, SamplingParams
from transformers import AutoModelForCausalLM, AutoTokenizer

# Claude Opus 4.6 via AWS Bedrock
# 必须使用 inference profile ID（支持 on-demand），不能用直接模型 ID anthropic.claude-opus-4-6-v1
# 参考 test.py: modelId="us.anthropic.claude-opus-4-6-v1"
BEDROCK_CLAUDE_MODELS = [
    "claude-opus-4-6-v1",
    "us.anthropic.claude-opus-4-6-v1",
]
BEDROCK_INFERENCE_PROFILE_ID = "us.anthropic.claude-opus-4-6-v1"

OPENAI_CHAT_MODELS = [
    "gpt-3.5-turbo",
    "gpt-3.5-turbo-16k-0613",
    "gpt-3.5-turbo-16k",
    "gpt-4",
    "gpt-4-0613",
    "gpt-4-turbo",
    "gpt-4-32k",
    "gpt-4-32k-0613",
    "gpt-4-1106-preview",
    "gpt-4o-mini",
    "gpt-5-mini",
    "glm-4.5",
    "gemini-2.5-flash",
    "claude-haiku-4-5-20251001",
    "grok-4-1-fast-non-reasoning"
]
OPENAI_LLM_MODELS = ["text-davinci-003", "text-ada-001"]

LOCAL_MODEL_PATHS = {
    "qwen3_4b": "/sfs/data/ShareModels/LLMs/Qwen3-4B",
    "qwen3_8b": "/sfs/data/ShareModels/LLMs/Qwen3-8B",
    "qwen3_32b": "/sfs/rhome/liaoyusheng/data/ShareModels/LLMs/Qwen3-32B",
    "qwen3_30b_moe": "/sfs/data/ShareModels/LLMs/Qwen3-30B-A3B-Instruct-2507",
    "qwen3_80b_moe": "/sfs/data/ShareModels/LLMs/Qwen3-Next-80B-A3B-Instruct",
    "qwen3_235b_moe": "/sfs/data/ShareModels/LLMs/Qwen3-235B-A22B-Instruct-2507",
    "qwen3_235b_moe_int4": "/sfs/data/ShareModels/LLMs/Qwen3-235B-A22B-Instruct-2507-AWQ",
    "gpt_oss_20b": "/sfs/data/ShareModels/LLMs/gpt-oss-20b",
    "gpt_oss_120b": "/sfs/data/ShareModels/LLMs/gpt-oss-120b",
    "llama3.1_70b": "/sfs/data/ShareModels/LLMs/Meta-Llama-3.1-70B-Instruct",
    "glm4.5": "/sfs/data/ShareModels/LLMs/GLM-4.5-Air",
    "llama4": "/sfs/data/ShareModels/LLMs/Llama-4-Scout-17B-16E-Instruct",
    "qwen3_coder_30b_moe": "/sfs/data/ShareModels/LLMs/Qwen3-Coder-30B-A3B-Instruct"
}

def disable_torch_init():
    """
    Disable the redundant torch default initialization to accelerate model creation.
    """
    import torch
    setattr(torch.nn.Linear, "reset_parameters", lambda self: None)
    setattr(torch.nn.LayerNorm, "reset_parameters", lambda self: None)

class BaseLLM:
    def __init__(self, model_args) -> None:
        self.model_name_or_path = model_args.model_name_or_path
        self.max_new_tokens: int = model_args.max_new_tokens
        self.temperature: float = model_args.temperature
        self.top_p: float = model_args.top_p
        self.top_k: int = model_args.top_k
        self.enable_thinking: bool = model_args.enable_thinking
        self.max_seq_len: int = model_args.max_seq_len
        self.presence_penalty: float = model_args.presence_penalty

        self.tokenizer = AutoTokenizer.from_pretrained(self.model_name_or_path)

    def __call__(self, prompt: str) -> str:
        return self.run(prompt)
    
    def process_inputs(self, messages: str, available_tools: list = None):
        if getattr(self, 'tokenizer', None):
            if "qwen3" in self.model_name_or_path.lower():
                messages[-1]["content"] += "/think" if self.enable_thinking else "/no_think"
                inputs = self.tokenizer.apply_chat_template(messages, tools=available_tools, add_generation_prompt=True, tokenize=False, enable_thinking=self.enable_thinking)
            else:
                inputs = self.tokenizer.apply_chat_template(messages, tools=available_tools, add_generation_prompt=True, tokenize=False)
        else:
            inputs = messages
        
        return inputs
    
    def process_outputs(self, outputs: str):
        if "qwen3" in self.model_name_or_path.lower() and "</think>" in outputs:
            outputs = {
                "reasoning": (outputs.rsplit("</think>", 1)[0] + "</think>").strip(),
                "output": outputs.rsplit("</think>", 1)[-1].strip(),
            }
        else:
            outputs = {
                "reasoning": "",
                "output": outputs.strip()
            }
        
        return outputs

    def run(self, prompt: str, n: int = 1):
        # return str
        raise NotImplementedError

class HFLocalLLM(BaseLLM):
    def __init__(self, model_args):
        super().__init__(model_args=model_args)

        self.model = AutoModelForCausalLM.from_pretrained(
            self.model_name_or_path,
            torch_dtype=torch.float16,
            trust_remote_code=True,
            device_map="auto",
            low_cpu_mem_usage=True
        )
        self.model.eval()

        self.tokenizer = AutoTokenizer.from_pretrained(
            self.model_name_or_path,
            trust_remote_code=True
        )

        self.device = self.model.device
    
    
    def run(self, messages: str, available_tools: list = None, n: int = 1):
        inputs = self.process_inputs(messages, available_tools)
        inputs = self.tokenizer(inputs, padding=True, truncation=True, return_tensors="pt")

        with torch.no_grad():
            model_outputs = self.model.generate(
                input_ids=inputs.input_ids.to(self.device),
                attention_mask=inputs.attention_mask.to(self.device),
                do_sample=False if self.temperature == 0.0 else True,
                temperature=self.temperature,
                top_p=self.top_p,
                max_new_tokens=self.max_new_tokens,
                tokenizer=self.tokenizer,
                return_dict_in_generate=True, 
                output_logits=True
            )

        final_model_outputs = model_outputs.sequences[:, len(inputs["input_ids"][0]):]
        outputs = self.tokenizer.batch_decode(final_model_outputs, skip_special_tokens=True)
        outputs = self.process_outputs(outputs)
        return outputs

class VLLM(BaseLLM):
    def __init__(self, model_args):
        super().__init__(model_args=model_args)

        self.gpu_memory_utilization: float = model_args.gpu_memory_utilization
        self.model = LLM(
            model=self.model_name_or_path, 
            tensor_parallel_size=torch.cuda.device_count(), 
            trust_remote_code=True, 
            enable_prefix_caching=True,
            max_model_len=self.max_seq_len, 
            gpu_memory_utilization=self.gpu_memory_utilization
        )

        self.tokenizer = AutoTokenizer.from_pretrained(
            self.model_name_or_path,
            trust_remote_code=True
        )

        self.sampling_params = SamplingParams(
            max_tokens=self.max_new_tokens,
            temperature=self.temperature,
            top_p=self.top_p,
        )

    def run(self, messages: str, available_tools: list = None, n: int = 1):
        # inputs = self.process_inputs(messages, available_tools)
        self.sampling_params.n = n
        outputs = self.model.chat(
            messages, 
            sampling_params=self.sampling_params,
            tools=available_tools,
            use_tqdm=False,
            chat_template_kwargs={"enable_thinking": self.enable_thinking} if "qwen3" in self.model_name_or_path.lower() else None,
        )
        outputs = self.process_outputs(outputs[0].outputs[0].text)
        return outputs

class VLLMServer(BaseLLM):
    def __init__(self, model_args):
        super().__init__(model_args=model_args)

        self.vllm_server_url = model_args.vllm_server_url
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_name_or_path, trust_remote_code=True)
        self.client = OpenAI(
            api_key="EMPTY",
            base_url=f"{self.vllm_server_url}/v1"
        )
        self.input_tokens = 0
        self.output_tokens = 0
    
    def token_count(self, messages: list, available_tools: list = None):
        if available_tools:
            input_ids = self.tokenizer.apply_chat_template(messages, available_tools, tokenize=True, add_generation_prompt=True)
        else:
            input_ids = self.tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True)
        token_counts = len(input_ids)
        return token_counts
    
    def _truncate_messages(self, messages: list, available_tools: list = None, max_tokens: int = 55000):
        for turn in messages:
            if turn["content"] is None:
                turn["content"] = ""
        
            if "glm" in self.model_name_or_path.lower() or "coder" in self.model_name_or_path.lower():
                if "tool_calls" in turn:
                    for tc in turn["tool_calls"]:
                        if isinstance(tc["function"]["arguments"], str):
                            tc["function"]["arguments"] = json.loads(tc["function"]["arguments"])

        max_tokens = self.max_seq_len - self.max_new_tokens - 1000
        current_tokens = self.token_count(messages, available_tools)

        # print(f"{max_tokens=}, {current_tokens=}")
        
        if current_tokens <= max_tokens:
            return messages
        
        while current_tokens > max_tokens and len(messages) > 3:
            del messages[3]
            current_tokens = self.token_count(messages, available_tools)
            print(f"{current_tokens=}")
        
        return messages
    
    def preprocess(self, messages: list):
        # if "gpt" in self.model_name_or_path.lower() :
        #     for turn in messages:
        #         if turn["content"] is None:
        #             turn["content"] = ""
                
        #         if "tool_calls" in turn:
        #             for tc in turn["tool_calls"]:
        #                 tc["function"]["arguments"] = str(tc["function"]["arguments"])

        # if "qwen" in self.model_name_or_path.lower():
        #     for turn in messages:
        #         if "tool_calls" in turn:
        #             for tc in turn["tool_calls"]:
        #                 tc["function"]["arguments"] = str(tc["function"]["arguments"])
        
        # elif "llama" in self.model_name_or_path.lower() and "glm" in self.model_name_or_path.lower():
        
        for turn in messages:
            if turn["content"] is None:
                turn["content"] = ""
            
            if "tool_calls" in turn:
                for tc in turn["tool_calls"]:
                    tc["function"]["arguments"] = json.dumps(tc["function"]["arguments"])
                    
        
        return messages

    def run(self, messages: list, available_tools: list = None, tool_choice: str = "auto", n: int = 1):
        messages = self._truncate_messages(messages, available_tools, max_tokens=self.max_seq_len)

        messages = copy.deepcopy(messages)
        messages = self.preprocess(messages)

        if "qwen3" in self.model_name_or_path.lower():
            extra_body = {"chat_template_kwargs": {"enable_thinking": self.enable_thinking}}
        
        elif "gpt-oss" in self.model_name_or_path.lower():
            extra_body = {"reasoning_effort": "medium" if not self.enable_thinking else "high"}
        
        else:
            extra_body = None

        response = self.client.chat.completions.create(
            model=self.model_name_or_path,
            messages=messages,
            tools=available_tools,
            max_tokens=self.max_new_tokens,
            temperature=self.temperature,
            n=n,
            top_p=self.top_p,
            presence_penalty=self.presence_penalty,
            # top_k=self.top_k,
            tool_choice=tool_choice if available_tools else None,
            extra_body=extra_body,
            stream=False
        )

        try:
            self.input_tokens += response.usage.prompt_tokens
            self.output_tokens += response.usage.completion_tokens
        except Exception as e:
            print(f"Error: {e}")

        return response


class ClaudeBedrockLLM:
    """Claude Opus 4.6 via AWS Bedrock API (like test.py)."""

    def __init__(self, model_args):
        import boto3

        raw = getattr(model_args, "model_name_or_path", BEDROCK_INFERENCE_PROFILE_ID)
        if raw == "claude-opus-4-6-v1" or raw not in BEDROCK_CLAUDE_MODELS and not raw.startswith("us.anthropic.claude"):
            self.model_name_or_path = BEDROCK_INFERENCE_PROFILE_ID
        else:
            self.model_name_or_path = raw
        self.max_new_tokens = getattr(model_args, "max_new_tokens", 4096)
        self.temperature = getattr(model_args, "temperature", 0.7)
        self.top_p = getattr(model_args, "top_p", 0.8)
        self.max_seq_len = getattr(model_args, "max_seq_len", 64000)
        self.presence_penalty = getattr(model_args, "presence_penalty", 0.0)
        self.input_tokens = 0
        self.output_tokens = 0
        self.region_name = getattr(model_args, "bedrock_region", "us-west-2")
        self.bedrock = boto3.client(service_name="bedrock-runtime", region_name=self.region_name)

    def _convert_tools_to_anthropic(self, available_tools):
        """Convert OpenAI-style tools to Anthropic input_schema format."""
        if not available_tools:
            return None
        anthropic_tools = []
        for t in available_tools:
            if t.get("type") == "function" and "function" in t:
                fn = t["function"]
                schema = fn.get("input_schema") or fn.get("parameters") or {"type": "object", "properties": {}}
                anthropic_tools.append({
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "input_schema": schema,
                })
        return anthropic_tools if anthropic_tools else None

    def _convert_messages_to_anthropic(self, messages):
        """Convert OpenAI-style messages to Anthropic format."""
        anthropic_messages = []
        for msg in messages:
            role = msg["role"]
            if role == "system":
                continue  # Anthropic uses system separately; we'll skip or prepend to first user
            content = msg.get("content")
            if role == "user":
                if isinstance(content, list):
                    anthropic_messages.append({"role": "user", "content": content})
                else:
                    anthropic_messages.append({"role": "user", "content": content or ""})
            elif role == "assistant":
                if "tool_calls" in msg and msg["tool_calls"]:
                    blocks = []
                    for tc in msg["tool_calls"]:
                        fn = tc.get("function", {})
                        args = fn.get("arguments")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {}
                        blocks.append({
                            "type": "tool_use",
                            "id": tc.get("id", f"toolu_{len(blocks)}"),
                            "name": fn.get("name", ""),
                            "input": args or {},
                        })
                    anthropic_messages.append({"role": "assistant", "content": blocks})
                else:
                    text = content if isinstance(content, str) else (content or "")
                    anthropic_messages.append({"role": "assistant", "content": text})
            elif role == "tool":
                tool_call_id = msg.get("tool_call_id")
                tool_content = msg.get("content", "")
                anthropic_messages.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tool_call_id, "content": str(tool_content)}],
                })
        return anthropic_messages

    def _convert_response_to_openai_like(self, response_body):
        """Convert Anthropic/Bedrock response to OpenAI-like structure for action_parser."""
        content_blocks = response_body.get("content", [])
        stop_reason = response_body.get("stop_reason", "end_turn")

        text_parts = []
        tool_calls = []
        for block in content_blocks:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                fn = SimpleNamespace(
                    name=block.get("name", ""),
                    arguments=json.dumps(block.get("input", {})),
                )
                tool_calls.append(SimpleNamespace(id=block.get("id", ""), type="function", function=fn))

        finish_reason = "tool_calls" if stop_reason == "tool_use" else "stop"
        message_content = "".join(text_parts).strip() if text_parts else None

        msg = SimpleNamespace(
            content=message_content,
            tool_calls=tool_calls,
            model_dump=lambda: {
                "role": "assistant",
                "content": message_content,
                "tool_calls": [
                    {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in tool_calls
                ] if tool_calls else [],
            },
        )
        choice = SimpleNamespace(finish_reason=finish_reason, message=msg)
        return SimpleNamespace(choices=[choice])

    def run(self, messages: list, available_tools: list = None, tool_choice: str = "auto", n: int = 1):
        messages = copy.deepcopy(messages)
        for turn in messages:
            if turn.get("content") is None:
                turn["content"] = ""
            if "tool_calls" in turn:
                for tc in turn["tool_calls"]:
                    args = tc["function"].get("arguments")
                    if isinstance(args, dict):
                        tc["function"]["arguments"] = json.dumps(args)

        anthropic_messages = self._convert_messages_to_anthropic(messages)
        anthropic_tools = self._convert_tools_to_anthropic(available_tools)

        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self.max_new_tokens,
            "messages": anthropic_messages,
            # "temperature": self.temperature,
            # "top_p": self.top_p,
        }
        if anthropic_tools:
            body["tools"] = anthropic_tools

        body_str = json.dumps(body)
        model_id = self.model_name_or_path
        # print(f"[Bedrock] modelId={model_id} region={self.region_name} max_tokens={self.max_new_tokens} temperature={self.temperature} top_p={self.top_p} "
        #       f"messages={len(anthropic_messages)} tools={len(anthropic_tools) if anthropic_tools else 0}", flush=True)
        try:
            response = self.bedrock.invoke_model(body=body_str, modelId=model_id)
        except Exception as e:
            err_str = str(e).lower()
            if ("on-demand" in err_str or "inference profile" in err_str) and model_id != BEDROCK_INFERENCE_PROFILE_ID:
                response = self.bedrock.invoke_model(body=body_str, modelId=BEDROCK_INFERENCE_PROFILE_ID)
            else:
                raise
        response_body = json.loads(response["body"].read())

        try:
            usage = response_body.get("usage", {})
            self.input_tokens += usage.get("input_tokens", 0)
            self.output_tokens += usage.get("completion_tokens", usage.get("output_tokens", 0))
        except Exception:
            pass

        return self._convert_response_to_openai_like(response_body)


def get_llm_backend(model_args):
    if model_args.model_name_or_path in OPENAI_CHAT_MODELS:
        return GPT4o(model_args)

    if model_args.model_name_or_path in BEDROCK_CLAUDE_MODELS or (
        isinstance(model_args.model_name_or_path, str)
        and "claude-opus-4-6" in model_args.model_name_or_path.lower()
    ):
        return ClaudeBedrockLLM(model_args)

    if model_args.model_name_or_path:
        model_args.model_name_or_path = model_args.model_name_or_path if model_args.model_name_or_path not in LOCAL_MODEL_PATHS else LOCAL_MODEL_PATHS[model_args.model_name_or_path]
    
    if model_args.vllm_server_url:
        return VLLMServer(model_args)
    else:
        return VLLM(model_args)
    

class GPT4o:
    def __init__(self, model_args) -> None:
        if "grok" in model_args.model_name_or_path:
            self.client = OpenAI(
                api_key="sk-cm87qYlaJXcTs50CUkwruf2caC8fivSARVmk6xYWgwicJbKE",
                base_url=f"http://192.154.241.225:3000/v1",
            )

        else: 
            self.client = OpenAI(
                api_key="sk-3OD8o4nLS4o52so3LJ9iw2kTlrZ0aGuRiLRI4sxaSZx4c6sm",
                base_url=f"http://192.154.241.225:3000/v1",
            )

        self.model_name_or_path: str =getattr(model_args, "model_name_or_path", "gpt-4o-2024-11-20")
        self.max_new_tokens: int = getattr(model_args, "max_new_tokens", 4096)
        self.temperature: float = getattr(model_args, "temperature", 0.7)
        self.top_p: float = getattr(model_args, "top_p", 0.8)
        self.top_k: int = getattr(model_args, "top_k", 20)
        self.enable_thinking: bool = getattr(model_args, "enable_thinking", False)
        self.max_seq_len: int = getattr(model_args, "max_seq_len", 64000)
        self.presence_penalty: float = getattr(model_args, "presence_penalty", 0.5)
    
    def preprocess(self, messages: list):
        for turn in messages:
            if turn["content"] is None:
                turn["content"] = ""
            
            if "tool_calls" in turn and "gemini" not in self.model_name_or_path:
                for tc in turn["tool_calls"]:
                    tc["function"]["arguments"] = str(tc["function"]["arguments"])
                    
        return messages
    
    def token_count(self, messages):
        messages_length = sum([len(msg["content"]) for msg in messages])
        return messages_length // 3 # token len approximation

    def _truncate_messages(self, messages: list):        
        max_tokens = self.max_seq_len - self.max_new_tokens
        current_tokens = self.token_count(messages)
        
        if current_tokens <= max_tokens:
            return messages
        
        while current_tokens > max_tokens and len(messages) > 3:
            del messages[3]
            current_tokens = self.token_count(messages)
            print(f"{current_tokens=}")
        
        return messages

    def run(self, messages: list, available_tools: list = None, tool_choice: str = "auto", n: int = 1):
        messages = self.preprocess(messages)
        messages = self._truncate_messages(messages)

        if available_tools:
            response = self.client.chat.completions.create(
                model=self.model_name_or_path,
                messages=messages,
                tools=available_tools,
                max_tokens=self.max_new_tokens,
                temperature=self.temperature,
                n=n,
                tool_choice=tool_choice if available_tools else None,
                parallel_tool_calls=False,
                stream=False
            )

        else:
            response = self.client.chat.completions.create(
                model=self.model_name_or_path,
                messages=messages,
                max_tokens=self.max_new_tokens,
                temperature=self.temperature,
                n=n,
                stream=False
            )

        return response


if __name__ == '__main__':
    model = GPT4o(model_args={})

    messages = [
        {"role": "user", "content": "hello"}
    ]
    print(model.run(messages))