from typing import List, Optional, Dict, Any, Callable
import json
import re
import pandas as pd
import os

from agentlite.agents.ReasoningBank.judge import TrajectoryJudge
from agentlite.agents.ReasoningBank.extractor import MemoryExtractor
from agentlite.agents.ReasoningBank.retriever import EHRRetriever
from agentlite.agents.ReasoningBank.consolidator import MemoryConsolidator
from agentlite.agents.ReasoningBank.models import MemoryItem, MemoryEntry, TrajectoryResult, ReActStep

from agentlite.agent_prompts.ReasoningBankPrompt import ReasoningBankPromptGen
from agentlite.agent_prompts.prompt_utils import DEFAULT_PROMPT
from agentlite.agents.agent_utils import *
from agentlite.commons import AgentAct, TaskPackage, EHRManager
from agentlite.commons.embedding_paths import resolve_bge_m3_path
from agentlite.commons.AgentAct import ActObsChainType
from agentlite.llm.agent_llms import BaseLLM
from agentlite.logging import DefaultLogger
from agentlite.logging.terminal_logger import AgentLogger
from agentlite.memory.AgentSTMemory import AgentSTMemory, MultipleTrialSTMemory
from agentlite.agents.MCPAgent import MCPBaseAgent


class MCPReasoningBankAgent(MCPBaseAgent):
    """
    MCP Agent with closed-loop memory integration.

    Implements the complete ReasoningBank cycle:
    1. Retrieve relevant memories
    2. Act with memory-augmented prompts (ReAct format)
    3. Judge trajectory success/failure
    4. Extract new memory items
    5. Consolidate into memory bank

    Uses ReAct (Reasoning + Acting) format for agent execution.
    """

    def __init__(
        self,
        name: str,
        role: str,
        llm: BaseLLM,
        constraint: str = DEFAULT_PROMPT["constraint"],
        instruction: str = DEFAULT_PROMPT["agent_instruction"],
        logger: AgentLogger = DefaultLogger,
        mcp_url: str = None,
        num_runs: int = 1, 
        resume: bool = True,
        update_memory: bool = True,
        task_name: str = None,
        memory_cache_path: str = None,
        # Memory bank settings
        enable_memory_injection: bool = True,
        extract_from_failures: bool = True,
        # Retriever settings
        top_k_retrieval: int = 3,
        similarity_threshold: float = 0.5,
        embedding_model_path: str = None,
        embedding_device: int = 0,
        # Extractor settings
        extraction_temperature: float = 0.9,
        max_memory_items: int = 3,
        # Judge settings
        score_threshold: float = 0.2,
        use_historical_comparison: bool = False,
        use_weighted_score: bool = True,
        f1_weight: float = 0.4,
        precision_weight: float = 0.2,
        recall_weight: float = 0.4,
        # Logging
        enable_logging: bool = True,
        **kwargs
    ):
        """
        Initialize MCP ReasoningBank agent.

        Args:
            name: the name of this agent
            role: the role of this agent
            llm: the language model for this agent
            constraint: the constraints of this agent
            instruction: the agent instruction
            logger: the logger for this agent
            mcp_url: MCP server URL
            num_runs: number of runs (default: 1)
            resume: whether to resume from the last data (default: False)
            update_memory: whether to update the memory bank (default: True)
            enable_memory_injection: Enable memory retrieval and injection (default: True)
            extract_from_failures: Extract memories from failed trajectories (default: False)
            top_k_retrieval: Number of memories to retrieve (default: 5)
            similarity_threshold: Minimum similarity for retrieval (default: 0.5)
            embedding_model_path: Path to embedding model
            embedding_device: Device to use for embedding model
            extraction_temperature: Temperature for memory extraction (default: 1.0)
            max_memory_items: Max memory items to extract per trajectory (default: 3)
            score_threshold: Minimum F1 score for success (default: 0.5)
            use_historical_comparison: Use historical comparison for judging (default: False)
            use_weighted_score: Use weighted score for judging (default: False)
            f1_weight: Weight for F1 in weighted scoring (default: 0.4)
            precision_weight: Weight for precision in weighted scoring (default: 0.2)
            recall_weight: Weight for recall in weighted scoring (default: 0.4)
            enable_logging: Enable logging (default: True)
            **kwargs: Additional arguments for MCPBaseAgent
        """
        embedding_model_path = kwargs.get("retriever_path", embedding_model_path)
        embedding_model_path = resolve_bge_m3_path(embedding_model_path)
        # Initialize parent class
        super().__init__(
            name=name,
            role=role,
            llm=llm,
            constraint=constraint,
            instruction=instruction,
            logger=logger,
            mcp_url=mcp_url,
            **kwargs
        )
        
        # Store settings
        self.task = task_name
        self.num_runs = num_runs
        self.resume = resume
        self.memory_cache_path = memory_cache_path
        self.update_memory = update_memory
        self.enable_memory_injection = enable_memory_injection
        self.top_k_retrieval = top_k_retrieval
        self.extract_from_failures = extract_from_failures
        self.enable_logging = enable_logging
        
        # ReasoningBankPromptGen is used by the extractor for memory extraction
        # For agent prompts, we use the parent class's prompt generation
        # self.prompt_gen is already initialized in parent MCPBaseAgent

        # Initialize all components without config
        self.judge = TrajectoryJudge(
            score_threshold=score_threshold,
            use_historical_comparison=use_historical_comparison,
            f1_weight=f1_weight,
            precision_weight=precision_weight,
            recall_weight=recall_weight,
            use_weighted_score=use_weighted_score
        )
        
        self.extractor = MemoryExtractor(
            llm=self.llm,
            temperature=extraction_temperature,
            max_items=max_memory_items
        )
        
        self.retriever = EHRRetriever(
            llm=self.llm,
            top_k=top_k_retrieval,
            similarity_threshold=similarity_threshold,
            cache_path=os.path.join(self.memory_cache_path, self.task, "embedding_cache.json"),
            embedding_model_path=embedding_model_path,
            embedding_device=embedding_device,
            update_cache=self.update_memory
        )
        
        self.consolidator = MemoryConsolidator(
            memory_bank_path=os.path.join(self.memory_cache_path, self.task, "memory_bank.json"),
            enable_logging=enable_logging
        )
        
        self.scores = []

    async def execute(self, task: TaskPackage):
        """
        Execute task with complete MCP ReasoningBank integration.

        Full cycle:
        0. Manually load EHR data
        1. Retrieve relevant memories
        2. Execute task with memory-augmented prompt (react)
        3. Judge success/failure
        4. Extract new memories
        5. Consolidate into memory bank

        Args:
            task: The task which agent receives and solves
        """
        # Step 0: Manually call load_ehr tool before the main execution
        subject_id = task.subject_id
        timestamp = task.timestamp
        
        if subject_id and timestamp:
            async with self.mcp_client:
                # Read table list
                table_list_result = await self.mcp_client.call_tool(
                    "read_resource_data",
                    {"uri": f"cache://ehr/ehr_data/{subject_id}/table_list.json"}
                )
                table_name_list = json.loads(table_list_result.content[0].text)
                table_list = []
                for table_name in table_name_list:
                    table_data_result = await self.mcp_client.call_tool(
                        "read_resource_data",
                        {"uri": f"cache://ehr/ehr_data/{subject_id}/{table_name}.json"}
                    )
                    table_data = json.loads(table_data_result.content[0].text)
                    df = pd.DataFrame(table_data)
                    table_list.append({
                        "table_name": table_name,
                        "table_data": df
                    })
                ehr_data = {
                    "subject_id": subject_id,
                    "timestamp": timestamp,
                    "table_list": table_list,
                }

        query = task.instruction

        # Step 1: Retrieve relevant memories
        retrieved_memories = []
        if self.enable_memory_injection:
            memory_bank = self.consolidator.get_all_entries()
            retrieved_memories = self.retriever.retrieve(
                ehr_data, memory_bank, k=self.top_k_retrieval
            )
        
        # Inject memory into system prompt if available
        if retrieved_memories:
            memory_augmented_prompt = self._build_memory_section(retrieved_memories)
            # Update the first user message with memory context
            self.messages[0]["content"] += "\n\n" + memory_augmented_prompt

        # Step 2: React (execute task with memory-augmented prompt)
        trajectory_steps = await self.react(task)

        # Build full trajectory string
        full_trajectory = self._format_trajectory(trajectory_steps)
        score, predictions = self.f1_score(task.answer, task.ground_truth)
        final_state = predictions if predictions else "No answer"
        model_output = task.answer

        if self.update_memory:
            # Step 3: Judge success/failure
            success = self.judge.judge_trajectory_success(score, self.scores)
            self.scores.append(score)

            # Step 4: Extract memories (respecting extract_from_failures setting)
            memory_items = []
            should_extract = success or self.extract_from_failures

            if should_extract:
                memory_items = self.extractor.extract_memories(
                    query, trajectory_steps, final_state, model_output, success
                )

            # Step 5: Consolidate
            entry_id = self.consolidator.add_from_trajectory(
                subject_id,
                trajectory=trajectory_steps,
                final_state=final_state,
                model_output=model_output,
                success=success,
                memory_items=memory_items,
                steps_taken=len(trajectory_steps),
                timestamp=timestamp # Pass the EHR timestamp
            )

    async def react(self, task: TaskPackage) -> List[ReActStep]:
        """
        Execute task using ReAct format with MCP tools (similar to MCPReflexionAgent.execute_one_trial).

        Args:
            task: Task to execute

        Returns:
            List[ReActStep]: List of trajectory steps
        """
        task.completion = "active"
        task.answer = None

        step_size = 0
        trajectory_steps = []
        self.logger.execute_task(task=task, agent_name=self.name)

        while task.completion == "active" and step_size < self.max_exec_steps:
            action = await self.__next_act__()
            self.logger.take_action(action, agent_name=self.name, step_idx=step_size+1)
            observation = await self.forward(task, action)
            self.logger.get_obs(obs=observation)
            self.__st_memorize__(task, action, observation)

            thinking = ""
            executions = self.logger.get_all_executions()
            if executions and "thinking_steps" in executions and executions["thinking_steps"]:
                thinking = executions["thinking_steps"][-1] if executions["thinking_steps"] else ""
            step = ReActStep(
                step_num=step_size + 1,
                think=thinking,
                action=action.name,
                observation=observation,
            )
            trajectory_steps.append(step)

            step_size += 1
        
        if task.completion == "active" and step_size == self.max_exec_steps:
            action = await self.__forced_termination__()
            self.logger.take_action(action, agent_name=self.name, step_idx=step_size+1)
            observation = await self.forward(task, action)
            self.logger.get_obs(obs=observation)

            thinking = ""
            executions = self.logger.get_all_executions()
            if executions and "thinking_steps" in executions and executions["thinking_steps"]:
                thinking = executions["thinking_steps"][-1] if executions["thinking_steps"] else ""
            step = ReActStep(
                step_num=step_size + 1,
                think=thinking,
                action=action.name,
                observation=observation,
            )
            trajectory_steps.append(step)

            step_size += 1
            
        self.logger.end_execute(task=task, agent_name=self.name)
        
        # Return trajectory steps
        return trajectory_steps    

    def _build_memory_section(self, memories: List[tuple[float, MemoryEntry]]) -> str:
        """
        Build memory section to inject into prompt.

        Args:
            memories: Retrieved memories (list of (score, entry) tuples)

        Returns:
            str: Memory section text
        """
        memory_section = "## Relevant Past Experience\n\n"
        memory_section += "Here are relevant strategies from similar tasks:\n\n"

        for i, (score, mem) in enumerate(memories, 1):
            memory_section += f"### Memory {i} (Similarity: {score:.2f})\n"
            
            # Access memory items from object or dict
            memory_items = getattr(mem, 'memory_items', [])

            for item in memory_items:
                title = getattr(item, 'title', None) or (item.get('title') if isinstance(item, dict) else 'No Title')
                description = getattr(item, 'description', None) or (item.get('description') if isinstance(item, dict) else '')
                content = getattr(item, 'content', None) or (item.get('content') if isinstance(item, dict) else '')
                
                memory_section += f"- **{title}**: {description}\n"
                memory_section += f"  *Content*: {content}\n"
            memory_section += "\n"

        return memory_section

    def _format_trajectory(self, steps: List[ReActStep]) -> str:
        """
        Format trajectory steps into string.

        Args:
            steps: List of ReAct steps

        Returns:
            str: Formatted trajectory
        """
        trajectory = ""
        for step in steps:
            trajectory += f"Step {step.step_num}:\n"
            trajectory += step.to_string() + "\n\n"
        return trajectory

    def get_memory_bank(self) -> List[MemoryEntry]:
        """
        Get all entries in memory bank.

        Returns:
            List[MemoryEntry]: All memory entries
        """
        return self.consolidator.get_all_entries()

    def get_statistics(self) -> Dict[str, Any]:
        """
        Get memory bank statistics.

        Returns:
            Dict[str, Any]: Statistics
        """
        return self.consolidator.get_statistics()
    
    def f1_score(self, output, standard_answer):
        predictions = set()
        try:
            if isinstance(output, str):
                pattern = r"```json\s*(.*?)\s*```"
                match = re.search(pattern, output, re.DOTALL)
                if match:
                    output = match.group(1).strip()
                output = eval(output)
            
            if isinstance(output, list):
                for item in output:
                    predictions.add(item)
            else:
                raise NotImplementedError
        except (json.JSONDecodeError, Exception) as e:
            print(f"Warning: Could not parse JSON: {e}")
        
        ground_truth = set()
        for answer in standard_answer:
            if answer['name'] is not None and not pd.isna(answer['name']) and isinstance(answer['name'], str):
                ground_truth.add(answer['name'])

        if len(predictions) == 0:
            return {'f1_score': 0.0, 'precision': 0.0, 'recall': 0.0}, list(predictions)
        predictions_lower = {pred.lower() for pred in predictions}
        ground_truth_lower = {gt.lower() for gt in ground_truth}
        
        intersection = predictions_lower.intersection(ground_truth_lower)
        precision = len(intersection) / len(predictions_lower)
        recall = len(intersection) / len(ground_truth_lower) if len(ground_truth_lower) > 0 else 0.0
        f1_score = 2 * (precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0
        score = {
            'f1_score': f1_score,
            'precision': precision,
            'recall': recall
        }
        return score, list(predictions)
