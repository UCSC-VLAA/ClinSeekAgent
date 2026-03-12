import os
import uuid
import time
from typing import List
from typing import Optional
from contextlib import AsyncExitStack
# from mcp import ClientSession, StdioServerParameters
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from mcp.client.stdio import stdio_client

from agentlite.agent_prompts import BasePromptGen
from agentlite.agent_prompts.prompt_utils import DEFAULT_PROMPT
from agentlite.agents.agent_utils import *
from agentlite.agents.BaseAgent import BaseAgent
from agentlite.commons import AgentAct, TaskPackage, EHRManager
from agentlite.commons.AgentAct import ActObsChainType
from agentlite.llm.agent_llms import BaseLLM
from agentlite.logging import DefaultLogger
from agentlite.logging.terminal_logger import AgentLogger
from agentlite.memory.AgentSTMemory import AgentSTMemory, DictAgentSTMemory, DynamicErrorSTMemory

class MCPBaseAgent(BaseAgent):
    def __init__(
        self,
        name: str,
        role: str,
        llm: BaseLLM,
        constraint: str = DEFAULT_PROMPT["constraint"],
        instruction: str = DEFAULT_PROMPT["agent_instruction"],
        logger: AgentLogger = DefaultLogger,
        mcp_url: str = None,
        **kwargs
    ):
        self.id = "mcp_agent"
        self.name = name  # short description for agent, use it for id part
        self.role = role  # describe the job duty of this agent
        self.llm = llm
        self.max_exec_steps = kwargs["max_exec_steps"]
        self.task_pool = []
        self.constraint = constraint
        self.instruction = instruction
        self.mcp_url = mcp_url
        self.prompt_gen = BasePromptGen(
            agent_role=self.role,
            constraint=self.constraint,
            instruction=self.instruction,
        )
        self.logger = logger
        self.__add_st_memory__()

        self.messages = []
        self.mcp_gpu_id = kwargs["mcp_gpu_id"]
        self.ehr_path = kwargs["ehr_path"]
        self.max_same_steps = kwargs["max_same_steps"]
        self.inner_tools = []

    async def connect_to_server(self):
        if self.mcp_url is None:
            env_vars = os.environ.copy()
            # Add or overwrite the specific environment variable
            env_vars['CUDA_VISIBLE_DEVICES'] = str(self.mcp_gpu_id)

            transport = StdioTransport(
                command=f"python",
                args=["run_mcp_server.py", "--data_path", f'{self.ehr_path}'],
                env=env_vars,
                # cwd="/path/to/server"
            )
            self.mcp_client = Client(transport)
        else:
            self.mcp_client = Client(self.mcp_url)
    
    async def load_ehr(self, task: TaskPackage):
        async with self.mcp_client:
            action_name = "load_ehr"
            action_params = {'subject_id': str(task.subject_id), 'timestamp': task.timestamp}
            result = await self.mcp_client.call_tool(action_name, action_params)
        action = AgentAct(name=action_name, params=action_params)
        self.logger.take_action(action, agent_name=self.name, step_idx=0)
        tool_id = f"chatcmpl-tool-{uuid.uuid4().hex[:24]}"
        if "qwen" in self.llm.model_name_or_path.lower():
            self.messages.append({
                "role": "assistant",
                "content": None,
                "function": {
                    "id": tool_id,
                    "name": action_name,
                    "arguments": action_params
                },
                "type": "function"
            })

        else:
            self.messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls":[
                    {
                        "id": tool_id,
                        "function": {
                            "name": action_name,
                            "arguments": action_params
                        },
                        "type": "function"
                    }
                ],  
            })

        observation = result.content[0].text
        self.logger.get_obs(obs=observation)
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_id,
            "content": observation
        })
    
    async def __call__(self, task: TaskPackage) -> str:
        """agent can be called with a task. it will assign the task and then execute and respond

        :param task: the task which agent receives and solves
        :type task: TaskPackage
        :return: the response of this task
        :rtype: str
        """
        # adding log information
        self.logger.receive_task(task=task, agent_name=self.name)
        self.assign(task)
        self.subject_id = task.subject_id
        self.messages = []
        self.messages.append({
            "role": "user",
            "content": self.prompt_gen.action_mcp_prompt(task=task),
        })

        # force load EHR
        start_time = time.time()
        start_input_tokens = self.llm.input_tokens
        start_output_tokens = self.llm.output_tokens
        await self.load_ehr(task)
        await self.execute(task)
        end_time = time.time()
        end_input_tokens = self.llm.input_tokens
        end_output_tokens = self.llm.output_tokens

        execution_time = end_time - start_time
        input_tokens = end_input_tokens - start_input_tokens
        output_tokens = end_output_tokens - start_output_tokens

        task.execution_steps = self.logger.get_all_executions()["execution_steps"]
        task.thinking_steps = self.logger.get_all_executions()["thinking_steps"]
        task.execution_time = execution_time
        task.input_tokens = input_tokens
        task.output_tokens = output_tokens
        return task
    
    def same_obs(self, temp_obs):
        if len(self.logger.current_execution_steps) < 2:
            return False

        prev_obs = self.logger.current_execution_steps[-2]["observation"]
        if temp_obs == prev_obs:
            return True
        
        else:
            return False


    async def execute(self, task: TaskPackage):
        """multi-step execution of actions. Generate the actions for a task until reach the done

        :param task: the task which agent receives and solves
        :type task: TaskPackage
        """
        step_size = 0
        same_steps = 0
        self.logger.execute_task(task=task, agent_name=self.name)

        while task.completion == "active" and step_size < self.max_exec_steps:
            # think = await self.__next_think__()
            action = await self.__next_act__()
            if action is None:
                step_size = self.max_exec_steps
                break

            self.logger.take_action(action, agent_name=self.name, step_idx=step_size+1)
            observation = await self.forward(task, action)
            self.logger.get_obs(obs=observation)

            if self.same_obs(observation):
                same_steps += 1
            else:
                same_steps = 0
            
            if same_steps >= self.max_same_steps:
                step_size = self.max_exec_steps
                break

            step_size += 1

        if task.completion == "active" and step_size >= self.max_exec_steps:
            action = await self.__forced_termination__()
            self.logger.take_action(action, agent_name=self.name, step_idx=step_size+1)
            observation = await self.forward(task, action)
            self.logger.get_obs(obs=observation)
            step_size += 1

        self.logger.end_execute(task=task, agent_name=self.name)
    
    def llm_layer(self, messages, available_tools: list = None, n: int = 1, tool_choice: str = "auto") -> str:
        max_try = 5
        try_iter = 0
        response = None
        while try_iter < max_try:
            try:
            # 请求 deepseek，function call 的描述信息通过 tools 参数传入
                response = self.llm.run(messages, available_tools=available_tools, n=n, tool_choice=tool_choice)
                if tool_choice == "none" or response.choices[0].finish_reason == "tool_calls":
                    break
                print(f"Warning: LLM do not call tools, retrying {try_iter}/{max_try}...")

            except Exception as e:
                print(f"Error: {e}, retrying {try_iter}/{max_try}...")
            
            try_iter += 1

        return response

    async def get_available_tools(self, tool_list: list = None):
        async with self.mcp_client: 
            response = await self.mcp_client.list_tools()
        # 生成 function call 的描述信息
        if "qwen" in self.llm.model_name_or_path.lower():
            available_tools = [{
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.inputSchema
                }
            } for tool in response]

        else:
            available_tools = [{
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.inputSchema
                }
            } for tool in response]
            
        if tool_list:
            available_tools = [tool for tool in available_tools if tool["function"]["name"] in tool_list]
        
        assert len(available_tools) > 0
        return available_tools

    async def __next_act__(self) -> AgentAct:
        # 获取所有 mcp 服务器 工具列表信息
        available_tools = await self.get_available_tools()
        available_tools += self.inner_tools
        response = self.llm_layer(self.messages, available_tools)
        # print(f"response: {response}", flush=True)
        # breakpoint()
        return self.__action_parser__(response)
    
    async def __forced_termination__(self) -> AgentAct:
        available_tools = await self.get_available_tools(tool_list=["finish"])
        self.messages.append({
            "role": "user",
            "content": self.prompt_gen.termination_prompt()
            })
        response = self.llm_layer(self.messages, available_tools)
        return self.__action_parser__(response, force_terminal=True)
    
    async def forward(self, task: TaskPackage, agent_act: AgentAct) -> str:
        """
        using this function to forward the action to get the observation.

        :param task: the task which agent receives and solves.
        :type task: TaskPackage
        :param agent_act: the action wrapper for execution.
        :type agent_act: AgentAct
        :return: observation
        :rtype: str
        """
        if agent_act.name != "finish":
            try:
                async with self.mcp_client:
                    # available_tools = await self.mcp_client.list_tools()
                    # for tool in available_tools:
                    #     if tool.name == agent_act.name:
                    #         if 'subject_id' in tool.inputSchema["required"]:
                    #             agent_act.params["subject_id"] = str(self.subject_id)
                    #         break
                    if "subject_id" in agent_act.params:
                        agent_act.params["subject_id"] = str(agent_act.params["subject_id"])
                    result = await self.mcp_client.call_tool(agent_act.name, agent_act.params)

                observation = result.content[0].text
                observation = self._limit_output_length(observation)
                tool_messages = {
                    "role": "tool",
                    "tool_call_id": self.messages[-1]["tool_calls"][0]["id"],
                    "content": observation
                }

                # 只有当 agent_act 有 id 属性时才添加 tool_call_id (防止 finish 等非工具动作报错)
                if hasattr(agent_act, "id") and agent_act.id:
                    tool_messages["tool_call_id"] = agent_act.id
                else:
                    # 如果没有 ID，说明逻辑有问题，或者这是不需要 ID 的操作
                    # 但对于 OpenAI Tool Calling，没有 ID 通常会报错
                    print("Warning: Tool message missing tool_call_id")
                self.messages.append(tool_messages)

            except Exception as e:
                # 即使报错，最好也带上 ID 返回
                err_msg = {
                    "role": "tool",
                    "content": str(e)
                }
                if hasattr(agent_act, "id") and agent_act.id:
                    err_msg["tool_call_id"] = agent_act.id
                
                self.messages.append(err_msg)
                return str(e)

        else:
            try:
                observation = agent_act.params.get("response", "")
            except :
                observation = agent_act.params
            task.answer = str(observation)
            task.completion = "completed"

            # finish 动作通常由模型生成，也需要闭环 tool_call
            if hasattr(agent_act, "id") and agent_act.id:
                 self.messages.append({
                    "role": "tool",
                    "tool_call_id": agent_act.id,
                    "name": "finish",
                    "content": "Task Completed." 
                })
            
            # else:
            #     observation = "Error: You should present your final answer as a **not empty list format** in the response paremter with `finish` tool calling!"
            #     self.messages.append({
            #         "role": "tool",
            #         # "tool_call_id": self.messages[-1]["tool_calls"][0]["id"],
            #         "content": observation
            #     })

        return str(observation)
    
    def __action_parser__(self, response, add_action_messages=True, log_thinking=False, force_terminal=False) -> AgentAct:
        if response is None:
            raise RuntimeError(
                "LLM returned None after all retries. Check: 1) Bedrock model ID is valid and enabled in your AWS account; "
                "2) AWS credentials and region (e.g. us-east-1, us-west-2); 3) Network connectivity."
            )
        if log_thinking:
            try:
                self.logger.get_thinking(response.choices[0].message.reasoning_content)
            except:
                pass

        content = response.choices[0]
        if content.finish_reason == "tool_calls":
            # 如何是需要使用工具，就解析工具
            tool_call = content.message.tool_calls[0]
            tool_name = tool_call.function.name
            # tool_args = json.loads(tool_call.function.arguments)
            try:
                tool_args = json.loads(tool_call.function.arguments)
            except:
                tool_args = tool_call.function.arguments

            action = AgentAct(name=tool_name, params=tool_args)
            action.id = tool_call.id 

            if add_action_messages:
                dump = content.message.model_dump()
                # Anthropic/Bedrock 要求每个 tool_use 都有对应的 tool_result；agent 每次只执行一个工具，
                # 故只保留实际执行的 tool_call，避免 "tool_use ids without tool_result" 错误
                if dump.get("tool_calls") and len(dump["tool_calls"]) > 1:
                    dump["tool_calls"] = [dump["tool_calls"][0]]
                self.messages.append(dump)
        
        # elif content.finish_reason == "stop":
        #     final_response = content.message.content.strip()
            
        #     # 1. 生成一个假的 tool_call_id
        #     fake_id = f"call_{str(uuid.uuid4())}"
            
        #     # 2. 构造 finish 工具需要的参数
        #     finish_params = {"response": final_response}
            
        #     # 3. 构造一个 Action，名字叫 finish，带上伪造的 ID
        #     action = AgentAct(name="finish", params=finish_params)
        #     action.id = fake_id
            
        #     if add_action_messages:
        #         # 🔥 [核心 Trick] 欺骗历史记录
        #         # 我们手动构造一条 "Assistant 发起工具调用" 的消息存进去
        #         # 这样下一步 forward 里存入 tool result 时，OpenAI 就觉得很合理
        #         fake_assistant_msg = {
        #             "role": "assistant",
        #             "content": final_response, # 保留它的原话作为 content (可选，也可以是 null)
        #             "tool_calls": [{
        #                 "id": fake_id,
        #                 "type": "function",
        #                 "function": {
        #                     "name": "finish",
        #                     "arguments": json.dumps(finish_params)
        #                 }
        #             }]
        #         }
        #         self.messages.append(fake_assistant_msg)
        
        else:
            if response:
                final_response = response.choices[0].message.content
            else:
                final_response = "None"
            if isinstance(final_response, str):
                final_response = final_response.strip()
            action = AgentAct(name="think" if not force_terminal else "finish", params={"response": final_response})
            action.id = None

        return action
            
    def _limit_output_length(self, output) -> str:
        """
        Limit the output length to the max_output_length.
        """
        self.max_output_length = 8000
        if len(str(output)) <= self.max_output_length:
            return output

        # Calculate original length and truncate output
        original_length = len(str(output))
        truncated_output = str(output)[-self.max_output_length:]
    
        # Add truncation message
        truncation_msg = f"... (totally {original_length} str, and has been cut to {self.max_output_length} str)"
        return truncated_output + truncation_msg

        